"""OpenMLBB API client — caching layer.

Sprint 4 / Phase A (step 1) extraction. This module currently owns the
disk + memory caching primitives and the cached HTTP wrapper (`api_get`).
Parsers and `get_*` accessors still live in app.py and will move here in
a follow-up commit; the split keeps each migration small enough to bisect
if something regresses.

Public surface used by app.py:
    api_get(path, params)                      — cached GET, 6h TTL
    make_cache_key(path, params)               — canonical cache key
    cache_modified_iso(cache_key)              — file-mtime ISO (for sitemap)
    cache_age_text(path_key)                   — humanised "5 minutes ago"
    _read_disk_cache(key)                      — used by health-check probes

Internal state (private, do not import):
    _memory_cache, _cache_lock
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import config

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
