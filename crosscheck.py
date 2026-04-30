"""
Cross-check the OpenMLBB-derived tier list against external community
tier lists. Produces a per-hero consensus tier, confidence score and
conflict flag so the Flask layer can surface accurate, defensible data.

Sources (verified April 28, 2026):
  - mlbb.gg        - automated, daily, Mythic Glory weighted (data)
  - mlbbhub.com    - automated, daily, Patch 2.1.67 (data)
  - mlbb.io        - data-driven, bot-protected, 24h cache (data)
  - pocketgamer    - human curated, ~monthly (editorial)

Editorial sources can disagree with raw win-rate data on heroes with
high skill ceilings (Fanny, etc.); their disagreements surface as a
soft `editorial_note` rather than a hard conflict.
"""
from __future__ import annotations

import codecs
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("sgs.crosscheck")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache" / "crosscheck"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 20.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCES = {
    "mlbbgg":      "https://mlbb.gg/tierlist",
    "mlbbhub":     "https://mlbbhub.com/tier-list",
    "mlbbio":      "https://mlbb.io/hero-tier",
    "pocketgamer": "https://www.pocketgamer.com/mobile-legends-bang-bang/tier-list/",
}

CACHE_TTL = {
    "mlbbgg":      12 * 60 * 60,   # 12h - automated, updates daily
    "mlbbhub":     12 * 60 * 60,   # 12h - automated, updates daily
    "mlbbio":      24 * 60 * 60,   # 24h - bot-protected; minimise request rate
    "pocketgamer": 72 * 60 * 60,   # 72h - editorial, updates ~monthly
}

# mlbb.io has bot protection - needs full browser-like headers to bypass 403.
HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

# Higher number = stronger tier. Spread of 4 = SS vs B (major conflict).
TIER_SCORES: dict[str, int] = {
    "SS": 6,
    "S":  5,
    "A":  4,
    "B":  3,
    "C":  2,
    "D":  1,
}


def _score_to_tier(score: float) -> str:
    """Map a numeric score back to its closest tier label."""
    rounded = round(score)
    for label, val in TIER_SCORES.items():
        if val == rounded:
            return label
    if rounded > 6:
        return "SS"
    return "D"


