"""
Cross-check the OpenMLBB-derived tier list against external community
tier lists. Produces a per-hero consensus tier, confidence score and
conflict flag so the Flask layer can surface accurate, defensible data.

Sources (verified April 28, 2026):
  - mlbb.gg        - automated, daily, Mythic Glory weighted (data)
  - mlbbhub.com    - automated, daily, Patch 2.1.67 (data)
  - pocketgamer    - human curated, ~monthly (editorial)

Editorial sources can disagree with raw win-rate data on heroes with
high skill ceilings (Fanny, etc.); their disagreements surface as a
soft `editorial_note` rather than a hard conflict.
"""
from __future__ import annotations

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
    "pocketgamer": "https://www.pocketgamer.com/mobile-legends-bang-bang/tier-list/",
}

CACHE_TTL = {
    "mlbbgg":      12 * 60 * 60,   # 12h - automated, updates daily
    "mlbbhub":     12 * 60 * 60,   # 12h - automated, updates daily
    "pocketgamer": 72 * 60 * 60,   # 72h - editorial, updates ~monthly
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
# Scrapers - each returns {hero_slug: tier_label}. Empty dict on failure.
# --------------------------------------------------------------------------- #
def _scrape_mlbbgg() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from mlbb.gg automated tier list."""
    cached = _read_cc_cache("mlbbgg")
    if cached is not None:
        log.info("[mlbb.gg] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    try:
        r = httpx.get(SOURCES["mlbbgg"], headers=HEADERS, timeout=REQUEST_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        html = r.text

        tier_blocks = re.findall(
            r'(?:tier|data-tier)[="\s]+(["\']?)(SS?|A|B|C|D)\1[^>]*>.*?'
            r'<(?:a|span)[^>]*>([^<]{2,30})</(?:a|span)>',
            html, re.DOTALL | re.IGNORECASE,
        )
        for _, tier_raw, name_raw in tier_blocks:
            name = name_raw.strip()
            if len(name) < 2:
                continue
            slug = _name_to_slug(name)
            if slug:
                results[slug] = tier_raw.upper()

        # Fallback: hero href + adjacent tier label
        if not results:
            hero_tiers = re.findall(
                r'href=["\'][^"\']*?/hero/([a-z0-9-]+)["\'][^>]*>[^<]*'
                r'(?:<[^>]+>)*\s*([A-Z]{1,2})\s*(?:</[^>]+>)*\s*(?:tier|Tier)',
                html, re.IGNORECASE,
            )
            for slug, tier in hero_tiers:
                tier_up = tier.upper()
                if tier_up in TIER_SCORES:
                    results[slug] = tier_up

        if results:
            _write_cc_cache("mlbbgg", results)
            log.info("[mlbb.gg] Fetched %d hero tiers", len(results))
        else:
            log.warning("[mlbb.gg] Parsed 0 tiers - HTML structure may have changed. Inspect the page manually.")

    except Exception as exc:  # noqa: BLE001
        log.warning("[mlbb.gg] Fetch failed: %s", exc)

    return results


def _scrape_mlbbhub() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from mlbbhub.com tier list."""
    cached = _read_cc_cache("mlbbhub")
    if cached is not None:
        log.info("[mlbbhub] Serving from cache (%d heroes)", len(cached))
        return cached

    results: dict[str, str] = {}
    try:
        r = httpx.get(SOURCES["mlbbhub"], headers=HEADERS, timeout=REQUEST_TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
        html = r.text

        # mlbbhub renders tier rows with a tier label then a list of hero anchors.
        # Strategy: split by tier-row boundaries, then pull hero names within each.
        tier_sections = re.findall(
            r'(?:tier[-_]?row|tier[-_]?(?:label|name|header))[^>]*>\s*'
            r'(SS?|A|B|C|D)\s*<.*?(?=tier[-_]?row|tier[-_]?label|$)',
            html, re.DOTALL | re.IGNORECASE,
        )
        # Easier alternative: scan for tier badge then collect adjacent hero names
        for match in re.finditer(
            r'(?:class=["\'][^"\']*tier[-_]?(?:badge|label|name)[^"\']*["\'][^>]*>\s*'
            r'(SS?|A|B|C|D)\s*<)(.*?)(?=class=["\'][^"\']*tier[-_]?(?:badge|label|name)|$)',
            html, re.DOTALL | re.IGNORECASE,
        ):
            tier = match.group(1).upper()
            block = match.group(2)
            for name_match in re.finditer(
                r'(?:alt|title|data-name)=["\']([A-Z][A-Za-z\'\.\- ]{1,28})["\']',
                block,
            ):
                slug = _name_to_slug(name_match.group(1))
                if slug:
                    results.setdefault(slug, tier)

        # Fallback: pull from hero anchor + nearby tier element
        if not results:
            for href_match in re.finditer(
                r'href=["\'][^"\']*?/(?:hero|heroes)/([a-z0-9-]+)["\']'
                r'(?:[^>]*>[^<]*){0,3}(?:<[^>]+>){0,3}\s*([A-Z]{1,2})\s*<',
                html, re.IGNORECASE,
            ):
                slug, tier = href_match.group(1), href_match.group(2).upper()
                if tier in TIER_SCORES:
                    results.setdefault(slug, tier)

        if results:
            _write_cc_cache("mlbbhub", results)
            log.info("[mlbbhub] Fetched %d hero tiers", len(results))
        else:
            log.warning("[mlbbhub] Parsed 0 tiers - HTML structure may have changed.")

    except Exception as exc:  # noqa: BLE001
        log.warning("[mlbbhub] Fetch failed: %s", exc)

    return results


def _scrape_pocketgamer() -> dict[str, str]:
    """Returns {hero_slug: tier_label} from Pocket Gamer's editorial tier list.

    PG groups heroes under 'Tier S', 'Tier A', etc. headings - we walk the page
    section by section and assign every hero name in each section to that tier.
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

        # Split into sections each starting with a "Tier X" heading.
        sections = re.split(
            r'(?i)<h[2-4][^>]*>\s*(?:Tier\s+)?(SS?|A|B|C|D)[^<]*</h[2-4]>',
            html,
        )
        # Pattern of sections: [pre, tier1, body1, tier2, body2, ...]
        for i in range(1, len(sections) - 1, 2):
            tier = sections[i].upper()
            body = sections[i + 1]
            if tier not in TIER_SCORES:
                continue
            # Hero names in PG appear as bolded headings or link text
            for name_match in re.finditer(
                r'<(?:strong|b|h[3-5]|a)[^>]*>\s*([A-Z][A-Za-z\'\.\- ]{1,28})\s*</(?:strong|b|h[3-5]|a)>',
                body,
            ):
                name = name_match.group(1).strip()
                if len(name) < 2:
                    continue
                # Skip obvious non-hero matches
                if name.lower() in {"tier", "list", "best", "top", "the", "and", "or"}:
                    continue
                slug = _name_to_slug(name)
                if slug:
                    results.setdefault(slug, tier)

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

    enough_data_sources = len(valid_data) >= 3  # openmlbb + 2 external
    safe_to_publish = (
        enough_data_sources
        and confidence >= 70
        and conflict_severity != "major"
    )

    return {
        "consensus_tier":    consensus_tier,
        "confidence":        confidence,
        "has_conflict":      has_conflict,
        "conflict_severity": conflict_severity,
        "editorial_note":    editorial_note,
        "safe_to_publish":   safe_to_publish,
        "source_tiers":      {**data_tiers, **editorial_tiers},
    }


def run_crosscheck(heroes: list[dict]) -> dict[str, dict]:
    """Orchestrate scrapers + consensus per hero. Returns {slug: result_dict}."""
    log.info("[CrossCheck] Running for %d heroes...", len(heroes))

    mlbbgg = _scrape_mlbbgg()
    hub    = _scrape_mlbbhub()
    pg     = _scrape_pocketgamer()

    results: dict[str, dict] = {}
    for hero in heroes:
        slug = hero.get("slug", "")
        openmlbb_tier = hero.get("tier", "C")

        data_tiers: dict[str, str] = {}
        if slug in mlbbgg: data_tiers["mlbbgg"]  = mlbbgg[slug]
        if slug in hub:    data_tiers["mlbbhub"] = hub[slug]

        editorial_tiers: dict[str, str] = {}
        if slug in pg: editorial_tiers["pocketgamer"] = pg[slug]

        results[slug] = compute_crosscheck(slug, openmlbb_tier, data_tiers, editorial_tiers)

    conflicts = [(s, r) for s, r in results.items() if r["has_conflict"]]
    log.info("[CrossCheck] Done. %d conflicts, %d editorial notes.",
             len(conflicts),
             sum(1 for r in results.values() if r.get("editorial_note")))

    for slug, r in conflicts:
        name = next((h["name"] for h in heroes if h.get("slug") == slug), slug)
        log.warning("[CONFLICT] %s | severity=%s | confidence=%d%% | sources=%s",
                    name, r["conflict_severity"], r["confidence"], r["source_tiers"])

    return results
