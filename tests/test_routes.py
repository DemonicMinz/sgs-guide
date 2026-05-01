"""Smoke tests for the SGS guide.

Intentionally narrow — only routes that don't depend on the OpenMLBB
upstream API are covered, so the suite runs offline and doesn't break
when the upstream is rate-limiting or down. Hero / tier-list / role
routes hit live data and are out of scope here; if they regress we'll
notice from the homepage check (which renders shared template chrome
from base.html) plus production traffic.
"""
from __future__ import annotations


def test_robots_txt_lists_sitemap(client):
    """robots.txt should be reachable and reference the sitemap index."""
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert b"Sitemap:" in r.data


def test_sitemap_index_lists_subsitemaps(client):
    """The root sitemap should be a valid sitemapindex pointing at the
    per-type sub-sitemaps."""
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert b"<sitemapindex" in r.data
    assert b"sitemap-heroes.xml" in r.data
    assert b"sitemap-roles.xml" in r.data


def test_about_page_renders(client):
    """About is template-only — should render without external API calls."""
    r = client.get("/about")
    assert r.status_code == 200
    assert b"Singapore" in r.data


def test_unknown_route_returns_404(client):
    """Unknown paths should return a 404 (handled by the custom 404 page)."""
    r = client.get("/this-page-definitely-does-not-exist-zzz")
    assert r.status_code == 404


def test_config_module_exposes_expected_fields():
    """config.py should be importable and expose the documented contract."""
    from config import config

    assert config.SITE_NAME == "Singapore Gaming Syndicate"
    assert isinstance(config.PORT, int)
    assert config.API_BASE.startswith("https://")
    assert isinstance(config.TOPUP_ENABLED, bool)
    assert config.LANG_TAG == "en-SG"


def test_homepage_returns_200(client):
    """Homepage hits get_all_heroes() but should serve from disk cache when
    the OpenMLBB API is unreachable. If this regresses post-deploy, watch
    the dev console for the actual exception."""
    r = client.get("/")
    assert r.status_code == 200
