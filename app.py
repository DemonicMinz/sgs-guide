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

API_BASE = "https://openmlbb.fastapicloud.dev"
CACHE_SECONDS = 6 * 60 * 60  # 6 hours
REQUEST_TIMEOUT = 20.0
PORT = int(os.getenv("PORT", "8085"))

SITE_NAME = "Singapore Gaming Syndicate"
SITE_SHORT = "SGS"
SITE_TAGLINE = "Where Singapore's Best Gamers Operate"
# SITE_URL: if unset or placeholder, we fall back to the live request host
# (so Cloudflare-tunnel URLs / future real domain just work without code edits).
_SITE_URL_ENV = (os.getenv("SITE_URL") or "").strip().rstrip("/")
_PLACEHOLDER_HOSTS = {"sgs.singapore", "example.com", "localhost", ""}
SITE_URL = _SITE_URL_ENV  # may be "" — see dynamic_site_url() below

TELEGRAM_URL = "https://t.me/SingaporeGamingSyndicate"
GA4_ID = os.getenv("GA4_ID", "")  # placeholder

# Search-engine verification tags (paste the content value from each console).
# Leave blank until you have them; the meta tags just won't render.
GOOGLE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "")
BING_VERIFICATION   = os.getenv("BING_SITE_VERIFICATION", "")
YANDEX_VERIFICATION = os.getenv("YANDEX_SITE_VERIFICATION", "")

# IndexNow key — free instant-indexing protocol for Bing / Yandex / Seznam /
# Naver. Generate with `python tools/generate_indexnow_key.py`. Once set, the
# key is hex-only, verified by a self-hosted file at /indexnow-<KEY>.txt.
INDEXNOW_KEY = (os.getenv("INDEXNOW_KEY") or "").strip()
# Validation below after `log` is initialized — deferred so we don't crash at import.

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
# Cache layer: disk + memory, 6h TTL, graceful fallback when API is down.
# --------------------------------------------------------------------------- #
_memory_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", key)
    return CACHE_DIR / f"{safe}.json"


def _read_disk_cache(key: str) -> tuple[float, Any] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("timestamp", 0), raw.get("data")
    except Exception:
        return None


