"""SEO blueprint — sitemaps, robots.txt, manifest, favicon, IndexNow.

Sprint 4 / Phase B extraction. Routes that exist purely to feed search
engines and crawlers — sitemap index + per-type sub-sitemaps, robots.txt,
ads.txt, the PWA manifest, favicon fallback, and the IndexNow ownership
file. None of these render Jinja templates from `templates/`.

Registered in app.py via:
    from blueprints.seo import bp as seo_bp
    app.register_blueprint(seo_bp)

App-level helpers (`dynamic_site_url`, `ROLE_COLORS`, `LANE_META`,
`BASE_DIR`) are imported lazily inside each route to avoid a circular
import — app.py imports this blueprint at module load time, and at that
point app.py has not finished defining its own helpers yet. Once a
request actually fires, app.py is fully initialised and the lazy imports
resolve cleanly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, Response, send_from_directory

from config import config
from lib.openmlbb import (
    cache_modified_iso,
    get_all_heroes,
    get_tier_list,
    make_cache_key,
)

bp = Blueprint("seo", __name__)


# --------------------------------------------------------------------------- #
# Sitemap index + per-type sub-sitemaps
# --------------------------------------------------------------------------- #
@bp.route("/sitemap.xml")
def sitemap() -> Response:
    """Root sitemap index — points to per-type sub-sitemaps."""
    from app import dynamic_site_url
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name in ("sitemap-core.xml", "sitemap-heroes.xml", "sitemap-counters.xml",
                 "sitemap-vs.xml",
                 "sitemap-roles.xml", "sitemap-lanes.xml", "sitemap-images.xml"):
        parts.append(
            f"<sitemap><loc>{base}/{name}</loc><lastmod>{lastmod}</lastmod></sitemap>"
        )
    parts.append("</sitemapindex>")
    return Response("\n".join(parts), mimetype="application/xml")


@bp.route("/sitemap-core.xml")
def sitemap_core() -> Response:
    """Static top-level pages."""
    from app import dynamic_site_url
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
        ("/meta-now", "0.9", "daily"),
        ("/patch-notes", "0.9", "daily"),
        ("/singapore-mlbb-scrim", "0.7", "weekly"),
        ("/singapore-mlbb-teams", "0.7", "weekly"),
        ("/about", "0.5", "monthly"),
    ]
    if config.TOPUP_ENABLED:
        urls.extend([
            ("/topup", "0.8", "weekly"),
            ("/topup/mlbb", "0.8", "weekly"),
        ])
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, prio, freq in urls:
        parts.append(
            f"<url><loc>{base}{path}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@bp.route("/sitemap-heroes.xml")
def sitemap_heroes() -> Response:
    """One entry per hero guide page.

    Emits a per-hero <lastmod> derived from when that hero's stats cache
    was last refreshed. Lets Google prioritise re-crawling only the heroes
    whose data actually changed since its last visit, instead of treating
    all 132 pages as having moved on every sitemap fetch.
    """
    from app import dynamic_site_url
    base = dynamic_site_url()
    heroes = get_all_heroes()
    fallback_lastmod = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for h in heroes:
        stats_key = make_cache_key(
            f"/api/heroes/{h['id']}/stats",
            {"rank": "all"},
        )
        hero_lastmod = cache_modified_iso(stats_key) or fallback_lastmod
        parts.append(
            f"<url><loc>{base}/hero/{h['slug']}</loc>"
            f"<lastmod>{hero_lastmod}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.8</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@bp.route("/sitemap-counters.xml")
def sitemap_counters() -> Response:
    """One entry per /counter/<slug> page — 132 long-tail counter guides.

    Each entry uses the same per-hero stats cache mtime as sitemap-heroes,
    so Google sees both pages as updating together when stats change.
    """
    from app import dynamic_site_url
    base = dynamic_site_url()
    heroes = get_all_heroes()
    fallback_lastmod = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for h in heroes:
        stats_key = make_cache_key(
            f"/api/heroes/{h['id']}/stats",
            {"rank": "all"},
        )
        hero_lastmod = cache_modified_iso(stats_key) or fallback_lastmod
        parts.append(
            f"<url><loc>{base}/counter/{h['slug']}</loc>"
            f"<lastmod>{hero_lastmod}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.7</priority></url>"
        )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@bp.route("/sitemap-vs.xml")
def sitemap_vs() -> Response:
    """Hero-vs-hero matchup pages — top-20 × top-20 by pick rate.

    The /vs/<a>/<b> route serves up to ~8.6k alpha-ordered hero pairs, but
    we deliberately only advertise the popular subset (~190 pairs) to avoid
    looking like a doorway-page farm. Less popular pairs are still indexable
    via internal links and the canonical URL — we just don't ASK Google to
    crawl them.
    """
    from app import dynamic_site_url
    base = dynamic_site_url()
    lastmod = datetime.now(timezone.utc).date().isoformat()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    try:
        ranking = get_tier_list("all") or []
    except Exception:
        ranking = []
    top = sorted(ranking, key=lambda h: (h.get("pick_rate") or 0), reverse=True)[:20]
    slugs = sorted({h["slug"] for h in top if h.get("slug")})
    for i, a_slug in enumerate(slugs):
        for b_slug in slugs[i + 1:]:  # alpha-ordered, no self-pair, no duplicates
            parts.append(
                f"<url><loc>{base}/vs/{a_slug}/{b_slug}</loc>"
                f"<lastmod>{lastmod}</lastmod>"
                f"<changefreq>weekly</changefreq><priority>0.5</priority></url>"
            )
    parts.append("</urlset>")
    return Response("\n".join(parts), mimetype="application/xml")


@bp.route("/sitemap-roles.xml")
def sitemap_roles() -> Response:
    """Role landing pages (one per MLBB class)."""
    from app import dynamic_site_url, ROLE_COLORS
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


@bp.route("/sitemap-lanes.xml")
def sitemap_lanes() -> Response:
    """Lane landing pages (one per MLBB lane: jungle/mid/exp/gold/roam)."""
    from app import dynamic_site_url, LANE_META
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


@bp.route("/sitemap-images.xml")
def sitemap_images() -> Response:
    """Image sitemap — helps Google Images index hero portraits."""
    from app import dynamic_site_url
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


# --------------------------------------------------------------------------- #
# robots.txt + ads.txt
# --------------------------------------------------------------------------- #
@bp.route("/robots.txt")
def robots() -> Response:
    """SEO-friendly robots.txt:
    - Allow all good crawlers.
    - Block LLM training bots that don't give anything back (set aside, don't
      hurt SEO; these are AI scrapers, NOT search engines).
    - Point to sitemap-index at the top (Google reads Sitemap directives).
    """
    from app import dynamic_site_url
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
        f"Sitemap: {base}/sitemap-counters.xml",
        f"Sitemap: {base}/sitemap-vs.xml",
        f"Sitemap: {base}/sitemap-roles.xml",
        f"Sitemap: {base}/sitemap-lanes.xml",
        f"Sitemap: {base}/sitemap-images.xml",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@bp.route("/ads.txt")
def ads_txt() -> Response:
    # Authorized Digital Sellers — declares Google AdSense as a direct seller.
    return Response(
        "google.com, pub-3287033837149583, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain",
    )


# --------------------------------------------------------------------------- #
# PWA manifest + favicon
# --------------------------------------------------------------------------- #
@bp.route("/manifest.webmanifest")
def manifest() -> Response:
    """PWA manifest — tells Android to enable Add-to-Homescreen and signals
    to Google that this is a fully-fledged mobile-first web app."""
    from app import dynamic_site_url
    base = dynamic_site_url()
    body = {
        "name": config.SITE_NAME,
        "short_name": config.SITE_SHORT,
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
        "lang": config.LANG_TAG,
        "categories": ["games", "sports", "entertainment"],
        "icons": [
            {"src": f"{base}/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": f"{base}/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(body), mimetype="application/manifest+json")


@bp.route("/favicon.ico")
def favicon() -> Response:
    """Serve a favicon so browsers/crawlers stop 404-ing on /favicon.ico.
    Falls back to the SVG brand mark if no .ico exists yet."""
    static = config.BASE_DIR / "static"
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


# --------------------------------------------------------------------------- #
# IndexNow ownership proof
# --------------------------------------------------------------------------- #
@bp.route("/indexnow-<key>.txt")
def indexnow_keyfile(key: str) -> Response:
    """Serve the IndexNow ownership proof file. Only responds for the
    currently-configured key; returns 404 for any other path so attackers
    can't enumerate keys."""
    if not config.INDEXNOW_KEY or key != config.INDEXNOW_KEY:
        return Response("Not found", status=404, mimetype="text/plain")
    return Response(config.INDEXNOW_KEY, mimetype="text/plain")
