"""Centralized configuration for the SGS guide.

All environment-variable reads SHOULD live here so tests can override them
cleanly and there's a single source of truth for defaults. New code should
import `config` from this module instead of calling `os.getenv()` directly.

Migration note: as of Sprint 3 the top-level env reads in `app.py` source
their values from here, but a handful of deeper, runtime-only env reads
(SGS_STALE_THRESHOLD_H, MLBB_PATCH, SGS_HEALTH_INTERVAL_H) are still done
inline at their use sites. Those will move here in a future cleanup; in
the meantime they're declared on the Config class so the contract is at
least documented in one place.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Load .env BEFORE the Config class is evaluated, so class-level reads see
# the values. app.py also calls load_dotenv() — that's idempotent, so doing
# it twice is harmless and protects against import-order surprises.
load_dotenv()


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ---- Server ---------------------------------------------------------- #
    PORT: Final[int] = int(os.getenv("PORT", "8085"))

    # ---- Site identity --------------------------------------------------- #
    # SITE_URL: blank means "fall back to the live request host", which lets
    # Cloudflare-tunnel preview URLs work without a code edit. Cleaning is
    # done here once so callers get a guaranteed-trimmed value.
    SITE_URL: Final[str] = (os.getenv("SITE_URL") or "").strip().rstrip("/")
    SITE_NAME: Final[str] = "Singapore Gaming Syndicate"
    SITE_SHORT: Final[str] = "SGS"
    SITE_TAGLINE: Final[str] = "Where Singapore's Best Gamers Operate"
    TELEGRAM_URL: Final[str] = "https://t.me/SingaporeGamingSyndicate"

    # ---- Analytics + verification --------------------------------------- #
    GA4_ID: Final[str] = os.getenv("GA4_ID", "")
    GOOGLE_VERIFICATION: Final[str] = os.getenv("GOOGLE_SITE_VERIFICATION", "")
    BING_VERIFICATION: Final[str] = os.getenv("BING_SITE_VERIFICATION", "")
    YANDEX_VERIFICATION: Final[str] = os.getenv("YANDEX_SITE_VERIFICATION", "")
    INDEXNOW_KEY: Final[str] = (os.getenv("INDEXNOW_KEY") or "").strip()

    # ---- Feature flags --------------------------------------------------- #
    TOPUP_ENABLED: Final[bool] = _bool_env("TOPUP_ENABLED", "false")

    # ---- Upstream API + cache ------------------------------------------- #
    API_BASE: Final[str] = "https://openmlbb.fastapicloud.dev"
    CACHE_SECONDS: Final[int] = 6 * 60 * 60  # 6 hours
    REQUEST_TIMEOUT: Final[float] = 20.0

    # ---- Locale / geo ---------------------------------------------------- #
    LANG_TAG: Final[str] = "en-SG"
    GEO_REGION: Final[str] = "SG"
    GEO_PLACENAME: Final[str] = "Singapore"
    GEO_ICBM: Final[str] = "1.3521, 103.8198"

    # ---- Paths ----------------------------------------------------------- #
    BASE_DIR: Final[Path] = Path(__file__).resolve().parent
    CACHE_DIR: Final[Path] = BASE_DIR / "cache"
    LOG_DIR: Final[Path] = BASE_DIR / "logs"

    # ---- Documented-but-not-yet-centralized ----------------------------- #
    # These are still read inline from os.getenv() in app.py. Listed here
    # so the contract is visible. Migrate in a follow-up commit.
    #   SGS_STALE_THRESHOLD_H   (default 24)   — stale-data banner cutoff
    #   MLBB_PATCH              (default "")   — manual patch tag override
    #   SGS_HEALTH_INTERVAL_H   (default 1)    — health-monitor interval


config: Final[Config] = Config()