def _write_disk_cache(key: str, data: Any) -> None:
    path = _cache_path(key)
    payload = {"timestamp": time.time(), "data": data}
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_cache_key(path: str, params: dict[str, Any] | None = None) -> str:
    """Canonical cache key: path + alphabetically-sorted querystring."""
    if not params:
        return path
    return path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Cached GET against the OpenMLBB API with graceful fallback."""
    key = make_cache_key(path, params)
    now = time.time()

    with _cache_lock:
        hit = _memory_cache.get(key)
    if hit and (now - hit[0]) < CACHE_SECONDS:
        return hit[1]

    disk = _read_disk_cache(key)
    if disk and (now - disk[0]) < CACHE_SECONDS:
        with _cache_lock:
            _memory_cache[key] = disk
        return disk[1]

    # Fetch fresh
    url = f"{API_BASE}{path}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": "SGS-MLBB-Guide/1.0"}) as client:
            resp = client.get(url, params=params or {})
            resp.raise_for_status()
            data = resp.json()
        with _cache_lock:
            _memory_cache[key] = (now, data)
        _write_disk_cache(key, data)
        return data
    except Exception as exc:
        log.warning("API fetch failed for %s — %s", key, exc)
        if disk:
            log.info("Serving stale cache for %s", key)
            return disk[1]
        return None


# --------------------------------------------------------------------------- #
# Data shaping helpers — defensive against nested shapes from the API.
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")


def tier_from_winrate(wr: float) -> str:
    """6-tier MLBB meta split based on 7-day win rate.
    SS = meta definers, D = currently weak."""
    if wr is None:
        return "C"
    pct = wr * 100 if wr <= 1 else wr
    if pct >= 54:
        return "SS"
    if pct >= 52:
        return "S"
    if pct >= 50:
        return "A"
    if pct >= 48:
        return "B"
    if pct >= 46:
        return "C"
    return "D"


def classify_counter(counter: dict) -> tuple[str, str]:
    """Given a counter record (with 'win_rate' vs target and 'increase' boost),
    classify how hard the matchup tilts.

    Returns (strength, explain) where strength is one of 'hard' / 'soft' / 'minor'.

    Thresholds derived from MLBB meta sites (Boostroom, ONE Esports, DiamondLobby):
      - Hard counter: the kit fundamentally suppresses the target — +3% or more
        win-rate boost, or ≥54% absolute WR in the matchup.
      - Soft counter: matchup tilt the target can play around with good positioning
        — +1-3% boost, or ≥52% WR.
      - Minor counter: technically an edge but often erased by skill difference.
    """
    try:
        inc = counter.get("increase") or 0
        inc_pct = inc * 100 if abs(inc) <= 1 else inc
    except (TypeError, ValueError):
        inc_pct = 0.0
    try:
        wr = counter.get("win_rate") or 0
        wr_pct = wr * 100 if wr <= 1 else wr
    except (TypeError, ValueError):
        wr_pct = 0.0

    if inc_pct >= 3 or wr_pct >= 54:
        return ("hard",
                "Hard counter — kit fundamentally suppresses this hero.")
    if inc_pct >= 1 or wr_pct >= 52:
        return ("soft",
                "Soft counter — matchup tilts against this hero but is playable.")
    return ("minor",
            "Minor edge — skill gap usually outweighs the matchup.")


def pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "--"
    v = value * 100 if value <= 1 else value
    return f"{v:.{digits}f}%"


def _records(payload: Any) -> list[dict]:
    if not payload:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        return []
    return data.get("records") or []


def parse_hero_list(payload: Any) -> list[dict]:
    """Flatten heroes list payload into a simple list[dict]."""
    out: list[dict] = []
    for rec in _records(payload):
        d = (rec or {}).get("data") or {}
        hero = (d.get("hero") or {}).get("data") or {}
        hid = d.get("hero_id") or hero.get("heroid")
        name = hero.get("name")
        if not (hid and name):
            continue
        roles = [r for r in (hero.get("sortlabel") or []) if r]
        out.append({
            "id": int(hid),
            "name": name,
            "slug": slugify(name),
            "head": hero.get("head"),
            "smallmap": hero.get("smallmap"),
            "role": roles[0] if roles else None,
        })
    # dedupe + sort by name
    seen = set()
    uniq = []
    for h in out:
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        uniq.append(h)
    uniq.sort(key=lambda h: h["name"].lower())
    return uniq


def parse_hero_detail(payload: Any) -> dict | None:
    """Extract usable fields from /api/heroes/{id}."""
    recs = _records(payload)
    if not recs:
        return None
    rec_data = (recs[0] or {}).get("data") or {}
    hero = (rec_data.get("hero") or {}).get("data") or {}

    # Skills
    skills = []
    for s_group in hero.get("heroskilllist") or []:
        for s in (s_group or {}).get("skilllist") or []:
            desc = s.get("skilldesc") or ""
            # strip the HTML-ish font tags for plain text
            desc_plain = re.sub(r"<[^>]+>", "", desc)
            skills.append({
                "id": s.get("skillid"),
                "name": s.get("skillname"),
                "icon": s.get("skillicon"),
                "desc": desc_plain,
                "cd_cost": s.get("skillcd&cost") or s.get("skillcd_cost") or "",
                "tags": [t.get("tagname") for t in (s.get("skilltag") or []) if t.get("tagname")],
            })

    # Roles / lanes
    role_labels = [x for x in (hero.get("sortlabel") or []) if x]
    lane_labels = [x for x in (hero.get("roadsortlabel") or []) if x]
    speciality = hero.get("speciality") or []

    # Difficulty (0-100 in API)
    try:
        difficulty_raw = int(hero.get("difficulty") or 0)
    except (ValueError, TypeError):
        difficulty_raw = 0
    diff_label = "Easy" if difficulty_raw <= 33 else ("Medium" if difficulty_raw <= 66 else "Hard")

    # Relations (strong/weak/assist) with names resolved against the full list later
    relation = rec_data.get("relation") or {}

    return {
        "id": int(rec_data.get("hero_id") or hero.get("heroid") or 0),
        "name": hero.get("name"),
        "slug": slugify(hero.get("name") or ""),
        "head": hero.get("head"),
        "painting": rec_data.get("painting") or hero.get("painting"),
        "head_big": rec_data.get("head_big") or hero.get("head"),
        "story": hero.get("story") or "",
        "tale": hero.get("tale") or "",
        "difficulty": difficulty_raw,
        "difficulty_label": diff_label,
        "roles": role_labels,
        "lanes": lane_labels,
        "speciality": speciality,
        "skills": skills,
        "recommend_level": [int(x) for x in (hero.get("recommendlevel") or []) if str(x).isdigit()],
        "relation_strong_ids": ((relation.get("strong") or {}).get("target_hero_id")) or [],
        "relation_weak_ids":   ((relation.get("weak")   or {}).get("target_hero_id")) or [],
        "relation_assist_ids": ((relation.get("assist") or {}).get("target_hero_id")) or [],
        "relation_strong_desc": ((relation.get("strong") or {}).get("desc")) or "",
        "relation_weak_desc":   ((relation.get("weak")   or {}).get("desc")) or "",
        "relation_assist_desc": ((relation.get("assist") or {}).get("desc")) or "",
    }


def parse_hero_stats(payload: Any) -> dict:
    recs = _records(payload)
    if not recs:
        return {}
    d = (recs[0] or {}).get("data") or {}
    return {
        "win_rate": d.get("main_hero_win_rate"),
        "pick_rate": d.get("main_hero_appearance_rate"),
        "ban_rate": d.get("main_hero_ban_rate"),
    }


def parse_sub_hero_list(payload: Any) -> list[dict]:
    """For /counters and /compatibility — returns list[{heroid, win_rate, increase_win_rate}]."""
    recs = _records(payload)
    if not recs:
        return []
    d = (recs[0] or {}).get("data") or {}
    out = []
    for sh in d.get("sub_hero") or []:
        out.append({
            "id": sh.get("heroid"),
            "win_rate": sh.get("hero_win_rate"),
            "increase": sh.get("increase_win_rate"),
            "head": ((sh.get("hero") or {}).get("data") or {}).get("head"),
        })
    return out


def parse_skill_combos(payload: Any) -> list[dict]:
    recs = _records(payload)
    out = []
    for r in recs:
        d = (r or {}).get("data") or {}
        icons = []
        for s in d.get("skill_id") or []:
            sd = (s or {}).get("data") or {}
            if sd.get("skillicon"):
                icons.append(sd["skillicon"])
        out.append({
            "desc": d.get("desc") or "",
            "icons": icons,
            "title": (r.get("caption") or "Combo").split("-", 1)[-1],
        })
    return out


def parse_academy_builds(payload: Any, hero_id: int) -> list[dict]:
    """Filter academy recommended builds to this hero, sorted by popularity.
    Each build is enriched with resolved item/emblem/spell icons + names via
    the /api/academy/equipment, /emblems and /spells catalogues."""
    recs = _records(payload)
    equip_map  = get_equipment_map()
    emblem_map = get_emblem_map()
    spell_map  = get_spell_map()

    builds = []
    for r in recs:
        data_outer = (r or {}).get("data") or {}
        data_inner = data_outer.get("data") or {}
        dynamic    = (r or {}).get("dynamic") or {}
        hero = data_inner.get("hero") or {}
        if int(hero.get("hero_id") or -1) != int(hero_id):
            continue
        created_ms = r.get("createdAt") or 0

        # Resolve each item slot → {id, name, icon}.
        equips_raw = data_inner.get("equips") or []
        equips = []
        for e in equips_raw:
            resolved = []
            for eid in (e.get("equip_ids") or []):
                info = equip_map.get(int(eid)) if eid is not None else None
                resolved.append({
                    "id": eid,
                    "name": (info or {}).get("name") or f"Item #{eid}",
                    "icon": (info or {}).get("icon"),
                })
            equips.append({
                "title": e.get("equip_title") or "",
                "desc":  e.get("equip_desc") or "",
                "slots": resolved,
            })

        # Resolve emblem talents (gift IDs → talent name + icon).
        emblems_raw = data_inner.get("emblems") or []
        emblems = []
        for em in emblems_raw:
            gifts = []
            for gid in (em.get("emblem_gifts") or []):
                info = emblem_map.get(int(gid)) if gid is not None else None
                gifts.append({
                    "id": gid,
                    "name": (info or {}).get("name") or f"Talent #{gid}",
                    "icon": (info or {}).get("icon"),
                    "tier": (info or {}).get("tier"),
                })
            emblems.append({
                "title": em.get("emblem_title") or "",
                "desc":  em.get("emblem_desc") or "",
                "emblem_id": em.get("emblem_id"),
                "gifts": gifts,
            })

        # Resolve the single battle spell.
        spell_obj = data_inner.get("spell") or {}
        sp_id = spell_obj.get("spell_id")
        sp_info = spell_map.get(int(sp_id)) if sp_id else None
        spell = {
            "id": sp_id,
            "name": (sp_info or {}).get("name") or (f"Spell #{sp_id}" if sp_id else None),
            "icon": (sp_info or {}).get("icon"),
            "shortdesc": (sp_info or {}).get("shortdesc"),
            "desc": spell_obj.get("spell_desc") or "",
        }

        builds.append({
            "title": data_inner.get("title") or "Recommended Build",
            "snapshot": data_inner.get("snapshot"),
            "overview": hero.get("hero_overview") or "",
            "strength": hero.get("hero_strength") or "",
            "weakness": hero.get("hero_weakness") or "",
            "recommend": data_inner.get("recommend") or "",
            "equips": equips,
            "emblems": emblems,
            "spell": spell,
            "game_version": data_inner.get("game_version"),
            "views": int(dynamic.get("views") or 0),
            "hot": float(dynamic.get("hot") or 0),
            "created_ms": int(created_ms),
        })
    # Most popular first — sort by hot score then views.
    builds.sort(key=lambda b: (b["hot"], b["views"]), reverse=True)
    return builds[:10]


# --------------------------------------------------------------------------- #
# Equipment / emblem / spell catalogues — cached 6h like everything else.
# Keyed by their in-game IDs so academy build IDs can be resolved to
# (name, icon URL) for the build cards.
# --------------------------------------------------------------------------- #
def _records_safe(payload: Any) -> list:
    try:
        return _records(payload)
    except Exception:  # noqa: BLE001
        return []


def get_equipment_map() -> dict[int, dict]:
    """Returns {equip_id: {name, icon, skills?}}. Each record's `data` has
    `equipid`, `equipname`, `equipicon`. Uses the *expanded* endpoint first
    (richer data), falling back to the plain one."""
    payload = api_get("/api/academy/equipment/expanded", {"size": 500})
    items = _records_safe(payload)
    if not items:
        payload = api_get("/api/academy/equipment", {"size": 500})
        items = _records_safe(payload)

    out: dict[int, dict] = {}
    for rec in items:
        d = ((rec or {}).get("data") or {})
        eid = d.get("equipid")
        if eid is None:
            continue
        out[int(eid)] = {
            "name": d.get("equipname"),
            "icon": d.get("equipicon"),
            "skills": [v for k, v in d.items()
                       if k.startswith("equipskill") and v],
        }
    return out


def get_emblem_map() -> dict[int, dict]:
    """Returns {gift_id: {name, icon, tier, desc}}. An academy build references
    emblem talents via `emblem_gifts` (a list of giftids)."""
    payload = api_get("/api/academy/emblems", {"size": 500})
    items = _records_safe(payload)
    out: dict[int, dict] = {}
    for rec in items:
        d = ((rec or {}).get("data") or {})
        gid = d.get("giftid")
        if gid is None:
            continue
        sk = d.get("emblemskill") or {}
        out[int(gid)] = {
            "name": sk.get("skillname"),
            "icon": sk.get("skillicon"),
            "tier": d.get("gifttiers"),
            "desc": sk.get("skilldesc") or sk.get("skilldescemblem"),
        }
    return out


def get_spell_map() -> dict[int, dict]:
    """Returns {spell_id: {name, icon, shortdesc, desc}}."""
    payload = api_get("/api/academy/spells", {"size": 200})
    items = _records_safe(payload)
    out: dict[int, dict] = {}
    for rec in items:
        d = ((rec or {}).get("data") or {})
        sid = d.get("battleskillid")
        if sid is None:
            continue
        inner = d.get("__data") or {}
        out[int(sid)] = {
            "name": inner.get("skillname") or d.get("skillname"),
            "icon": inner.get("skillicon") or d.get("skillicon"),
            "shortdesc": d.get("skillshortdesc"),
            "desc": inner.get("skilldesc"),
        }
    return out


def parse_tier_ranking(payload: Any) -> list[dict]:
    """From /api/heroes/rank — returns list of heroes with stats sorted by win rate."""
    recs = _records(payload)
    out = []
    for r in recs:
        d = (r or {}).get("data") or {}
        main = (d.get("main_hero") or {}).get("data") or {}
        if not main.get("name"):
            continue
        wr = d.get("main_hero_win_rate")
        out.append({
            "id": d.get("main_heroid"),
            "name": main.get("name"),
            "slug": slugify(main.get("name") or ""),
            "head": main.get("head"),
            "win_rate": wr,
            "pick_rate": d.get("main_hero_appearance_rate"),
            "ban_rate": d.get("main_hero_ban_rate"),
            "tier": tier_from_winrate(wr),
        })
    return out


# --------------------------------------------------------------------------- #
# High-level data accessors
# --------------------------------------------------------------------------- #
# In-process memo for the enriched hero list, keyed by underlying payload ids.
# The upstream payload is already disk+memory cached for 6h via api_get, so
# this only rebuilds when a fresh /api/heroes pull drops us a new hero set.
_HEROES_ENRICHED_LOCK = threading.Lock()
_HEROES_ENRICHED_CACHE: dict[str, Any] = {"key": None, "data": []}


def get_all_heroes() -> list[dict]:
    """Return the full hero catalogue enriched with role + lane fields.

    The OpenMLBB list endpoint (/api/heroes) strips `sortlabel` and
    `roadsortlabel` on heroes (as of April 2026), so role information must
    come from the per-hero detail endpoint. We fan out those detail fetches
    in parallel once per 6h cache cycle — the results are served from disk
    after the first warm, so runtime cost is negligible.
    """
    payload = api_get("/api/heroes", {"size": 200})
    base = parse_hero_list(payload)

    ids = tuple(sorted(h["id"] for h in base))
    cache_key = f"{len(ids)}:{hash(ids)}"
    with _HEROES_ENRICHED_LOCK:
        if _HEROES_ENRICHED_CACHE.get("key") == cache_key and _HEROES_ENRICHED_CACHE.get("data"):
            return _HEROES_ENRICHED_CACHE["data"]  # type: ignore[return-value]

    # Parallel fan-out: each api_get is cached, so the second time through
    # this is just 132 disk reads (~<1s total on an SSD).
    details: dict[int, dict] = {}
    futs = {_EXECUTOR.submit(api_get, f"/api/heroes/{h['id']}"): h["id"] for h in base}
    for fut in futs:
        hid = futs[fut]
        try:
            det = parse_hero_detail(fut.result(timeout=REQUEST_TIMEOUT * 2))
            if det:
                details[hid] = det
        except Exception:  # noqa: BLE001
            # If a detail fetch fails we leave role=None for that hero — the
            # role landing page will simply skip them until next warm.
            pass

    enriched: list[dict] = []
    for h in base:
        det = details.get(h["id"])
        if det:
            roles = det.get("roles") or []
            lanes = det.get("lanes") or []
            h["role"]  = roles[0] if roles else None
            h["roles"] = roles
            h["lanes"] = lanes
        enriched.append(h)

    with _HEROES_ENRICHED_LOCK:
        _HEROES_ENRICHED_CACHE["key"] = cache_key
        _HEROES_ENRICHED_CACHE["data"] = enriched
    return enriched


def get_hero_detail(hero_id: int) -> dict | None:
    payload = api_get(f"/api/heroes/{hero_id}")
    return parse_hero_detail(payload)


def get_hero_stats(hero_id: int) -> dict:
    payload = api_get(f"/api/heroes/{hero_id}/stats", {"rank": "all"})
    return parse_hero_stats(payload)


def get_hero_counters(hero_id: int) -> list[dict]:
    payload = api_get(f"/api/heroes/{hero_id}/counters", {"days": 7, "rank": "all"})
    return parse_sub_hero_list(payload)


def get_hero_compat(hero_id: int) -> list[dict]:
    payload = api_get(f"/api/heroes/{hero_id}/compatibility", {"days": 7, "rank": "all"})
    return parse_sub_hero_list(payload)


def get_hero_combos(hero_id: int) -> list[dict]:
    payload = api_get(f"/api/heroes/{hero_id}/skill-combos")
    return parse_skill_combos(payload)


def get_academy_builds(hero_id: int) -> list[dict]:
    """Per-hero recommended builds — returns up to 50 builds for this hero,
    vs. ~2 from the global /api/academy/recommended endpoint."""
    payload = api_get(f"/api/academy/heroes/{hero_id}/recommended", {"size": 50})
    return parse_academy_builds(payload, hero_id)


def get_tier_list(rank: str = "all") -> list[dict]:
    # NB: default page size on this endpoint is 20, we need 200 to get all ~132 heroes.
    payload = api_get(
        "/api/heroes/rank",
        {"rank": rank, "days": 7, "sort_field": "win_rate", "size": 200},
    )
    heroes = parse_tier_ranking(payload)
    heroes.sort(key=lambda h: (h["win_rate"] or 0), reverse=True)
    return heroes


def hero_index_by_id() -> dict[int, dict]:
    return {h["id"]: h for h in get_all_heroes()}


# --------------------------------------------------------------------------- #
# Role/lane inference
# --------------------------------------------------------------------------- #
def primary_role(hero_detail: dict) -> str:
    roles = hero_detail.get("roles") or []
    if roles:
        return roles[0]
    return "Fighter"


def role_color(role: str) -> str:
    return ROLE_COLORS.get(role, "#FFD700")


# --------------------------------------------------------------------------- #
# Shared thread pool used for parallel API fan-out (hero page cold loads,
# warm_cache background fills). Small pool — the upstream API is the bottleneck,
# not CPU. 8 workers is enough to overlap all the small per-hero endpoints.
# --------------------------------------------------------------------------- #
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sgs-fetch")


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
def cache_age_text(path_key: str) -> str:
    disk = _read_disk_cache(path_key)
    if not disk:
        return "just now"
    age = max(0, int(time.time() - disk[0]))
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{age // 60} minutes ago"
    hours = age // 3600
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


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
@app.route("/")
def index() -> str:
    heroes = get_all_heroes()
    tier_list = get_tier_list("all")
    # Enrich heroes with win rate from tier list where available
    wr_by_id = {h["id"]: h for h in tier_list}
    for h in heroes:
        wr = wr_by_id.get(h["id"])
        if wr:
            h["win_rate"] = wr["win_rate"]
            h["tier"] = wr["tier"]
    top_meta = tier_list[:10]

    # "Trending this patch" driven by ban rate — the single strongest market
    # signal for which heroes are considered overpowered. Research (April 2026)
    # shows Gloo, Julian, Kalea etc. with 60-80% ban rates in Mythic Glory.
    # Top-ranked MLBB guide sites (BitTopup, mlbbmeta.com) lead with this.
    catalog = {h["id"]: h for h in heroes}
    trending = sorted(
        [h for h in tier_list if h.get("ban_rate") is not None],
        key=lambda h: h.get("ban_rate") or 0,
        reverse=True,
    )[:6]
    for t in trending:
        meta = catalog.get(t["id"])
        if meta:
            t["role"] = meta.get("role")

    updated = cache_age_text("/api/heroes?size=200")
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "index.html",
        heroes=heroes,
        top_meta=top_meta,
        trending=trending,
        updated=updated,
        page_title=(
            f"MLBB Tier List {current_month} — Live Hero Guides & "
            f"Best Builds | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB tier list for {current_month}: {len(heroes)} heroes "
            f"ranked by real ranked win rate, with builds, counters & combos. "
            f"Singapore's #1 Mobile Legends community — join on Telegram."
        ),
        page_keywords=(
            f"mlbb tier list {current_month.lower()}, mobile legends tier "
            "list, best mlbb heroes, mlbb meta 2026, mobile legends singapore, "
            "hero build 2026, mlbb counter picks"
        ),
        canonical="/",
    )


@app.route("/hero/<slug>")
def hero_page(slug: str) -> str:
    heroes = get_all_heroes()
    hero = next((h for h in heroes if h["slug"] == slug), None)
    if not hero:
        abort(404)

    # Fan-out: all 6 downstream endpoints in parallel. On a cold cache this
    # drops first-view latency from ~sum(requests) to ~max(requests) — typically
    # 5x speed-up against the OpenMLBB API.
    hid = hero["id"]
    f_detail    = _EXECUTOR.submit(get_hero_detail,   hid)
    f_stats     = _EXECUTOR.submit(get_hero_stats,    hid)
    f_counters  = _EXECUTOR.submit(get_hero_counters, hid)
    f_synergies = _EXECUTOR.submit(get_hero_compat,   hid)
    f_combos    = _EXECUTOR.submit(get_hero_combos,   hid)
    f_builds    = _EXECUTOR.submit(get_academy_builds, hid)

    detail = f_detail.result()
    if not detail:
        abort(502)
    stats     = f_stats.result()     or {}
    counters  = (f_counters.result()  or [])[:3]
    synergies = (f_synergies.result() or [])[:3]
    combos    = f_combos.result()    or []
    builds    = f_builds.result()    or []

    # Resolve related hero names/slugs
    by_id = {h["id"]: h for h in heroes}
    def resolve(ids: list[int]) -> list[dict]:
        return [by_id[i] for i in ids if i in by_id][:3]

    strong_against = resolve(detail.get("relation_strong_ids") or [])
    weak_against = resolve(detail.get("relation_weak_ids") or [])
    works_with = resolve(detail.get("relation_assist_ids") or [])

    # Enrich counters/synergies with names
    for lst in (counters, synergies):
        for item in lst:
            meta = by_id.get(item.get("id"))
            if meta:
                item["name"] = meta["name"]
                item["slug"] = meta["slug"]
                item["head"] = item.get("head") or meta.get("head")
            else:
                item["name"] = f"Hero #{item.get('id')}"
                item["slug"] = ""

    # Tag each counter with hard/soft/minor classification — this is the single
    # strongest SEO/UX signal for counter guides (Boostroom/ONE Esports use it).
    for c in counters:
        c["strength"], c["strength_explain"] = classify_counter(c)
    counter_tally = {
        "hard":  sum(1 for c in counters if c.get("strength") == "hard"),
        "soft":  sum(1 for c in counters if c.get("strength") == "soft"),
        "minor": sum(1 for c in counters if c.get("strength") == "minor"),
    }

    role = primary_role(detail)
    tier = tier_from_winrate(stats.get("win_rate") or 0)
    tips = hero_tips(role, detail["name"])
    wr_pct = pct(stats.get("win_rate"))
    pr_pct = pct(stats.get("pick_rate"))
    br_pct = pct(stats.get("ban_rate"))

    name = detail["name"]
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    page_title = (
        f"{name} Guide {current_month} — Best Build, Counter & "
        f"Combo | SGS Singapore"
    )
    page_desc = (
        f"{name} MLBB guide ({current_month}): best build, emblem, spell, "
        f"combos, counters and pro tips. Live {wr_pct} win rate from real "
        f"ranked matches. Join Singapore's #1 Mobile Legends community on "
        f"Telegram."
    )
    page_keywords = ", ".join([
        f"{name} guide", f"{name} build {current_month.lower()}",
        f"{name} mlbb", f"how to play {name}",
        f"{name} combo", f"{name} tips singapore",
        f"{name} counter", f"best {name} build", f"{name} emblem",
        f"mobile legends {name}",
    ])

    updated = cache_age_text(f"/api/heroes/{hero['id']}/stats?rank=all")

    faqs = build_hero_faqs(
        name=name,
        role=role,
        tier=tier,
        wr_pct=wr_pct,
        counters=counters,
        synergies=synergies,
        difficulty=detail.get("difficulty_label"),
        lanes=detail.get("lanes") or [],
    )

    return render_template(
        "hero.html",
        hero=hero,
        detail=detail,
        stats=stats,
        counters=counters,
        counter_tally=counter_tally,
        synergies=synergies,
        combos=combos,
        builds=builds,
        strong_against=strong_against,
        weak_against=weak_against,
        works_with=works_with,
        role=role,
        tier=tier,
        tips=tips,
        wr_pct=wr_pct,
        pr_pct=pr_pct,
        br_pct=br_pct,
        updated=updated,
        faqs=faqs,
        page_title=page_title,
        page_desc=page_desc,
        page_keywords=page_keywords,
        canonical=f"/hero/{slug}",
        hide_cta_band=True,
    )


@app.route("/role/<role>")
def role_page(role: str) -> str:
    """Role landing page — keyword-rich index of every hero in a class.
    Targets queries like 'best assassins mlbb 2026', 'top tank heroes', etc.
    """
    role_norm = role.strip().lower()
    canonical_name = next(
        (r for r in ROLE_COLORS if r.lower() == role_norm), None
    )
    if not canonical_name:
        abort(404)

    all_heroes = get_tier_list("all")
    # get_all_heroes() gives us role+lane; tier list gives us win rate. Merge.
    catalog = {h["id"]: h for h in get_all_heroes()}
    filtered: list[dict] = []
    for h in all_heroes:
        meta = catalog.get(h["id"])
        if not meta:
            continue
        if (meta.get("role") or "").lower() == role_norm:
            merged = dict(meta)
            merged.update({
                "win_rate":  h.get("win_rate"),
                "pick_rate": h.get("pick_rate"),
                "ban_rate":  h.get("ban_rate"),
                "tier":      h.get("tier"),
            })
            filtered.append(merged)
    filtered.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)

    top10 = filtered[:10]
    updated = cache_age_text("/api/heroes?size=200")

    tier_buckets: dict[str, list[dict]] = {k: [] for k in ("SS", "S", "A", "B", "C", "D")}
    for h in filtered:
        tier_buckets.setdefault(h.get("tier") or "C", []).append(h)

    # 3-hero pool recommendation — the "comfort / counter / safe blind"
    # framework used by every high-ranking MLBB guide site (Boostroom,
    # ONE Esports). Gives users an actionable takeaway and packs in
    # long-tail keywords like "best blind pick tank mlbb".
    hero_pool = build_hero_pool(filtered)

    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "role.html",
        role=canonical_name,
        role_color=role_color(canonical_name),
        intro=ROLE_INTRO.get(role_norm, ""),
        heroes=filtered,
        top10=top10,
        tier_buckets=tier_buckets,
        hero_pool=hero_pool,
        updated=updated,
        page_title=(
            f"Best {canonical_name}s in MLBB {current_month} — Live Tier "
            f"List, Builds & Counters | SGS"
        ),
        page_desc=(
            f"Every Mobile Legends {canonical_name.lower()} hero ranked by "
            f"live win rate ({current_month}) — {len(filtered)} heroes, "
            f"updated every 6 hours from real ranked data. Find the best "
            f"{canonical_name.lower()} to climb to Mythic in 2026."
        ),
        page_keywords=(
            f"best {canonical_name.lower()} mlbb {current_month.lower()}, "
            f"top {canonical_name.lower()} mobile legends, "
            f"{canonical_name.lower()} tier list, mlbb "
            f"{canonical_name.lower()} guide singapore, best blind pick "
            f"{canonical_name.lower()}"
        ),
        canonical=f"/role/{role_norm}",
    )


@app.route("/lane/<lane>")
def lane_page(lane: str) -> str:
    """Lane landing page — targets queries like 'best jungler mlbb',
    'best mid laner mobile legends 2026', 'exp lane tier list'. Lanes cut
    *across* roles (a jungler might be Assassin, Fighter or even Tank), so
    this is a genuinely different axis of navigation from /role/*.
    """
    slug = (lane or "").strip().lower()
    meta = LANE_META.get(slug)
    if not meta:
        abort(404)

    canonical_name = meta["canonical"]  # "Jungle" / "Mid Lane" / …
    label = meta["label"]                # "Junglers" / "Mid-laner" / …

    tier_data = get_tier_list("all")
    catalog = {h["id"]: h for h in get_all_heroes()}

    filtered: list[dict] = []
    for h in tier_data:
        hero_meta = catalog.get(h["id"])
        if not hero_meta:
            continue
        hero_lanes = hero_meta.get("lanes") or []
        if canonical_name not in hero_lanes:
            continue
        merged = dict(hero_meta)
        merged.update({
            "win_rate":  h.get("win_rate"),
            "pick_rate": h.get("pick_rate"),
            "ban_rate":  h.get("ban_rate"),
            "tier":      h.get("tier"),
        })
        filtered.append(merged)
    filtered.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)

    top10 = filtered[:10]
    tier_buckets: dict[str, list[dict]] = {k: [] for k in ("SS", "S", "A", "B", "C", "D")}
    for h in filtered:
        tier_buckets.setdefault(h.get("tier") or "C", []).append(h)

    # Role breakdown — "Jungle is 60% assassins, 30% fighters, 10% tanks"
    # is exactly the kind of snippet that wins long-tail roles-per-lane searches.
    role_counts: dict[str, int] = {}
    for h in filtered:
        r = h.get("role") or "Unknown"
        role_counts[r] = role_counts.get(r, 0) + 1
    role_breakdown = sorted(role_counts.items(), key=lambda kv: -kv[1])

    # Same 3-hero pool framework as /role/* — works for lanes too.
    hero_pool = build_hero_pool(filtered)

    updated = cache_age_text("/api/heroes?size=200")
    current_month = datetime.now(timezone.utc).strftime("%B %Y")

    return render_template(
        "lane.html",
        lane_slug=slug,
        lane_name=canonical_name,
        lane_label=label,
        lane_color=meta["color"],
        intro=meta["intro"],
        tips=meta["tips"],
        heroes=filtered,
        top10=top10,
        tier_buckets=tier_buckets,
        role_breakdown=role_breakdown,
        hero_pool=hero_pool,
        updated=updated,
        page_title=(
            f"Best {label}s in MLBB {current_month} — "
            f"{canonical_name} Tier List, Builds & Tips | SGS"
        ),
        page_desc=(
            f"Every Mobile Legends {canonical_name.lower()} hero ranked "
            f"by live win rate ({current_month}) — {len(filtered)} heroes, "
            f"updated every 6 hours from real ranked data. Find the best "
            f"{canonical_name.lower()} pick to climb in 2026."
        ),
        page_keywords=(
            f"best {canonical_name.lower()} mlbb {current_month.lower()}, "
            f"top {label.lower()}s mobile legends, "
            f"{canonical_name.lower()} tier list, mlbb "
            f"{canonical_name.lower()} heroes, best {label.lower()} to climb, "
            f"{canonical_name.lower()} meta {current_month.lower()}"
        ),
        canonical=f"/lane/{slug}",
    )


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


@app.route("/tier-list")
def tier_list_page() -> str:
    rank = request.args.get("rank", "all").lower()
    allowed = {"all", "epic", "legend", "mythic", "honor", "glory"}
    if rank not in allowed:
        rank = "all"
    heroes = get_tier_list(rank)

    buckets = {"SS": [], "S": [], "A": [], "B": [], "C": [], "D": []}
    for h in heroes:
        buckets[h["tier"]].append(h)

    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": rank, "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "tier_list.html",
        rank=rank,
        buckets=buckets,
        total=len(heroes),
        updated=updated,
        page_title=(
            f"MLBB Tier List {current_month} — Live Win Rates, "
            f"SS–D Ranked Heroes | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB tier list for {current_month}: {len(heroes)} heroes "
            f"ranked SS through D by real ranked win rate. Updated every 6 "
            f"hours from the latest Epic–Mythic Glory games. See the best "
            f"Mobile Legends heroes right now."
        ),
        page_keywords=(
            f"mlbb tier list {current_month.lower()}, mobile legends tier "
            f"list, best heroes mlbb, mlbb meta singapore, "
            f"{rank} tier list mlbb, ss tier mlbb, mlbb patch tier list"
        ),
        # Rank-specific canonical — treats each ?rank=… as a distinct page for
        # Google, so queries like "mlbb mythic tier list" resolve to the
        # mythic view rather than being merged into the default "all ranks".
        canonical=("/tier-list" if rank == "all" else f"/tier-list?rank={rank}"),
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
            f"MLBB Meta {current_month} — Top Picks, Highest Win "
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


@app.route("/sitemap.xml")
def sitemap() -> Response:
    """Root sitemap index — points to per-type sub-sitemaps."""
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name in ("sitemap-core.xml", "sitemap-heroes.xml",
                 "sitemap-roles.xml", "sitemap-lanes.xml", "sitemap-images.xml"):
        parts.append(
            f"<sitemap><loc>{base}/{name}</loc><lastmod>{lastmod}</lastmod></sitemap>"
        )
    parts.append("</sitemapindex>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/sitemap-core.xml")
def sitemap_core() -> Response:
    """Static top-level pages."""
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    urls = [
        ("", "1.0", "daily"),
        ("/tier-list", "0.9", "daily"),
        # Rank-bracket tier-list variants — exposed so Google can index the
        # long-tail queries like "mlbb mythic tier list" separately from the
        # "all ranks" default. Each is canonical to itself via ?rank=… query.
        ("/tier-list?rank=mythic", "0.8", "daily"),
        ("/tier-list?rank=glory",  "0.8", "daily"),
        ("/tier-list?rank=legend", "0.7", "daily"),
        ("/tier-list?rank=epic",   "0.7", "daily"),
        ("/meta", "0.9", "daily"),
        ("/patch-notes", "0.9", "daily"),
        ("/about", "0.5", "monthly"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, prio, freq in urls:
        parts.append(
            f"<url><loc>{base}{path}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/sitemap-heroes.xml")
def sitemap_heroes() -> Response:
    """One entry per hero guide page."""
    base = dynamic_site_url()
    heroes = get_all_heroes()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for h in heroes:
        parts.append(
            f"<url><loc>{base}/hero/{h['slug']}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.8</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/sitemap-roles.xml")
def sitemap_roles() -> Response:
    """Role landing pages (one per MLBB class)."""
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for role in ROLE_COLORS.keys():
        parts.append(
            f"<url><loc>{base}/role/{role.lower()}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/sitemap-lanes.xml")
def sitemap_lanes() -> Response:
    """Lane landing pages (one per MLBB lane: jungle/mid/exp/gold/roam)."""
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for slug in LANE_META.keys():
        parts.append(
            f"<url><loc>{base}/lane/{slug}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/sitemap-images.xml")
def sitemap_images() -> Response:
    """Image sitemap — helps Google Images index hero portraits."""
    base = dynamic_site_url()
    heroes = get_all_heroes()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
             '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    for h in heroes:
        parts.append(f'<url><loc>{base}/hero/{h["slug"]}</loc>')
        img = h.get("head_big") or h.get("head")
        if img:
            caption = f"{h['name']} MLBB hero portrait"
            parts.append(
                "<image:image>"
                f"<image:loc>{img}</image:loc>"
                f"<image:title>{h['name']}</image:title>"
                f"<image:caption>{caption}</image:caption>"
                "</image:image>"
            )
        parts.append("</url>")
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@app.route("/robots.txt")
def robots() -> Response:
    """SEO-friendly robots.txt:
    - Allow all good crawlers.
    - Block LLM training bots that don't give anything back (set aside, don't
      hurt SEO; these are AI scrapers, NOT search engines).
    - Point to sitemap-index at the top (Google reads Sitemap directives).
    """
    base = dynamic_site_url()
    lines = [
        "# SGS MLBB Guide — robots.txt",
        "",
        "# Welcome, search engines.",
        "User-agent: Googlebot",
        "Allow: /",
        "",
        "User-agent: Bingbot",
        "Allow: /",
        "",
        "User-agent: DuckDuckBot",
        "Allow: /",
        "",
        "User-agent: Slurp",
        "Allow: /",
        "",
        "User-agent: Baiduspider",
        "Allow: /",
        "",
        "User-agent: YandexBot",
        "Allow: /",
        "",
        "# Default: open to any well-behaved crawler.",
        "User-agent: *",
        "Allow: /",
        "Disallow: /healthz",
        "Disallow: /*?*",  # stop crawlers expanding every query-string permutation
        "Crawl-delay: 1",
        "",
        "# Block AI training scrapers (they don't drive traffic back).",
        "User-agent: GPTBot",
        "Disallow: /",
        "User-agent: ChatGPT-User",
        "Disallow: /",
        "User-agent: CCBot",
        "Disallow: /",
        "User-agent: anthropic-ai",
        "Disallow: /",
        "User-agent: Claude-Web",
        "Disallow: /",
        "User-agent: Google-Extended",
        "Disallow: /",
        "User-agent: FacebookBot",
        "Disallow: /",
        "",
        f"Sitemap: {base}/sitemap.xml",
        f"Sitemap: {base}/sitemap-core.xml",
        f"Sitemap: {base}/sitemap-heroes.xml",
        f"Sitemap: {base}/sitemap-roles.xml",
        f"Sitemap: {base}/sitemap-lanes.xml",
        f"Sitemap: {base}/sitemap-images.xml",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/ads.txt")
def ads_txt() -> Response:
    # Authorized Digital Sellers — declares Google AdSense as a direct seller.
    return Response(
        "google.com, pub-3287033837149583, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain",
    )


@app.route("/manifest.webmanifest")
def manifest() -> Response:
    """PWA manifest — tells Android to enable Add-to-Homescreen and signals
    to Google that this is a fully-fledged mobile-first web app."""
    base = dynamic_site_url()
    body = {
        "name": SITE_NAME,
        "short_name": SITE_SHORT,
        "description": (
            "Live MLBB hero tier list, builds and guides. "
            "Singapore's #1 Mobile Legends community."
        ),
        "start_url": "/?utm_source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0a0a0f",
        "theme_color": "#0a0a0f",
        "lang": LANG_TAG,
        "categories": ["games", "sports", "entertainment"],
        "icons": [
            {"src": f"{base}/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": f"{base}/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(body), mimetype="application/manifest+json")


@app.route("/favicon.ico")
def favicon() -> Response:
    """Serve a favicon so browsers/crawlers stop 404-ing on /favicon.ico.
    Falls back to the SVG brand mark if no .ico exists yet."""
    static = BASE_DIR / "static"
    for name in ("favicon.ico", "favicon.svg", "icon-192.png"):
        p = static / name
        if p.exists():
            return send_from_directory(str(static), name)
    # Minimal 1x1 transparent PNG if nothing set up yet.
    return Response(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
        mimetype="image/png",
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
@app.route("/indexnow-<key>.txt")
def indexnow_keyfile(key: str) -> Response:
    """Serve the IndexNow ownership proof file. Only responds for the
    currently-configured key; returns 404 for any other path so attackers
    can't enumerate keys."""
    if not INDEXNOW_KEY or key != INDEXNOW_KEY:
        return Response("Not found", status=404, mimetype="text/plain")
    return Response(INDEXNOW_KEY, mimetype="text/plain")


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
