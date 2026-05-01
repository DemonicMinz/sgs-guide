"""Opt-in logging configuration for the SGS guide.

This module is NOT auto-wired into app.py. The existing app.py uses
`logging.basicConfig(...)` which gunicorn-managed deployments rely on, and
swapping to a rotating file handler wholesale would risk double-logging
under gunicorn's own log capture.

Use cases for calling `configure_logging()` explicitly:
  * Local non-gunicorn dev runs that want logs persisted across restarts.
  * Future systemd/supervisor setups that don't want gunicorn to own
    the log lifecycle.
  * Tests that need a clean known logger configuration.

Example:
    from logging_config import configure_logging
    from config import config
    configure_logging(config.LOG_DIR)
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    log_filename: str = "sgs.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB per file
    backup_count: int = 5,              # keep 5 rotated files
) -> None:
    """Attach a rotating file handler + stream handler to the root logger.

    Idempotent — calling twice will not stack handlers, because we check
    for existing handlers before adding new ones.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid stacking handlers on repeated calls (e.g. werkzeug auto-reload).
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               for h in root.handlers):
        root.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.handlers.RotatingFileHandler)
               for h in root.handlers):
        root.addHandler(stream_handler)

    # Quiet down noisy 3rd-party libs that flood at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