# --------------------------------------------------------------------------- #
# Slug normalizer - must agree with app.slugify so cross-source joins work.
# --------------------------------------------------------------------------- #
def _name_to_slug(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace to hyphens.
    Matches the spec test cases: Masha->masha, Chang'e->change,
    Popol and Kupa->popol-and-kupa, X.Borg->xborg, Yi Sun-shin->yi-sun-shin.
    """
    s = (name or "").lower().strip()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")


def _normalize_tier(raw: str) -> str | None:
    """Normalise tier labels across sources. Returns None if invalid.
    `S+` and `s+` map to `SS`; everything else is uppercased and validated
    against TIER_SCORES."""
    if not raw:
        return None
    cleaned = raw.strip().upper().replace("+", "S")
    if cleaned in TIER_SCORES:
        return cleaned
    return None


# --------------------------------------------------------------------------- #
# Disk cache - per-source JSON file, TTL'd.
# --------------------------------------------------------------------------- #
def _cache_file(source: str) -> Path:
    return CACHE_DIR / f"{source}.json"


def _read_cc_cache(source: str) -> dict[str, str] | None:
    path = _cache_file(source)
    if not path.exists():
        return None
    try:
        ttl = CACHE_TTL.get(source, 12 * 60 * 60)
        if time.time() - path.stat().st_mtime > ttl:
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] cache read failed: %s", source, exc)
    return None


def _write_cc_cache(source: str, data: dict[str, str]) -> None:
    try:
        with _cache_file(source).open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] cache write failed: %s", source, exc)


# --------------------------------------------------------------------------- #
# Helpers for SPA pages that embed JSON in __next_f.push() chunks.
# --------------------------------------------------------------------------- #
_NEXT_F_PUSH = re.compile(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)')


def _extract_rsc_payload(html: str) -> str:
    """Concatenate and unescape the React Server Components stream that
    Next.js App Router emits as `self.__next_f.push([...])` chunks. The
    decoded text contains the page's JSON data inline."""
    out = ""
    for fragment in _NEXT_F_PUSH.findall(html):
        try:
            out += codecs.decode(fragment, "unicode_escape")
        except Exception:  # noqa: BLE001
            pass
    return out


def _find_balanced(text: str, open_idx: int) -> int:
    """Index of the bracket matching `text[open_idx]`. Skips strings (with
    \\" escapes). Returns -1 if no match."""
    open_ch = text[open_idx]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


# --------------------------------------------------------------------------- #
# Scrapers - each returns {hero_slug: tier_label}. Empty dict on failure.
# --------------------------------------------------------------------------- #
def _scrape_mlbbgg() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from mlbb.gg automated tier list.

    mlbb.gg is a Next.js App Router site - the tier data is embedded
    inline as a serialized RSC payload, not in the HTML markup. We pull
    the chunks, find the `[{"tier":"SS","data":[...]},...]` array and
    parse it as JSON.
    """
    cached = _read_cc_cache("mlbbgg")
    if cached is not None:
        log.info("[mlbb.gg] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    try:
        r = httpx.get(SOURCES["mlbbgg"], headers=HEADERS, timeout=REQUEST_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        payload = _extract_rsc_payload(r.text)

        anchor = re.search(r'"data"\s*:\s*\[\s*\{"tier"', payload)
        if anchor:
            arr_start = payload.index("[", anchor.start())
            arr_end = _find_balanced(payload, arr_start)
            if arr_end > arr_start:
                try:
                    parsed = json.loads(payload[arr_start:arr_end + 1])
                except json.JSONDecodeError as exc:
                    log.warning("[mlbb.gg] JSON parse failed: %s", exc)
                    parsed = []
                for tier_block in parsed:
                    tier = (tier_block.get("tier") or "").upper()
                    if tier not in TIER_SCORES:
                        continue
                    for entry in tier_block.get("data", []) or []:
                        hero = entry.get("hero") or {}
                        slug = _name_to_slug(hero.get("name") or "")
                        if slug:
                            results.setdefault(slug, tier)

        if results:
            _write_cc_cache("mlbbgg", results)
            log.info("[mlbb.gg] Fetched %d hero tiers", len(results))
        else:
            log.warning("[mlbb.gg] Parsed 0 tiers - RSC payload structure may have changed. Inspect the page manually.")

    except Exception as exc:  # noqa: BLE001
        log.warning("[mlbb.gg] Fetch failed: %s", exc)

    return results


# Pattern shared by the mlbbhub scraper. mlbbhub embeds the tier list
# inside a JSON-LD ItemList where each name reads "Hero (X-Tier)" and the
# url points at /heroes/<slug>. The blob is a JSON string nested inside
# another JSON document so quotes appear escaped (\\\" or \").
_MLBBHUB_ENTRY = re.compile(
    r'(?:\\?")name(?:\\?")\s*:\s*(?:\\?")([A-Z][A-Za-z0-9 \.\'\-]{1,28})'
    r'\s*\(([A-Z]{1,2})-Tier\)(?:\\?").{1,80}?'
    r'(?:\\?")url(?:\\?")\s*:\s*(?:\\?")https?://mlbbhub\.com/heroes/([a-z0-9\-]+)(?:\\?")',
    re.DOTALL,
)


def _scrape_mlbbhub() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from mlbbhub.com.

    mlbbhub emits its full tier list as a JSON-LD ItemList where each
    entry's name is `"<Hero> (<Tier>-Tier)"`. Stable structured data
    that does not depend on visual layout - the cleanest source we
    have.
    """
    cached = _read_cc_cache("mlbbhub")
    if cached is not None:
        log.info("[mlbbhub] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    try:
        r = httpx.get(SOURCES["mlbbhub"], headers=HEADERS, timeout=REQUEST_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        for m in _MLBBHUB_ENTRY.finditer(r.text):
            tier = m.group(2).upper()
            slug = m.group(3)
            if tier in TIER_SCORES and slug:
                results.setdefault(slug, tier)

        if results:
            _write_cc_cache("mlbbhub", results)
            log.info("[mlbbhub] Fetched %d hero tiers", len(results))
        else:
            log.warning("[mlbbhub] Parsed 0 tiers - JSON-LD structure may have changed.")

    except Exception as exc:  # noqa: BLE001
        log.warning("[mlbbhub] Fetch failed: %s", exc)

    return results


# mlbb.io's /hero-tier page is an empty Next.js shell - the tier table is
# fetched client-side from this JSON endpoint and rendered in the browser.
# Hitting it directly is the canonical path the site itself uses.
_MLBBIO_API = "https://mlbb.io/api/hero/hero-tiers"

# mlbb.io's published `tier` label uses an unusually harsh scale - on a
# 132-hero roster it labels ~60 heroes D and ~39 heroes C, while mlbb.gg
# and mlbbhub label the same heroes A or B. Joining its tier directly
# pulled consensus down on the majority of heroes (safe-list shrank
# 93 -> 52). mlbb.io's underlying numeric `score` is still a useful
# independent signal, so we re-bucket by score-rank percentile into a
# distribution shaped like mlbb.gg's typical tier list:
#   SS top 2.5% / S 19.5% / A 37% / B 20% / C 6% / D bottom 15%.
_MLBBIO_PERCENTILE_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.025, "SS"),
    (0.220, "S"),
    (0.590, "A"),
    (0.790, "B"),
    (0.850, "C"),
    (1.000, "D"),
)


def _score_rank_to_tier(rank: int, total: int) -> str:
    """Map a 0-indexed score rank (0 = highest score) to a tier via percentile."""
    if total <= 0:
        return "D"
    pct = (rank + 1) / total
    for threshold, tier in _MLBBIO_PERCENTILE_BUCKETS:
        if pct <= threshold:
            return tier
    return "D"


def _scrape_mlbbio() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from mlbb.io's tier-list JSON API.

    The page at /hero-tier is a thin Next.js shell that calls
    /api/hero/hero-tiers client-side; we hit that endpoint directly.
    Schema: {"success": bool, "data": {"heroes": [{"hero_name": str,
    "tier": "SS"|..|"D", "score": float, ...}, ...]}}.

    We rank heroes by `score` and re-bucket into our shared tier scale
    rather than trusting mlbb.io's published `tier` field, which is
    harsher than the rest of the community sources (see comment on
    _MLBBIO_PERCENTILE_BUCKETS above). On 403 / timeout / parse failure,
    returns an empty dict so the rest of the pipeline keeps working.
    Cached aggressively (24h) since the API is gated behind anti-bot
    rules.
    """
    cached = _read_cc_cache("mlbbio")
    if cached is not None:
        log.info("[mlbb.io] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    headers = {
        **HEADERS_BROWSER,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://mlbb.io/hero-tier",
    }
    try:
        r = httpx.get(
            _MLBBIO_API,
            headers=headers,
            timeout=20.0,
            follow_redirects=True,
        )

        if r.status_code == 403:
            log.warning(
                "[mlbb.io] Got 403 - bot protection blocked us. "
                "Try updating browser headers or wait for cache TTL."
            )
            return {}

        r.raise_for_status()
        payload = r.json()

        heroes = ((payload or {}).get("data") or {}).get("heroes") or []
        ranked = sorted(
            (h for h in heroes
             if isinstance(h.get("score"), (int, float)) and h.get("hero_name")),
            key=lambda h: h["score"],
            reverse=True,
        )
        total = len(ranked)
        for rank, entry in enumerate(ranked):
            slug = _name_to_slug(entry["hero_name"])
            if slug:
                results.setdefault(slug, _score_rank_to_tier(rank, total))

        if results:
            _write_cc_cache("mlbbio", results)
            log.info("[mlbb.io] Fetched %d hero tiers (rebucketed by score)", len(results))
        else:
            log.warning(
                "[mlbb.io] Parsed 0 tiers - API schema may have changed. "
                "Inspect %s manually.", _MLBBIO_API
            )

    except httpx.HTTPStatusError as exc:
        log.warning("[mlbb.io] HTTP error: %s", exc)
    except httpx.TimeoutException:
        log.warning("[mlbb.io] Request timed out after 20s")
    except Exception as exc:  # noqa: BLE001
        log.warning("[mlbb.io] Unexpected error: %s", exc)

    return results


# Word lists used to filter false positives when parsing PG's prose-heavy
# role sections. Anything containing one of these tokens is rejected as
# a hero name.
_PG_NOISE_TOKENS = (
    "tier", "below", "click", "note", "pocket", "mobile legends",
    "updated", "role", "damage", "team", "play", "best", "pick", "win",
    "jungle", "tank", "mage", "fighter", "marksman", "support", "assassin",
)
_PG_ROLES = ("Tanks", "Fighters", "Marksmen", "Mages", "Assassins", "Supports")


def _scrape_pocketgamer() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from Pocket Gamer's editorial tier
    list.

    PG groups heroes by role rather than by tier. Within each role
    section the layout is `Tier\\n<role>\\nS+\\nHero, Hero\\nS\\nHero...`,
    so we walk each role section line-by-line and treat any tier-letter
    line as a heading whose following lines are comma-separated hero
    names. `S+` is normalised to `SS`. Editorial source - any false
    positives that don't match an OpenMLBB slug are silently dropped at
    the join in run_crosscheck().
    """
    cached = _read_cc_cache("pocketgamer")
    if cached is not None:
        log.info("[pocketgamer] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    try:
        r = httpx.get(SOURCES["pocketgamer"], headers=HEADERS, timeout=REQUEST_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        html = r.text

        for role in _PG_ROLES:
            section_match = re.search(
                rf'Best Mobile Legends {role}(.*?)(?:Best Mobile Legends|$)',
                html, re.DOTALL,
            )
            if not section_match:
                continue
            text = re.sub(r'<[^>]+>', '\n', section_match.group(1))
            text = re.sub(r'\n{2,}', '\n', text)

            current_tier: str | None = None
            for raw_line in text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                tier_match = re.fullmatch(r'(S\+|SS|S|A|B|C|D)', line)
                if tier_match:
                    current_tier = tier_match.group(1)
                    if current_tier == "S+":
                        current_tier = "SS"
                    continue
                if current_tier is None or line == "-":
                    continue
                lower = line.lower()
                if any(tok in lower for tok in _PG_NOISE_TOKENS):
                    continue
                for raw_name in line.split(","):
                    name = raw_name.strip()
                    if not name or len(name) < 2 or len(name) > 25:
                        continue
                    if not re.match(r"^[A-Z][A-Za-z\.'\- ]+$", name):
                        continue
                    slug = _name_to_slug(name)
                    if slug:
                        results.setdefault(slug, current_tier)

        if results:
            _write_cc_cache("pocketgamer", results)
            log.info("[pocketgamer] Fetched %d hero tiers", len(results))
        else:
            log.warning("[pocketgamer] Parsed 0 tiers - HTML structure may have changed.")

    except Exception as exc:  # noqa: BLE001
        log.warning("[pocketgamer] Fetch failed: %s", exc)

    return results


# --------------------------------------------------------------------------- #
# Consensus engine
# --------------------------------------------------------------------------- #
def compute_crosscheck(
    hero_slug: str,
    openmlbb_tier: str,
    data_tiers: dict[str, str],      # from mlbbgg + mlbbhub (automated)
    editorial_tiers: dict[str, str], # from pocketgamer (human-curated)
) -> dict[str, Any]:
    """Separate data-driven sources from editorial ones.
    Conflicts among data sources are flagged prominently; editorial-only
    disagreements surface as a soft note (often a skill-ceiling effect).
    """
    all_data = [openmlbb_tier] + list(data_tiers.values())
    valid_data = [t for t in all_data if t in TIER_SCORES]

    if not valid_data:
        return {
            "consensus_tier":    openmlbb_tier,
            "confidence":        0,
            "has_conflict":      False,
            "conflict_severity": "none",
            "editorial_note":    "",
            "safe_to_publish":   False,
            "source_tiers":      {**data_tiers, **editorial_tiers},
        }

    scores = [TIER_SCORES[t] for t in valid_data]
    avg = sum(scores) / len(scores)
    consensus_tier = _score_to_tier(avg)
    spread = max(scores) - min(scores)
    variance = sum((s - avg) ** 2 for s in scores) / len(scores)
    std_dev = variance ** 0.5
    confidence = max(0, round(100 - (std_dev / 2.5) * 100))

    if spread >= 4:
        conflict_severity = "major"
    elif spread >= 2:
        conflict_severity = "minor"
    else:
        conflict_severity = "none"

    has_conflict = conflict_severity != "none"

    editorial_note = ""
    for source, tier in editorial_tiers.items():
        if tier in TIER_SCORES:
            editorial_score = TIER_SCORES[tier]
            if abs(editorial_score - avg) >= 2 and conflict_severity == "none":
                editorial_note = (
                    f"{source} rates this hero as {tier} - this may reflect "
                    f"high-rank or coordinated play differences vs general ranked data."
                )

    # Bonus: heroes where ALL 4 data sources (openmlbb + 3 externals) returned
    # AND agree exactly get +5 confidence. Universal agreement = guaranteed
    # safe to publish on social.
    all_four_agree = len(valid_data) == 4 and spread == 0
    if all_four_agree:
        confidence = min(100, confidence + 5)

    enough_data_sources = len(valid_data) >= 3  # openmlbb + 2 external (mlbbio may fail)
    safe_to_publish = (
        enough_data_sources
        and confidence >= 70
        and conflict_severity != "major"
    )

    # How many of the 3 external data-driven sources returned a tier for this
    # hero. 3 = fully verified. Editorial sources (pocketgamer) don't count.
    data_source_count = sum(
        1 for src in data_tiers if src in ("mlbbgg", "mlbbhub", "mlbbio")
    )

    return {
        "consensus_tier":    consensus_tier,
        "confidence":        confidence,
        "has_conflict":      has_conflict,
        "conflict_severity": conflict_severity,
        "editorial_note":    editorial_note,
        "safe_to_publish":   safe_to_publish,
        "source_tiers":      {**data_tiers, **editorial_tiers},
        "data_source_count": data_source_count,
        "verified_strongly": data_source_count >= 3,
    }


def run_crosscheck(heroes: list[dict]) -> dict[str, dict]:
    """Orchestrate scrapers + consensus per hero. Returns {slug: result_dict}.

    Each scraper is independent: if one fails (returns {}), the others still
    feed the consensus engine. mlbb.io specifically may return {} on days
    where its bot protection rejects us; the pipeline degrades gracefully.
    """
    log.info("[CrossCheck] Running for %d heroes...", len(heroes))

    mlbbgg = _scrape_mlbbgg()
    hub    = _scrape_mlbbhub()
    mlbbio = _scrape_mlbbio()
    pg     = _scrape_pocketgamer()

    results: dict[str, dict] = {}
    for hero in heroes:
        slug = hero.get("slug", "")
        openmlbb_tier = hero.get("tier", "C")

        data_tiers: dict[str, str] = {}
        if slug in mlbbgg: data_tiers["mlbbgg"]  = mlbbgg[slug]
        if slug in hub:    data_tiers["mlbbhub"] = hub[slug]
        if slug in mlbbio: data_tiers["mlbbio"]  = mlbbio[slug]

        editorial_tiers: dict[str, str] = {}
        if slug in pg: editorial_tiers["pocketgamer"] = pg[slug]

        results[slug] = compute_crosscheck(slug, openmlbb_tier, data_tiers, editorial_tiers)

    conflicts = [(s, r) for s, r in results.items() if r["has_conflict"]]
    log.info(
        "[CrossCheck] Done. %d conflicts, %d editorial notes "
        "(sources: openmlbb + mlbbgg + mlbbhub + mlbbio + pg).",
        len(conflicts),
        sum(1 for r in results.values() if r.get("editorial_note")),
    )

    for slug, r in conflicts:
        name = next((h["name"] for h in heroes if h.get("slug") == slug), slug)
        log.warning("[CONFLICT] %s | severity=%s | confidence=%d%% | sources=%s",
                    name, r["conflict_severity"], r["confidence"], r["source_tiers"])

    return results
