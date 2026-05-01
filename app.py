"""
SGS MLBB Hero Guide - Flask application.
Pulls LIVE hero data from the OpenMLBB API and caches responses for 6h.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, render_template, request, send_from_directory, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from crosscheck import run_crosscheck

try:
    from flask_compress import Compress
    _HAS_COMPRESS = True
except ImportError:  # graceful degradation in dev
    _HAS_COMPRESS = False

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Sourced from config.py — the single source of truth for env-var reads.
# Module-level aliases preserved so the rest of app.py and Jinja templates
# don't need to change.
from config import config as _config

API_BASE = _config.API_BASE
CACHE_SECONDS = _config.CACHE_SECONDS
REQUEST_TIMEOUT = _config.REQUEST_TIMEOUT
PORT = _config.PORT

SITE_NAME = _config.SITE_NAME
SITE_SHORT = _config.SITE_SHORT
SITE_TAGLINE = _config.SITE_TAGLINE
# SITE_URL: if unset or placeholder, we fall back to the live request host
# (so Cloudflare-tunnel URLs / future real domain just work without code edits).
_SITE_URL_ENV = _config.SITE_URL
_PLACEHOLDER_HOSTS = {"sgs.singapore", "example.com", "localhost", ""}
SITE_URL = _SITE_URL_ENV  # may be "" — see dynamic_site_url() below

TELEGRAM_URL = _config.TELEGRAM_URL
GA4_ID = _config.GA4_ID  # placeholder

# Search-engine verification tags (paste the content value from each console).
# Leave blank until you have them; the meta tags just won't render.
GOOGLE_VERIFICATION = _config.GOOGLE_VERIFICATION
BING_VERIFICATION   = _config.BING_VERIFICATION
YANDEX_VERIFICATION = _config.YANDEX_VERIFICATION

# IndexNow key — free instant-indexing protocol for Bing / Yandex / Seznam /
# Naver. Generate with `python tools/generate_indexnow_key.py`. Once set, the
# key is hex-only, verified by a self-hosted file at /indexnow-<KEY>.txt.
INDEXNOW_KEY = _config.INDEXNOW_KEY
# Validation below after `log` is initialized — deferred so we don't crash at import.

# Feature flag — flip to "true" in .env to expose the Top Up section
# (routes, nav links, sitemap entries). All top-up code stays loaded on disk
# regardless; this only gates user-facing entry points.
TOPUP_ENABLED = _config.TOPUP_ENABLED

# Geo-targeting — hard-coded to Singapore for this site's audience.
GEO_REGION = "SG"
GEO_PLACENAME = "Singapore"
GEO_ICBM = "1.3521, 103.8198"  # Singapore lat/lng centroid
LANG_TAG = "en-SG"

ROLE_COLORS = {
    "Tank":     "#4FC3F7",
    "Fighter":  "#FF8A65",
    "Mage":     "#CE93D8",
    "Marksman": "#A5D6A7",
    "Support":  "#FFD54F",
    "Assassin": "#EF9A9A",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sgs")

if INDEXNOW_KEY and not re.fullmatch(r"[a-f0-9]{8,128}", INDEXNOW_KEY):
    log.warning(
        "INDEXNOW_KEY must be 8-128 hex chars; got %r. "
        "IndexNow submissions will be rejected until fixed.",
        INDEXNOW_KEY,
    )

app = Flask(__name__, static_folder="static", template_folder="templates")

# Trust 1 proxy hop so `request.url_root` reflects the PUBLIC host (Cloudflare
# tunnel / nginx) — not the internal gunicorn bind. Without this, canonical
# URLs would be "http://127.0.0.1:8085/…" which is poison for SEO.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ---- Top-up payment blueprint -------------------------------------------- #
# Routes live in topup_payment.py; HitPay + MooGold clients are side modules.
# Registration is lazy-safe: if env vars are missing, endpoints degrade to
# the mock supplier and refuse to create real HitPay payments.
# Gated by TOPUP_ENABLED — when false, the module is never imported and the
# /api/topup/* endpoints simply do not exist.
if TOPUP_ENABLED:
    try:
        from topup_payment import bp as topup_bp
        app.register_blueprint(topup_bp)
        logging.getLogger(__name__).info("Registered topup_payment blueprint at /api/topup")
    except Exception as _e:  # pragma: no cover
        logging.getLogger(__name__).exception("Failed to register topup blueprint: %s", _e)
else:
    logging.getLogger(__name__).info("Top Up feature disabled (TOPUP_ENABLED=false). Skipping blueprint registration.")

# ---- SEO blueprint -------------------------------------------------------- #
# Sitemaps, robots.txt, manifest, favicon, IndexNow ownership file. All these
# routes live in blueprints/seo.py — purely crawler-facing, no Jinja templates.
from blueprints.seo import bp as seo_bp
app.register_blueprint(seo_bp)

# ---- Heroes blueprint ----------------------------------------------------- #
# Homepage, hero detail, tier list, role + lane landing pages. The traffic-
# heaviest set of routes — ~95% of organic search hits land here.
from blueprints.heroes import bp as heroes_bp
app.register_blueprint(heroes_bp)

# ---- Response compression (brotli > gzip > deflate) ---------------------- #
# Cuts HTML/CSS/JS/XML bytes 70-85% on the wire. Biggest single perf win.
if _HAS_COMPRESS:
    app.config["COMPRESS_ALGORITHM"] = ["br", "gzip", "deflate"]
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "text/xml", "text/plain",
        "application/json", "application/javascript",
        "application/xml", "application/xml+rss",
        "image/svg+xml",
    ]
    app.config["COMPRESS_LEVEL"] = 6          # balanced CPU / ratio
    app.config["COMPRESS_BR_LEVEL"] = 5       # brotli is slow at high levels
    app.config["COMPRESS_MIN_SIZE"] = 500     # skip tiny payloads
    Compress(app)
else:
    log.info("flask-compress not installed — responses will not be compressed.")

# Longer default static file cache (Flask default is 12h; we go a year).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 365


# --------------------------------------------------------------------------- #
# Cache layer + cached HTTP wrapper now live in lib/openmlbb.py (Sprint 4
# Phase A, step 1). Public names are re-exported here so the rest of app.py
# and any downstream callers keep working unchanged.
# --------------------------------------------------------------------------- #
from lib.openmlbb import (
    # Cache layer + HTTP wrapper
    api_get,
    cache_age_text,
    cache_modified_iso,
    make_cache_key,
    _read_disk_cache,  # used by health-check probes below
    # Utility helpers used directly by routes/templates
    classify_counter,
    pct,
    primary_role,
    slugify,
    tier_from_winrate,
    # Concurrency primitive — used by hero_page parallel fan-out
    _EXECUTOR,
    # Catalog + accessor functions used by routes
    get_academy_builds,
    get_all_heroes,
    get_equipment_map,
    get_hero_combos,
    get_hero_compat,
    get_hero_counters,
    get_hero_detail,
    get_hero_stats,
    get_tier_list,
    hero_index_by_id,
)


def role_color(role: str) -> str:
    return ROLE_COLORS.get(role, "#FFD700")


# --------------------------------------------------------------------------- #
# Warm cache on startup — synchronous for the two "every page" payloads,
# then a background thread fills the rest (academy builds + top-10 hero details)
# so the first hero-detail view is warm even on cold boots.
# --------------------------------------------------------------------------- #
def _warm_background(top_ids: list[int]) -> None:
    """Runs in a daemon thread after server start. No exceptions leak."""
    try:
        log.info("Background warm — fetching catalogues …")
        api_get("/api/academy/equipment/expanded", {"size": 500})
        api_get("/api/academy/equipment", {"size": 500})
        api_get("/api/academy/emblems", {"size": 500})
        api_get("/api/academy/spells", {"size": 200})

        log.info("Background warm — pre-fetching %d hero details …", len(top_ids))
        futs = []
        for hid in top_ids:
            futs.append(_EXECUTOR.submit(api_get, f"/api/heroes/{hid}"))
            futs.append(_EXECUTOR.submit(api_get, f"/api/heroes/{hid}/stats", {"rank": "all"}))
            futs.append(_EXECUTOR.submit(api_get, f"/api/heroes/{hid}/counters", {"days": 7, "rank": "all"}))
            futs.append(_EXECUTOR.submit(api_get, f"/api/heroes/{hid}/compatibility", {"days": 7, "rank": "all"}))
            futs.append(_EXECUTOR.submit(api_get, f"/api/heroes/{hid}/skill-combos"))
            futs.append(_EXECUTOR.submit(api_get, f"/api/academy/heroes/{hid}/recommended", {"size": 50}))
        for f in futs:
            try:
                f.result(timeout=REQUEST_TIMEOUT * 2)
            except Exception:  # noqa: BLE001
                pass
        log.info("Background warm done.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Background warm failed — %s", exc)


def warm_cache() -> None:
    """Fast, synchronous warm — only the two payloads every page needs.
    Everything else is filled in by a daemon thread so the server is usable in
    well under 2 seconds from a cold boot."""
    log.info("Warming cache — fetching hero list …")
    heroes = get_all_heroes()
    log.info("Got %d heroes. Caching tier list …", len(heroes))
    tier = get_tier_list("all")

    # Kick off the heavier fills in the background.
    top_ids = [h["id"] for h in tier[:10] if h.get("id")]
    threading.Thread(target=_warm_background, args=(top_ids,), daemon=True,
                     name="sgs-warm-bg").start()
    log.info("Cache warm. Academy + top-10 hero details filling in background.")


# --------------------------------------------------------------------------- #
# Tip generation (rule-based, role-aware)
# --------------------------------------------------------------------------- #
ROLE_TIPS: dict[str, list[str]] = {
    "Tank": [
        "Always initiate team fights — your crowd control is what opens the window for your damage dealers.",
        "Buy Tough Boots or Warrior Boots early against heavy CC enemies, and swap to Dominance Ice late game.",
        "Ward the jungle entrances and river bushes — information wins games more than damage does.",
        "Protect your Marksman during drafts and split-push pressure — peel, don't chase kills.",
        "Save ultimate for shutting down enemy carries, not opening engagements.",
    ],
    "Fighter": [
        "Snowball the early game — fighters fall off if the enemy scales uncontested.",
        "Build sustain items like Bloodlust Axe or Queen's Wings when behind to survive teamfights.",
        "Use side lane pressure to create map objectives — fighters are strongest as split pushers.",
        "Weave in basic attacks between skills to maximise passive procs and item effects.",
        "Look for flanks instead of front-line brawling once enemies have their core items.",
    ],
    "Mage": [
        "Clear waves fast, then rotate to help jungle or roam — mana efficiency matters more than chasing kills.",
        "Build Clock of Destiny or Lightning Truncheon first for power spikes at mid-game.",
        "Always stay behind your tank in team fights — your job is burst damage, not absorbing skillshots.",
        "Use bushes to bait enemies into range — mages win when the enemy overextends.",
        "Purchase Immortality or Winter Truncheon as late-game survival against assassins.",
    ],
    "Marksman": [
        "Farm safely until your second core item — marksmen only come online after Windtalker or Berserker's Fury.",
        "Always position at the edge of fights — one mispositioned marksman is a lost game.",
        "Buy Wind of Nature against heavy physical burst dealers like Karrie or Beatrix.",
        "Use Purify or Flicker for spell — Inspire is fine, but survival spells win lane 1v2 scenarios.",
        "Keep a wave pushing before turret dives — back-up waves make dives suicidal.",
    ],
    "Support": [
        "Your job is vision and protection — keep 2 wards up at all times in the river and jungle.",
        "Pick up your Marksman before they farm 6 — supports snowball by enabling their carry.",
        "Use ultimates reactively, not pre-emptively — save skills for saves, not damage trades.",
        "Always rotate before team fights — supports who arrive late are supports who lose.",
        "Build Oracle or Guardian Helmet to keep your whole team alive longer than the enemy frontline.",
    ],
    "Assassin": [
        "Always start in the jungle — assassins farm the jungle, not the lane.",
        "Punish overextended supports and mages — avoid 1v1s against fighters with sustain.",
        "Ward enemy jungle entrances and counter-jungle at level 4 to break the enemy's early tempo.",
        "Build Blade of Despair or Hunter Strike first to snowball — assassins need the power spike.",
        "Pick off stragglers between team fights rather than diving into 5v1 engagements.",
    ],
}


def hero_tips(role: str, hero_name: str) -> list[str]:
    tips = list(ROLE_TIPS.get(role, ROLE_TIPS["Fighter"]))
    # Personalise first tip with hero name
    if tips:
        tips[0] = tips[0].replace("fighters", f"{hero_name}").replace("assassins", f"{hero_name}").replace("marksmen", f"{hero_name}").replace("mages", f"{hero_name}")
    return tips[:5]


# --------------------------------------------------------------------------- #
# Counter items: keyed by hero speciality tag or role.
# Item names must match EXACTLY what's in the equipment API (for icon lookup).
# Unresolved names still render as plain text without an icon.
# --------------------------------------------------------------------------- #
COUNTER_ITEM_RULES: list[dict] = [
    {
        "triggers": ["Regen", "Sustain", "Heal"],
        "items": ["Sea Halberd", "Necklace of Durance"],
        "reason": "Reduces healing received by 50% — essential against regen-heavy heroes.",
    },
    {
        "triggers": ["HP", "High HP", "Tank"],
        "items": ["Demon Hunter Sword", "Calamity Reaper"],
        "reason": "Deals % max HP damage — punishes heroes stacking health items.",
    },
    {
        "triggers": ["Physical", "Burst", "Fighter", "Marksman"],
        "items": ["Antique Cuirass", "Dominance Ice", "Thunder Belt"],
        "reason": "Reduces physical attack and slows — limits burst damage output.",
    },
    {
        "triggers": ["Magic", "Mage", "Poke"],
        "items": ["Athena's Shield", "Radiant Armor"],
        "reason": "Magic damage reduction — essential against sustained magic dealers.",
    },
    {
        "triggers": ["Crowd Control", "CC", "Stun", "Lock"],
        "items": ["Tough Boots"],
        "reason": "Reduces crowd control duration — makes CC heroes less effective.",
    },
    {
        "triggers": ["Shield", "Protect"],
        "items": ["True Damage", "Malefic Roar"],
        "reason": "Penetrates or bypasses shields — counters passive shield mechanics.",
    },
    {
        "triggers": ["Assassin", "Blink", "Chase"],
        "items": ["Wind of Nature", "Winter Truncheon"],
        "reason": "Invulnerability on activation — lets you survive burst combos.",
    },
    {
        "triggers": ["Support", "Heal Ally"],
        "items": ["Sea Halberd", "Necklace of Durance", "Dominance Ice"],
        "reason": "Anti-heal items stack in team fights — shuts down support-dependent comps.",
    },
]


def get_counter_items(hero_detail: dict, equipment_map: dict) -> list[dict]:
    """Derive counter items for a hero from speciality tags + role.

    Returns up to 4 {name, icon, reason} dicts. Item icons resolve against
    the live equipment catalogue — names that don't match render as plain text.
    """
    specialities = [s.lower() for s in (hero_detail.get("speciality") or [])]
    role = (hero_detail.get("roles") or ["Fighter"])[0]
    tags = specialities + [role.lower()]

    name_to_icon: dict[str, str | None] = {}
    for item in equipment_map.values():
        name = item.get("name")
        if name:
            name_to_icon[name] = item.get("icon")

    seen_items: set[str] = set()
    results: list[dict] = []

    for rule in COUNTER_ITEM_RULES:
        if not any(trigger.lower() in tag for trigger in rule["triggers"] for tag in tags):
            continue
        for item_name in rule["items"]:
            if item_name in seen_items:
                continue
            seen_items.add(item_name)
            results.append({
                "name":   item_name,
                "icon":   name_to_icon.get(item_name),
                "reason": rule["reason"],
            })
        if len(results) >= 4:
            break

    return results[:4]


def build_hero_faqs(
    name: str,
    role: str,
    tier: str,
    wr_pct: str,
    counters: list[dict],
    synergies: list[dict],
    difficulty: str | None,
    lanes: list[str] | None,
) -> list[dict]:
    """Generate FAQ entries tailored to this specific hero.

    These populate BOTH the visible FAQ section on the page AND the FAQPage
    schema.org block — which is what triggers Google's expandable FAQ rich
    result (huge SERP real-estate win for long-tail queries).
    """
    tier_label = {
        "SS": "meta-defining (elite pick this patch)",
        "S":  "dominant (a strong first-pick)",
        "A":  "reliable (solid performer)",
        "B":  "balanced (situational)",
        "C":  "niche (needs the right comp)",
        "D":  "currently underperforming",
    }.get(tier, "average")

    faqs: list[dict] = []

    faqs.append({
        "q": f"Is {name} good in the current MLBB meta (2026)?",
        "a": (
            f"{name} is currently {tier_label} with a {wr_pct} win rate in ranked play "
            f"over the last 7 days. Our tier list is refreshed every 6 hours from real "
            f"match data across all ranks from Epic to Mythic Glory — see the live "
            f"number at the top of this page for the latest status."
        ),
    })

    lane_text = ""
    if lanes:
        lane_text = " " + "/".join(lanes).lower() + " lane"
    faqs.append({
        "q": f"What is the best lane and role for {name}?",
        "a": (
            f"{name} is a {role.lower()} hero{lane_text}. Stick to the role the hero is "
            f"designed for — forcing a {role.lower()} into an off-role almost always "
            f"drops their win rate by 3-5%. Check the Overview tab on this page for the "
            f"recommended lane layout."
        ),
    })

    if counters:
        top = counters[0].get("name") or "a strong counter"
        others = ", ".join(c.get("name", "") for c in counters[:3] if c.get("name"))
        faqs.append({
            "q": f"Who counters {name} in Mobile Legends?",
            "a": (
                f"The strongest counters to {name} right now are {others}. "
                f"{top} alone can flip a {name} matchup by several percent — if you see "
                f"any of these picked, adjust your build or lane assignment. Full counter "
                f"list with live win-rate impact is in the Counters tab."
            ),
        })

        # Hard vs soft counter FAQ — direct match for \"hard counter to X\" queries,
        # which is one of the single most-searched MLBB long-tail patterns.
        hard = [c for c in counters if c.get("strength") == "hard"]
        soft = [c for c in counters if c.get("strength") == "soft"]
        if hard or soft:
            hard_names = ", ".join(c.get("name", "") for c in hard[:3] if c.get("name"))
            soft_names = ", ".join(c.get("name", "") for c in soft[:3] if c.get("name"))
            parts = []
            if hard_names:
                parts.append(
                    f"Hard counters to {name}: {hard_names}. These heroes' kits "
                    f"fundamentally suppress {name} — expect a +3% or larger win-rate "
                    f"swing in the opponent's favour."
                )
            if soft_names:
                parts.append(
                    f"Soft counters: {soft_names}. The matchup tilts against {name} "
                    f"but is still playable with careful positioning and itemisation."
                )
            faqs.append({
                "q": f"What is the hardest counter to {name}?",
                "a": " ".join(parts),
            })

    if synergies:
        top = synergies[0].get("name") or "a strong pair"
        others = ", ".join(s.get("name", "") for s in synergies[:3] if s.get("name"))
        faqs.append({
            "q": f"Which heroes work best with {name}?",
            "a": (
                f"{name} pairs best with {others}. {top} synergy is particularly strong "
                f"this patch — combo-drafting them together boosts your win rate in "
                f"ranked. Full synergy list with partner win-rate deltas is on the "
                f"Synergy tab."
            ),
        })

    if difficulty:
        faqs.append({
            "q": f"Is {name} hard to play?",
            "a": (
                f"{name} has a {difficulty.lower()} difficulty rating. Beginners should "
                f"practise the combo sequence in custom mode for at least 20 games before "
                f"taking {name} into ranked. The Skills and Tips tabs on this page walk "
                f"through the exact combo order used by Mythic-rank SGS players."
            ),
        })

    faqs.append({
        "q": f"What is the best build for {name} in 2026?",
        "a": (
            f"The Builds tab on this page shows the highest-voted community builds "
            f"for {name}, sorted by popularity and refreshed every 6 hours. Each build "
            f"lists the exact item order, emblem talent path and battle spell — pick the "
            f"build that matches your current patch version."
        ),
    })

    faqs.append({
        "q": f"How do I find {name} mains to scrim with in Singapore?",
        "a": (
            f"Join the Singapore Gaming Syndicate Telegram group — it's free, there's "
            f"no sign-up, and every member is verified. You'll find {name} mains across "
            f"Epic, Legend, Mythic and Mythic Glory ranks looking for daily scrims."
        ),
    })

    return faqs


def build_hero_pool(filtered: list[dict]) -> dict[str, Any]:
    """Construct a 3-hero pool recommendation for a role landing page:

        - comfort pick   : highest pick rate (safest to spam)
        - counter pick   : niche hero with above-avg WR but low pick rate
                           (surprise value — best counter-draft option)
        - safe blind pick: highest WR among the top-5 most-picked
                           (low bust risk when drafted first)

    This mirrors the "3-hero pool" framework from Boostroom and ONE Esports
    guides — the dominant content pattern on page 1 of Google for
    'best heroes to climb mlbb' style queries.
    """
    if not filtered:
        return {}

    by_pick = sorted(filtered, key=lambda x: x.get("pick_rate") or 0, reverse=True)
    by_win = sorted(filtered, key=lambda x: x.get("win_rate") or 0, reverse=True)

    comfort = by_pick[0] if by_pick else None
    top5_picked = by_pick[:5]
    safe_blind = max(top5_picked, key=lambda x: x.get("win_rate") or 0) if top5_picked else None
    # Counter pick: highest WR hero that's NOT in the top-5 most-picked.
    top5_ids = {h["id"] for h in top5_picked}
    counter = next((h for h in by_win if h["id"] not in top5_ids), None)

    if comfort and safe_blind and comfort["id"] == safe_blind["id"] and len(by_pick) > 1:
        safe_blind = by_pick[1]

    return {
        "comfort":    comfort,
        "counter":    counter,
        "safe_blind": safe_blind,
    }


# --------------------------------------------------------------------------- #
# Lanes — /lane/<slug> landing pages.
#
# MLBB has 5 canonical lanes: jungle / mid / exp / gold / roam. The OpenMLBB
# API returns them as {"Jungle","Mid Lane","Exp Lane","Gold Lane","Roam"} in
# hero.lanes. We map slugs <-> canonical names here, plus intro copy per lane
# (keyword-dense, pro-coach tone — same pattern as ROLE_INTRO).
# --------------------------------------------------------------------------- #
LANE_META: dict[str, dict[str, Any]] = {
    "jungle": {
        "canonical": "Jungle",
        "label": "Jungler",
        "color": "#8bc34a",
        "intro": (
            "The Jungle lane is where MLBB games are decided. Junglers farm "
            "the Turtle/Lord buffs, rotate for ganks and snowball the tempo "
            "of the match. A strong jungler in the 2026 meta is one who "
            "clears fast, secures objectives and can 1-shot the enemy "
            "carries. Below is the live SGS tier list for every hero that "
            "plays jungle — ranked by real 7-day win rate across Epic to "
            "Mythic Glory ranked games."
        ),
        "tips": [
            "Clear your first buff, rotate to a lane for an invade or gank.",
            "Secure the first Turtle at 2:00 — hard priority over split-pushing.",
            "Always ping Retribution + smoke timings before Lord attempts.",
        ],
    },
    "mid": {
        "canonical": "Mid Lane",
        "label": "Mid-laner",
        "color": "#ce93d8",
        "intro": (
            "The Mid Lane is MLBB's burst-damage role — mages and occasional "
            "assassins who clear waves fast and roam to every skirmish. In "
            "the 2026 meta, mid-laners are expected to secure vision on the "
            "jungle, rotate to side-lanes by 3:00 and hit key spike items "
            "before 8 minutes. Below is the live SGS tier list of every "
            "hero that plays Mid Lane — ranked by real ranked win rate."
        ),
        "tips": [
            "Shove your wave, then help Jungler fight over Turtle or invade.",
            "Buy the first Magic Wand/Clock of Destiny spike by 5:00.",
            "Ward the enemy Jungle entrance — mid has the best rotation paths.",
        ],
    },
    "exp": {
        "canonical": "Exp Lane",
        "label": "Exp-laner",
        "color": "#ff8a65",
        "intro": (
            "The Exp Lane (formerly 'Top lane') is where duelling fighters "
            "and bruiser tanks farm solo XP and create map pressure. The "
            "best MLBB Exp-laners in 2026 are those who can 1v1 the enemy "
            "laner, soak tower aggro, and rotate for team-fights by 4:00. "
            "Below is the live SGS tier list of every hero that plays "
            "Exp Lane — sorted by real 7-day win rate."
        ),
        "tips": [
            "Freeze waves near your tower until Jungler rotates for a dive.",
            "Sustain > damage in lane; itemise for tankiness after Warrior Boots.",
            "TP with Flicker or Vengeance at 3:30 for the first Turtle fight.",
        ],
    },
    "gold": {
        "canonical": "Gold Lane",
        "label": "Gold-laner",
        "color": "#a5d6a7",
        "intro": (
            "The Gold Lane is the marksman lane — the scaling physical "
            "carry of every MLBB team. Gold-laners farm the buff minion "
            "solo, stay safe early, and become the team's win-condition "
            "after their two-item spike. The 2026 meta rewards safe, "
            "kiteable marksmen with crit or attack-speed builds. Below is "
            "the live SGS tier list of every MLBB hero that plays Gold "
            "Lane — ranked by real 7-day win rate."
        ),
        "tips": [
            "Always last-hit the gold buff minion — it gives 10% extra gold.",
            "Retribution-less MMs lose mid-game: build Demon Hunter Sword first.",
            "Respect the Roam's rotation — back the instant they leave bot.",
        ],
    },
    "roam": {
        "canonical": "Roam",
        "label": "Roamer",
        "color": "#ffd54f",
        "intro": (
            "The Roam lane is MLBB's map-control role — tanks and supports "
            "who buy the Roam item (Conceal / Dire Hit / Encourage boots), "
            "ward the map and babysit the Gold-laner early. A great MLBB "
            "roamer in 2026 is one who never dies, stacks vision, and "
            "arrives first to every objective. Below is the live SGS tier "
            "list of every hero that plays Roam — ranked by real ranked "
            "win rate."
        ),
        "tips": [
            "Always equip a Roam item before 3 minutes (lose it past 4:00).",
            "Ward the enemy red buff at 0:30 — the first invade defines the game.",
            "Follow your jungler's ganks, not your own — they pick the timing.",
        ],
    },
}


def lane_slug_from_label(lane_label: str) -> str | None:
    """Map an API-style lane label ('Exp Lane') back to its URL slug ('exp')."""
    norm = (lane_label or "").strip().lower()
    for slug, meta in LANE_META.items():
        if meta["canonical"].lower() == norm:
            return slug
    return None


# Long-form content for /role/<role> landing pages — SEO keyword expansion.
ROLE_INTRO = {
    "tank": (
        "Tanks are the Mobile Legends frontliners — heroes who absorb damage, "
        "crowd-control the enemy team, and create space for their carries. In "
        "the 2026 meta, the best tanks are those who bring both reliable CC and "
        "map pressure. Below is the live SGS tier list for every tank hero, "
        "ranked by real 7-day win rate from ranked matches in Epic through "
        "Mythic Glory."
    ),
    "fighter": (
        "Fighters bridge damage and durability — Mobile Legends' signature "
        "flex class. They dominate the EXP lane and often rotate to deliver "
        "decisive team-fight damage. The 2026 meta rewards fighters with "
        "sustain and burst; below is the live SGS tier list of every fighter "
        "in the game, sorted by real 7-day win rate."
    ),
    "mage": (
        "Mages are Mobile Legends' magic damage core. They clear waves fast, "
        "control team fights with their AOE skills, and scale with magic "
        "power items. Below is the live SGS tier list for every MLBB mage, "
        "ranked by 7-day win rate across ranked play."
    ),
    "marksman": (
        "Marksmen (ADC) are the late-game physical carry in Mobile Legends. "
        "They scale from basic attacks and need peel and vision to survive "
        "the early mid-game. The best MLBB marksmen in the 2026 meta balance "
        "safety and damage — the live tier list below ranks every MM by real "
        "win rate."
    ),
    "support": (
        "Supports keep the team alive and set up picks. A good MLBB support "
        "multiplies the value of every other hero on the team. Below is the "
        "live SGS tier list of every support hero in MLBB, ranked by 7-day "
        "win rate — updated every 6 hours from real match data."
    ),
    "assassin": (
        "Assassins pick off stragglers and dive the enemy backline. They "
        "rule the jungle and need the early farm to scale. The 2026 meta has "
        "shifted towards burst-heavy assassins — the live SGS tier list "
        "below ranks every assassin in Mobile Legends by real ranked "
        "win rate."
    ),
}


# --------------------------------------------------------------------------- #
# Template context helpers
# --------------------------------------------------------------------------- #
# (cache_age_text moved to lib/openmlbb.py and re-exported above.)


# --------------------------------------------------------------------------- #
# Data freshness + health
# --------------------------------------------------------------------------- #
# These are the cache keys whose ages determine site freshness. If ALL of
# these are stale (> STALE_THRESHOLD_HOURS) we show users a warning banner
# instead of silently serving old data as if it were current.
_FRESHNESS_KEYS = (
    "/api/heroes?size=200",
    make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    ),
)
STALE_THRESHOLD_HOURS = int(os.getenv("SGS_STALE_THRESHOLD_H", "24"))


def _oldest_core_cache_age_seconds() -> int | None:
    """Age of the oldest critical cache entry, in seconds. None if we've
    never successfully fetched (first-boot edge case)."""
    ages = []
    now = time.time()
    for k in _FRESHNESS_KEYS:
        disk = _read_disk_cache(k)
        if disk:
            ages.append(now - disk[0])
    if not ages:
        return None
    return int(max(ages))


def data_freshness() -> dict[str, Any]:
    """Returns a dict the templates read to decide whether to show a banner:

        status:  "fresh" | "stale" | "unknown"
        hours:   int | None                     (age of oldest critical entry)
        human:   str                            ("3 hours ago" / "Never fetched")
        threshold: int                          (hours at which we flip to stale)
        as_of:   str | None                     (ISO date of last refresh)
    """
    age_s = _oldest_core_cache_age_seconds()
    if age_s is None:
        return {
            "status": "unknown",
            "hours": None,
            "human": "Never successfully refreshed",
            "threshold": STALE_THRESHOLD_HOURS,
            "as_of": None,
        }
    hours = age_s // 3600
    status = "stale" if hours >= STALE_THRESHOLD_HOURS else "fresh"
    if hours == 0:
        human = f"{max(1, age_s // 60)} min ago"
    elif hours < 24:
        human = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        human = f"{hours // 24} day{'s' if (hours // 24) != 1 else ''} ago"
    as_of = datetime.fromtimestamp(time.time() - age_s, timezone.utc).date().isoformat()
    return {
        "status": status,
        "hours": hours,
        "human": human,
        "threshold": STALE_THRESHOLD_HOURS,
        "as_of": as_of,
    }


def patch_window() -> dict[str, str]:
    """Auto-derived patch label + data-coverage window.

    We don't get a patch number from the API, so this falls back through:
      1. MLBB_PATCH env var                   ("Patch 2.1.61")
      2. Newest hero fallback (heuristic)    ("Patch current")
      3. Plain date window                   ("Data through Apr 18, 2026")

    Crucially: the "data through" date is ALWAYS shown (even with an explicit
    patch tag), so users know exactly how fresh the underlying stats are,
    even if the env-var patch label is weeks out of date.
    """
    freshness = data_freshness()
    env_patch = (os.getenv("MLBB_PATCH") or "").strip()

    # Data-coverage window: the 7-day rolling average ends on the API's last
    # refresh, so "data through {as_of}" is the most honest cutoff label.
    if freshness["as_of"]:
        try:
            as_of_d = datetime.fromisoformat(freshness["as_of"]).date()
            as_of_human = as_of_d.strftime("%b %d, %Y")
        except Exception:
            as_of_human = freshness["as_of"]
    else:
        as_of_human = "unknown"

    if env_patch:
        label = f"Patch {env_patch}"
    else:
        label = "Live MLBB Meta"

    return {
        "label": label,
        "env_patch": env_patch,
        "as_of": freshness["as_of"] or "",
        "as_of_human": as_of_human,
    }


# --------------------------------------------------------------------------- #
# Scheduled health-check: one-line daily log of API reachability + data age.
# --------------------------------------------------------------------------- #
HEALTH_LOG = BASE_DIR / "logs" / "health.log"
_HEALTH_THREAD: threading.Thread | None = None
_HEALTH_STOP = threading.Event()


def _health_once() -> dict[str, Any]:
    """Run one health probe: hit the API, write a log line, return the result."""
    t0 = time.time()
    reachable = False
    err = ""
    try:
        with httpx.Client(timeout=10.0, headers={"User-Agent": "SGS-Healthcheck/1.0"}) as c:
            r = c.get(f"{API_BASE}/api/heroes", params={"size": 1})
            r.raise_for_status()
            reachable = True
    except Exception as e:  # noqa: BLE001
        err = str(e)[:200]

    elapsed_ms = int((time.time() - t0) * 1000)
    fresh = data_freshness()

    status = "OK" if (reachable and fresh["status"] == "fresh") else (
        "STALE" if fresh["status"] == "stale" else
        "DOWN" if not reachable else "UNKNOWN"
    )

    line = (
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"[{status}] api_reachable={reachable} "
        f"cache_age_h={fresh['hours'] if fresh['hours'] is not None else '-'} "
        f"latency_ms={elapsed_ms}"
    )
    if err:
        line += f" err={err!r}"

    try:
        HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Keep the log bounded — rotate at ~2 MB by truncating the oldest half.
        if HEALTH_LOG.exists() and HEALTH_LOG.stat().st_size > 2 * 1024 * 1024:
            kept = HEALTH_LOG.read_text(encoding="utf-8").splitlines()[-1000:]
            HEALTH_LOG.write_text("\n".join(kept) + "\n", encoding="utf-8")
        with HEALTH_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("health log write failed: %s", e)

    # ALSO emit to stderr so systemd/journalctl picks it up for remote monitoring.
    log.info("healthcheck | %s", line)
    return {"status": status, "reachable": reachable, "cache_age_h": fresh["hours"], "latency_ms": elapsed_ms}


def _health_loop(interval_s: int) -> None:
    """Daemon loop — one probe every `interval_s` (default 1h)."""
    # Initial probe after startup (gives API warm-cache time to settle).
    _HEALTH_STOP.wait(60)
    while not _HEALTH_STOP.is_set():
        try:
            _health_once()
        except Exception as e:  # noqa: BLE001
            log.warning("healthcheck iteration crashed: %s", e)
        _HEALTH_STOP.wait(interval_s)


def start_health_monitor() -> None:
    """Spin up the healthcheck thread. Idempotent / no-op if already running."""
    global _HEALTH_THREAD  # noqa: PLW0603
    if _HEALTH_THREAD and _HEALTH_THREAD.is_alive():
        return
    interval_h = float(os.getenv("SGS_HEALTH_INTERVAL_H", "1"))
    interval_s = max(60, int(interval_h * 3600))
    _HEALTH_THREAD = threading.Thread(
        target=_health_loop, args=(interval_s,),
        name="sgs-health", daemon=True,
    )
    _HEALTH_THREAD.start()
    log.info("Health monitor started — probe every %.1fh, log -> %s", interval_h, HEALTH_LOG)


def time_ago(ms: int | None) -> str:
    """Human-friendly relative time from a unix-ms timestamp."""
    if not ms:
        return ""
    age = max(0, int(time.time() - (ms / 1000)))
    if age < 60:
        return "just now"
    if age < 3600:
        m = age // 60
        return f"{m}m ago"
    if age < 86400:
        h = age // 3600
        return f"{h}h ago"
    days = age // 86400
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def static_url(filename: str) -> str:
    """url_for('static', …) + a mtime-based cache-buster so browsers pick up
    CSS/JS changes even with our aggressive year-long static cache headers."""
    path = os.path.join(app.static_folder or "static", filename)
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    return f"{url_for('static', filename=filename)}?v={mtime}"


def dynamic_site_url() -> str:
    """Return the effective public base URL (no trailing slash).

    Priority:
      1. SITE_URL env var if it's a real-looking hostname.
      2. Current request host via `request.url_root` (honours Cloudflare /
         nginx X-Forwarded-* headers thanks to ProxyFix).
      3. Fallback constant if we're outside any request context.
    """
    if _SITE_URL_ENV:
        try:
            host = _SITE_URL_ENV.split("//", 1)[-1].split("/", 1)[0].lower()
            if host not in _PLACEHOLDER_HOSTS:
                return _SITE_URL_ENV
        except Exception:
            pass
    try:
        root = request.url_root  # e.g. "https://foo.trycloudflare.com/"
        return root.rstrip("/")
    except RuntimeError:
        return "https://example.com"  # outside request ctx; rarely reached


def full_url(path: str = "/") -> str:
    """Build a fully-qualified URL for a site-relative path."""
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return dynamic_site_url() + path


@app.context_processor
def inject_globals() -> dict[str, Any]:
    site_url = dynamic_site_url()
    # canonical is a site-relative path set by each route; compose full URL here
    # so templates don't have to string-concat.
    try:
        canonical_path = (request.view_args or {}).get("__canonical", None)  # noqa
    except RuntimeError:
        canonical_path = None
    return {
        "SITE_NAME": SITE_NAME,
        "SITE_SHORT": SITE_SHORT,
        "SITE_TAGLINE": SITE_TAGLINE,
        "SITE_URL": site_url,
        "TELEGRAM_URL": TELEGRAM_URL,
        "GA4_ID": GA4_ID,
        "GOOGLE_VERIFICATION": GOOGLE_VERIFICATION,
        "BING_VERIFICATION": BING_VERIFICATION,
        "YANDEX_VERIFICATION": YANDEX_VERIFICATION,
        "GEO_REGION": GEO_REGION,
        "GEO_PLACENAME": GEO_PLACENAME,
        "GEO_ICBM": GEO_ICBM,
        "LANG_TAG": LANG_TAG,
        "YEAR": datetime.now(timezone.utc).year,
        "TODAY_ISO": datetime.now(timezone.utc).date().isoformat(),
        # Feature flag — controls visibility of /topup nav links and entry points.
        "topup_enabled": TOPUP_ENABLED,
        # Freshness signals — top-ranking MLBB sites (mlbbmeta, bittopup,
        # esports.gg) always include the current month in titles and an
        # explicit "Updated [Month Day, Year]" badge. Google heavily boosts
        # fresh-feeling content for gaming/meta queries.
        "CURRENT_MONTH": datetime.now(timezone.utc).strftime("%B %Y"),
        "CURRENT_MONTH_SHORT": datetime.now(timezone.utc).strftime("%b %Y"),
        "UPDATED_HUMAN": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "CURRENT_PATCH": os.getenv("MLBB_PATCH", ""),
        "DATA_FRESHNESS": data_freshness(),
        "PATCH_WINDOW": patch_window(),
        "role_color": role_color,
        "ROLE_COLORS": ROLE_COLORS,
        "LANE_META": LANE_META,
        "pct": pct,
        "tier_from_winrate": tier_from_winrate,
        "time_ago": time_ago,
        "static_url": static_url,
        "full_url": full_url,
    }


# --------------------------------------------------------------------------- #
# Response headers: Cache-Control + ETag for conditional GETs
# --------------------------------------------------------------------------- #
@app.after_request
def add_perf_headers(response: Response) -> Response:
    """
    Tell browsers (and any CDN / reverse proxy in front of us) how long they
    may reuse responses. Also attaches security/SEO-trust headers that Google
    and modern browsers reward (X-Content-Type-Options, Referrer-Policy, etc.).
    """
    path = request.path or ""

    # ---- Security / trust headers (apply to every response) -------------- #
    # These are lightweight and signal a well-maintained site. Google's Core
    # Web Vitals doesn't score these directly, but they improve E-E-A-T signals,
    # block MIME-sniffing attacks, and prevent clickjacking-style abuses.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    )
    # Only send HSTS when the request came in over HTTPS (avoids breaking dev).
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

    # ---- Cache-Control --------------------------------------------------- #
    if path == "/healthz":
        response.headers["Cache-Control"] = "no-store"
        return response

    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    if path in ("/robots.txt", "/ads.txt", "/manifest.webmanifest") or path.endswith(".xml"):
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    if response.mimetype == "text/html" and response.status_code == 200:
        response.headers["Cache-Control"] = (
            "public, max-age=300, stale-while-revalidate=3600"
        )
        response.headers.setdefault("Vary", "Accept-Encoding")
        try:
            response.add_etag()
            return response.make_conditional(request)
        except Exception:
            return response

    return response


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/patch-notes")
def patch_notes_page() -> str:
    """Auto-generated patch meta digest.

    We don't have week-over-week delta data, but we CAN derive a genuinely
    useful patch report from the live 7-day ranked stats:

      - 'Meta Definers'    : WR >= 54% (SS tier — what's breaking the game)
      - 'Pick Priorities'  : top 5 by pick rate (what the ladder is spamming)
      - 'Ban Priorities'   : top 5 by ban rate (what pros fear)
      - 'Under the Radar'  : high WR + low pick (<= 1%) — secret-OP hidden gems
      - 'Struggling Heroes': bottom 5 WR (nerf-candidates / avoid these)

    Each section is both an SEO landing for queries like 'mlbb patch notes
    tier list', 'most banned mlbb', 'hidden op heroes mlbb', AND a genuinely
    useful patch digest users will want to bookmark.
    """
    tier_data = get_tier_list("all")
    catalog = {h["id"]: h for h in get_all_heroes()}

    def enrich(h: dict) -> dict:
        meta = catalog.get(h["id"]) or {}
        out = dict(h)
        out["slug"] = meta.get("slug") or slugify(h.get("name", ""))
        out["head"] = h.get("head") or meta.get("head")
        out["role"] = meta.get("role")
        out["lanes"] = meta.get("lanes") or []
        return out

    # Meta definers: SS tier (WR >= 54%) — what's breaking the game
    meta_definers = [enrich(h) for h in tier_data if (h.get("win_rate") or 0) >= 0.54][:10]

    # Pick + ban priorities
    by_pick = sorted(
        [enrich(h) for h in tier_data],
        key=lambda h: h.get("pick_rate") or 0,
        reverse=True,
    )[:5]
    by_ban = sorted(
        [enrich(h) for h in tier_data],
        key=lambda h: h.get("ban_rate") or 0,
        reverse=True,
    )[:5]

    # Under the radar: high WR + low pick rate
    under_radar = sorted(
        [
            enrich(h) for h in tier_data
            if (h.get("win_rate") or 0) >= 0.52
            and (h.get("pick_rate") or 1) <= 0.01
        ],
        key=lambda h: h.get("win_rate") or 0,
        reverse=True,
    )[:5]

    # Struggling heroes: bottom WR
    struggling = sorted(
        [enrich(h) for h in tier_data if h.get("win_rate") is not None],
        key=lambda h: h.get("win_rate") or 0,
    )[:5]

    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    patch_tag = os.getenv("MLBB_PATCH", "").strip()
    patch_label = f"Patch {patch_tag}" if patch_tag else f"Live MLBB Meta"

    return render_template(
        "patch_notes.html",
        patch_label=patch_label,
        patch_tag=patch_tag,
        meta_definers=meta_definers,
        by_pick=by_pick,
        by_ban=by_ban,
        under_radar=under_radar,
        struggling=struggling,
        total_heroes=len(tier_data),
        updated=updated,
        page_title=(
            f"MLBB {patch_label} — {current_month} Meta Report, "
            f"Tier Shifts & Ban List | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB patch meta report for {current_month}: "
            f"meta-defining heroes, top picks, ban priorities and "
            f"under-the-radar gems. Auto-refreshed every 6 hours from "
            f"real ranked match data — no guesswork, no outdated tier lists."
        ),
        page_keywords=(
            f"mlbb patch notes {current_month.lower()}, mobile legends patch "
            "meta, mlbb most banned, mlbb hidden op, mlbb meta report, "
            "mlbb patch analysis singapore, mobile legends tier shifts"
        ),
        canonical="/patch-notes",
    )


@app.route("/meta")
def meta_page() -> str:
    all_heroes = get_tier_list("all")
    by_wr = sorted(all_heroes, key=lambda h: h["win_rate"] or 0, reverse=True)[:10]
    by_pick = sorted(all_heroes, key=lambda h: h["pick_rate"] or 0, reverse=True)[:10]
    by_ban = sorted(all_heroes, key=lambda h: h["ban_rate"] or 0, reverse=True)[:10]
    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "meta.html",
        by_wr=by_wr,
        by_pick=by_pick,
        by_ban=by_ban,
        updated=updated,
        page_title=(
            f"MLBB Stats {current_month} — Top Picks, Highest Win "
            f"Rate & Most Banned | {SITE_NAME}"
        ),
        page_desc=(
            f"Current MLBB meta analysis for {current_month} — top-picked, "
            f"highest win rate and most banned heroes this patch. "
            f"Data refreshed every 6 hours from real ranked matches."
        ),
        page_keywords=(
            f"mlbb meta {current_month.lower()}, mobile legends meta, "
            "best picks mlbb, most banned heroes, top win rate mlbb, "
            "mlbb ban list, mlbb s+ tier"
        ),
        canonical="/meta",
    )


@app.route("/topup")
def topup() -> str:
    if not TOPUP_ENABLED:
        abort(404)
    return render_template(
        "topup.html",
        page_title=f"Instant Game Top Up — Cheapest MLBB Diamonds in SG | {SITE_NAME}",
        page_desc="Instant top-up for Mobile Legends diamonds and more. Cheapest prices in Singapore — powered by Singapore Gaming Syndicate.",
        page_keywords="mlbb diamond top up singapore, mobile legends top up, cheap mlbb diamonds, game top up sg",
        canonical="/topup",
        hide_cta_band=True,
    )


@app.route("/topup/mlbb")
def topup_mlbb() -> str:
    if not TOPUP_ENABLED:
        abort(404)
    return render_template(
        "topup_mlbb.html",
        page_title=f"MLBB Diamond Top Up — Instant & Cheapest | {SITE_NAME}",
        page_desc="Top up Mobile Legends diamonds instantly. Cheapest MLBB diamond prices in Singapore — fast, secure, official channels.",
        page_keywords="mlbb diamond top up, mobile legends diamonds singapore, cheap mlbb diamonds, buy mlbb diamonds",
        canonical="/topup/mlbb",
        hide_cta_band=True,
    )


@app.route("/topup/status/<ref>")
def topup_status(ref: str) -> str:
    """HitPay redirect lands here after checkout — shows live order status."""
    if not TOPUP_ENABLED:
        abort(404)
    # Light sanity check on the reference format
    if not re.fullmatch(r"sgs-[a-f0-9]{8,32}", ref):
        abort(404)
    return render_template(
        "topup_status.html",
        ref=ref,
        page_title=f"Order Status — {ref} | {SITE_NAME}",
        page_desc="Your top-up order is being processed.",
        canonical=f"/topup/status/{ref}",
        hide_cta_band=True,
    )


@app.route("/about")
def about_page() -> str:
    return render_template(
        "about.html",
        page_title=f"About {SITE_NAME} — Singapore's #1 MLBB Community",
        page_desc="SGS is Singapore's largest verified Mobile Legends community. Practice, scrim and rank up with real players.",
        page_keywords="singapore gaming syndicate, mlbb community singapore, join mlbb team singapore",
        canonical="/about",
        hide_cta_band=True,
    )


@app.route("/healthz")
def healthz() -> Response:
    """Machine-readable health endpoint.

    Returns HTTP 200 when data is fresh, 503 when we've fallen off the update
    schedule. Lets an uptime-monitor (UptimeRobot, BetterUptime) alert within
    minutes if the upstream API has silently died.
    """
    fresh = data_freshness()
    body = {
        "status": fresh["status"],
        "cache_age_hours": fresh["hours"],
        "last_refresh": fresh["as_of"],
        "stale_threshold_hours": fresh["threshold"],
        "patch": patch_window(),
    }
    code = 200 if fresh["status"] == "fresh" else 503
    return Response(json.dumps(body), mimetype="application/json", status=code)


@app.route("/healthz/run")
def healthz_run() -> Response:
    """Manual trigger for a one-shot health probe (useful for cron + debugging)."""
    result = _health_once()
    return Response(json.dumps(result), mimetype="application/json")


@app.route("/api/facebook-safe-list")
def facebook_safe_list() -> Response:
    """Heroes whose tier ranking is corroborated by enough independent sources
    to publish on social channels (Facebook posts, etc.) without risking a
    contradiction by a community fact-check. Filters tier_list by
    safe_to_publish=True and returns only the high-confidence rows."""
    tier_data = get_tier_list("all")
    safe = [
        {
            "name":              h.get("name"),
            "slug":              h.get("slug"),
            "consensus_tier":    h.get("consensus_tier"),
            "openmlbb_tier":     h.get("tier"),
            "confidence":        h.get("confidence"),
            "win_rate":          h.get("win_rate"),
            "source_tiers":      h.get("source_tiers", {}),
            "data_source_count": h.get("data_source_count", 0),
            "verified_strongly": h.get("verified_strongly", False),
        }
        for h in tier_data
        if h.get("safe_to_publish")
    ]
    safe.sort(key=lambda r: r.get("confidence") or 0, reverse=True)
    body = {
        "count":             len(safe),
        "total":             len(tier_data),
        "data_sources":      ["openmlbb", "mlbbgg", "mlbbhub", "mlbbio"],
        "editorial_sources": ["pocketgamer"],
        "heroes":            safe,
    }
    return Response(json.dumps(body), mimetype="application/json")


# --------------------------------------------------------------------------- #
# IndexNow: instant-indexing protocol (Bing, Yandex, Seznam, Naver).
# --------------------------------------------------------------------------- #
# How it works:
#   1. We generate a hex key and host it at /indexnow-<KEY>.txt (this route).
#   2. We POST new/updated URLs to https://api.indexnow.org/IndexNow along
#      with the key. The target search engines fetch our key file to prove
#      we own the domain, then index the URLs within ~15-90 minutes.
# Google doesn't participate, but Bing ranks #2 globally and feeds DuckDuckGo
# + Ecosia + Yahoo, covering ~10-15% of English-language search traffic.
def submit_indexnow(urls: list[str], search_engine: str = "api.indexnow.org") -> dict[str, Any]:
    """POST a batch of URLs to IndexNow. Returns a result dict.

    Call from a tool / cron / admin route after a content update:

        submit_indexnow(["https://sgs.sg/", "https://sgs.sg/tier-list"])

    IndexNow accepts up to 10,000 URLs per request. All URLs must share the
    host the key file is served from. Standard response: 200 on success,
    202 accepted-pending-validation, 400/403/422 on problems.
    """
    if not INDEXNOW_KEY:
        return {"ok": False, "error": "INDEXNOW_KEY not configured"}
    if not urls:
        return {"ok": False, "error": "no urls given"}
    site_url = (SITE_URL or "").rstrip("/")
    if not site_url or "://" not in site_url:
        return {"ok": False, "error": f"invalid SITE_URL: {site_url!r}"}
    host = site_url.split("://", 1)[1].split("/", 1)[0]

    payload = {
        "host": host,
        "key": INDEXNOW_KEY,
        "keyLocation": f"{site_url}/indexnow-{INDEXNOW_KEY}.txt",
        "urlList": urls[:10_000],
    }
    try:
        with httpx.Client(timeout=15.0, headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "SGS-IndexNow/1.0",
        }) as c:
            r = c.post(f"https://{search_engine}/IndexNow", json=payload)
            ok = r.status_code in (200, 202)
            return {
                "ok": ok,
                "status": r.status_code,
                "submitted": len(payload["urlList"]),
                "engine": search_engine,
                "body": r.text[:500],
            }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.errorhandler(404)
def not_found(_e) -> tuple[str, int]:
    return render_template(
        "404.html",
        page_title=f"404 — Hero Not Found | {SITE_NAME}",
        page_desc="That page doesn't exist. Jump back to the hero list, tier list or current meta.",
        page_keywords="",
        canonical="/",
    ), 404


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
# Kick off the background health monitor at module import so it runs under
# any WSGI server (gunicorn / waitress / flask run / __main__). Disable with
# SGS_HEALTH_INTERVAL_H=0 if you want to opt out (e.g. during tests).
if (os.getenv("SGS_HEALTH_INTERVAL_H", "1") or "0").strip() not in ("0", "", "off", "false"):
    try:
        start_health_monitor()
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to start health monitor: %s", exc)


if __name__ == "__main__":
    try:
        warm_cache()
    except Exception as exc:  # noqa: BLE001
        log.warning("Cache warm failed — will rely on lazy loads. %s", exc)
    app.run(host="0.0.0.0", port=PORT, debug=False)
