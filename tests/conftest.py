"""Shared pytest fixtures.

Sets SGS_HEALTH_INTERVAL_H=0 BEFORE app.py is imported so the background
health-monitor thread does not start during the test session (per the note
at the bottom of app.py). This must run before any test imports `app`.
"""
from __future__ import annotations

import os

# Disable the background health-monitor thread for the duration of tests.
# app.py reads this env var at import time (line ~2744), so it has to be
# set before `from app import app` happens anywhere in the test suite.
os.environ.setdefault("SGS_HEALTH_INTERVAL_H", "0")

import pytest  # noqa: E402  (must come after the env-var setup above)


@pytest.fixture
def client():
    """A Flask test client with TESTING=True."""
    from app import app  # imported lazily so env-var setup above takes effect

    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
