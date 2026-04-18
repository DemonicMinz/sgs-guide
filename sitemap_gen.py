"""
Standalone sitemap generator.

Writes a static sitemap.xml to disk by reusing the live helpers in app.py.
Useful for cron jobs or pre-deploy steps. The Flask /sitemap.xml route
remains the source of truth at request time; this file mirrors it.

Usage:
    python sitemap_gen.py [output_path]
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from app import SITE_URL, get_all_heroes


def build_sitemap() -> str:
    base = SITE_URL.rstrip("/")
    lastmod = datetime.now(timezone.utc).date().isoformat()
    urls = [("", "1.0"), ("/tier-list", "0.9"), ("/meta", "0.9"), ("/about", "0.5")]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, prio in urls:
        parts.append(
            f"<url><loc>{base}{path}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>daily</changefreq><priority>{prio}</priority></url>"
        )
    for h in get_all_heroes():
        parts.append(
            f"<url><loc>{base}/hero/{h['slug']}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>daily</changefreq><priority>0.8</priority></url>"
        )
    parts.append("</urlset>")
    return "\n".join(parts)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("sitemap.xml")
    xml = build_sitemap()
    out.write_text(xml, encoding="utf-8")
    print(f"Wrote {out} ({len(xml)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
