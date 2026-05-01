"""OpenMLBB API client — caching layer + parsers + high-level accessors.

Sprint 4 / Phase A extraction. Owns:
  * Disk + memory cache primitives (_cache_path, _read_disk_cache, ...).
  * The cached HTTP wrapper api_get().
  * Defensive payload parsers (parse_hero_list, parse_hero_detail, ...).
  * High-level accessors callers actually use (get_all_heroes,
    get_hero_detail, get_tier_list, ...).
  * The shared thread pool _EXECUTOR used for parallel hero fan-out.

App.py imports from here; nothing here imports from app.py — that
constraint keeps the dependency graph acyclic and lets blueprint
extraction in a later sprint slot in cleanly.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import config
from crosscheck import run_crosscheck

# --------------------------------------------------------------------------- #
# Module-level configuration
# --------------------------------------------------------------------------- #
API_BASE = config.API_BASE
CACHE_SECONDS = config.CACHE_SECONDS
REQUEST_TIMEOUT = config.REQUEST_TIMEOUT
CACHE_DIR: Path = config.CACHE_DIR
CACHE_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("sgs")  # share app.py's logger to keep log output stable

_USER_AGENT = "SGS-MLBB-Guide/1.0"


# --------------------------------------------------------------------------- #
# Cache primitives
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
    except Exception:  # noqa: BLE001
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


def cache_modified_iso(cache_key: str) -> str | None:
    """ISO timestamp (UTC) of when a cache file was last written, or None.

    Used by sitemap-heroes.xml to emit per-hero <lastmod> values, so Google
    only re-crawls heroes whose stats actually changed instead of re-fetching
    every page on every sitemap visit.
    """
    try:
        path = _cache_path(cache_key)
        if not path.exists():
            return None
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# HTTP wrapper
# --------------------------------------------------------------------------- #
def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Cached GET against the OpenMLBB API with graceful fallback.

    Lookup order:
      1. In-memory cache, if entry < CACHE_SECONDS old.
      2. Disk cache, if entry < CACHE_SECONDS old.
      3. Live HTTP request — on success, repopulates both caches.
      4. Stale disk cache (any age) — on HTTP failure, returns last-known.
    """
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
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = client.get(url, params=params or {})
            resp.raise_for_status()
            data = resp.json()
        with _cache_lock:
            _memory_cache[key] = (now, data)
        _write_disk_cache(key, data)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("API fetch failed for %s — %s", key, exc)
        if disk:
            log.info("Serving stale cache for %s", key)
            return disk[1]
        return None


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #
def cache_age_text(path_key: str) -> str:
    """Render a cache file's age as 'just now' / 'N minutes ago' / 'N hours ago'."""
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
# Shared thread pool used for parallel API fan-out (hero page cold loads,
# warm_cache background fills). Small pool — the upstream API is the bottleneck,
# not CPU. 8 workers is enough to overlap all the small per-hero endpoints.
# --------------------------------------------------------------------------- #
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sgs-fetch")


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


_CROSSCHECK_LOCK = threading.Lock()
_CROSSCHECK_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_CROSSCHECK_TTL = 6 * 60 * 60  # 6h - matches CACHE_SECONDS


def get_crosscheck_for(heroes: list[dict]) -> dict[str, dict]:
    """TTL-cached wrapper around run_crosscheck() so we don't refetch every
    request. Keyed by the hero set (rank), but in practice always called with
    'all' from get_tier_list, so a single shared 6h cache is enough."""
    with _CROSSCHECK_LOCK:
        if (
            _CROSSCHECK_CACHE["data"]
            and (time.time() - _CROSSCHECK_CACHE["ts"]) < _CROSSCHECK_TTL
        ):
            return _CROSSCHECK_CACHE["data"]  # type: ignore[return-value]
    try:
        result = run_crosscheck(heroes)
    except Exception as exc:  # noqa: BLE001
        # Never crash the site if crosscheck fails - degrade to empty.
        log.warning("[crosscheck] run_crosscheck failed: %s", exc)
        result = {}
    with _CROSSCHECK_LOCK:
        _CROSSCHECK_CACHE["ts"] = time.time()
        _CROSSCHECK_CACHE["data"] = result
    return result


def get_tier_list(rank: str = "all") -> list[dict]:
    # NB: default page size on this endpoint is 20, we need 200 to get all ~132 heroes.
    payload = api_get(
        "/api/heroes/rank",
        {"rank": rank, "days": 7, "sort_field": "win_rate", "size": 200},
    )
    heroes = parse_tier_ranking(payload)
    heroes.sort(key=lambda h: (h["win_rate"] or 0), reverse=True)

    # Enrich each hero with cross-source consensus data. If scrapers all
    # return zero (e.g. SPA pages we can't parse), each hero just gets the
    # OpenMLBB tier alone with safe_to_publish=False.
    cc = get_crosscheck_for(heroes)
    for h in heroes:
        info = cc.get(h.get("slug", ""), {})
        h["consensus_tier"]    = info.get("consensus_tier", h["tier"])
        h["confidence"]        = info.get("confidence", 0)
        h["has_conflict"]      = info.get("has_conflict", False)
        h["conflict_severity"] = info.get("conflict_severity", "none")
        h["editorial_note"]    = info.get("editorial_note", "")
        h["safe_to_publish"]   = info.get("safe_to_publish", False)
        h["source_tiers"]      = info.get("source_tiers", {})
        h["data_source_count"] = info.get("data_source_count", 0)
        h["verified_strongly"] = info.get("verified_strongly", False)
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
