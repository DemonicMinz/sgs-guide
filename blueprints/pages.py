"""Pages blueprint — meta report, patch notes, about.

Sprint 4 / Phase D extraction. The remaining template-rendering routes
that don't fit the heroes blueprint:

    /meta           meta_page         live top-10 win/pick/ban report
    /patch-notes    patch_notes_page  auto-generated patch meta digest
    /about          about_page        static brand page

Routes that DO NOT live here:
  * /topup*       gated by feature flag, kept in app.py for now since the
                  payment side already lives in topup_payment.py blueprint.
  * /healthz*     operations endpoints, kept in app.py with the health
                  monitor it controls.
  * /api/*        custom JSON APIs, kept in app.py until Phase E if ever.

Registered in app.py via:
    from blueprints.pages import bp as pages_bp
    app.register_blueprint(pages_bp)

Lazy-imports SITE_NAME from app for use in page_title strings, same
pattern as the heroes blueprint.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Blueprint, render_template

from lib.openmlbb import (
    cache_age_text,
    get_all_heroes,
    get_tier_list,
    make_cache_key,
    slugify,
)

bp = Blueprint("pages", __name__)


# --------------------------------------------------------------------------- #
# Patch notes
# --------------------------------------------------------------------------- #
@bp.route("/patch-notes")
def patch_notes_page() -> str:
    """Auto-generated patch meta digest.

    We don't have week-over-week delta data, but we CAN derive a genuinely
    useful patch report from the live 7-day ranked stats:

      - 'Meta Definers'    : WR >= 54% (SS tier — what's breaking the game)
      - 'Pick Priorities'  : top 5 by pick rate (what the ladder is spamming)
      - 'Ban Priorities'   : top 5 by ban rate (what pros fear)
      - 'Under the Radar'  : high WR + low pick (<= 1%) — secret-OP hidden gems
      - 'Struggling Heroes': bottom 5 WR (nerf-candidates / avoid these)

    Each section is both an SEO landing for queries like 'mlbb patch notes
    tier list', 'most banned mlbb', 'hidden op heroes mlbb', AND a genuinely
    useful patch digest users will want to bookmark.
    """
    from app import SITE_NAME  # lazy

    tier_data = get_tier_list("all")
    catalog = {h["id"]: h for h in get_all_heroes()}

    def enrich(h: dict) -> dict:
        meta = catalog.get(h["id"]) or {}
        out = dict(h)
        out["slug"] = meta.get("slug") or slugify(h.get("name", ""))
        out["head"] = h.get("head") or meta.get("head")
        out["role"] = meta.get("role")
        out["lanes"] = meta.get("lanes") or []
        return out

    # Meta definers: SS tier (WR >= 54%) — what's breaking the game
    meta_definers = [enrich(h) for h in tier_data if (h.get("win_rate") or 0) >= 0.54][:10]

    # Pick + ban priorities
    by_pick = sorted(
        [enrich(h) for h in tier_data],
        key=lambda h: h.get("pick_rate") or 0,
        reverse=True,
    )[:5]
    by_ban = sorted(
        [enrich(h) for h in tier_data],
        key=lambda h: h.get("ban_rate") or 0,
        reverse=True,
    )[:5]

    # Under the radar: high WR + low pick rate
    under_radar = sorted(
        [
            enrich(h) for h in tier_data
            if (h.get("win_rate") or 0) >= 0.52
            and (h.get("pick_rate") or 1) <= 0.01
        ],
        key=lambda h: h.get("win_rate") or 0,
        reverse=True,
    )[:5]

    # Struggling heroes: bottom WR
    struggling = sorted(
        [enrich(h) for h in tier_data if h.get("win_rate") is not None],
        key=lambda h: h.get("win_rate") or 0,
    )[:5]

    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    patch_tag = os.getenv("MLBB_PATCH", "").strip()
    patch_label = f"Patch {patch_tag}" if patch_tag else f"Live MLBB Meta"

    return render_template(
        "patch_notes.html",
        patch_label=patch_label,
        patch_tag=patch_tag,
        meta_definers=meta_definers,
        by_pick=by_pick,
        by_ban=by_ban,
        under_radar=under_radar,
        struggling=struggling,
        total_heroes=len(tier_data),
        updated=updated,
        page_title=(
            f"MLBB {patch_label} — {current_month} Meta Report, "
            f"Tier Shifts & Ban List | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB patch meta report for {current_month}: "
            f"meta-defining heroes, top picks, ban priorities and "
            f"under-the-radar gems. Auto-refreshed every 6 hours from "
            f"real ranked match data — no guesswork, no outdated tier lists."
        ),
        page_keywords=(
            f"mlbb patch notes {current_month.lower()}, mobile legends patch "
            "meta, mlbb most banned, mlbb hidden op, mlbb meta report, "
            "mlbb patch analysis singapore, mobile legends tier shifts"
        ),
        canonical="/patch-notes",
    )


# --------------------------------------------------------------------------- #
# Meta page (now labelled "Stats" in the nav, URL kept for SEO continuity)
# --------------------------------------------------------------------------- #
@bp.route("/meta")
def meta_page() -> str:
    from app import SITE_NAME  # lazy

    all_heroes = get_tier_list("all")
    by_wr = sorted(all_heroes, key=lambda h: h["win_rate"] or 0, reverse=True)[:10]
    by_pick = sorted(all_heroes, key=lambda h: h["pick_rate"] or 0, reverse=True)[:10]
    by_ban = sorted(all_heroes, key=lambda h: h["ban_rate"] or 0, reverse=True)[:10]
    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "meta.html",
        by_wr=by_wr,
        by_pick=by_pick,
        by_ban=by_ban,
        updated=updated,
        page_title=(
            f"MLBB Stats {current_month} — Top Picks, Highest Win "
            f"Rate & Most Banned | {SITE_NAME}"
        ),
        page_desc=(
            f"Current MLBB meta analysis for {current_month} — top-picked, "
            f"highest win rate and most banned heroes this patch. "
            f"Data refreshed every 6 hours from real ranked matches."
        ),
        page_keywords=(
            f"mlbb meta {current_month.lower()}, mobile legends meta, "
            "best picks mlbb, most banned heroes, top win rate mlbb, "
            "mlbb ban list, mlbb s+ tier"
        ),
        canonical="/meta",
    )


# --------------------------------------------------------------------------- #
# Meta Now — keyword-targeted "current meta" landing page
# --------------------------------------------------------------------------- #
# Specifically built to rank for time-sensitive head-term-adjacent queries
# that the existing /meta and /patch-notes pages don't naturally target:
#   "mlbb meta now" / "mlbb meta this week" / "current mlbb meta" /
#   "mlbb best heroes right now" / "mlbb may 2026 meta"
# Uses the same data layer as /patch-notes but with present-tense framing
# and a dateModified that updates every render — Google ranks "now" queries
# heavily on freshness signal, not just content quality.
@bp.route("/meta-now")
def meta_now_page() -> str:
    from app import SITE_NAME  # lazy

    tier_data = get_tier_list("all")
    catalog = {h["id"]: h for h in get_all_heroes()}

    def enrich(h: dict) -> dict:
        meta = catalog.get(h["id"]) or {}
        out = dict(h)
        out["slug"] = meta.get("slug") or slugify(h.get("name", ""))
        out["head"] = h.get("head") or meta.get("head")
        out["role"] = meta.get("role")
        return out

    enriched = [enrich(h) for h in tier_data if h.get("name")]

    top_wr = sorted(
        [h for h in enriched if h.get("win_rate") is not None],
        key=lambda h: h.get("win_rate") or 0,
        reverse=True,
    )
    top_ban = sorted(
        [h for h in enriched if h.get("ban_rate") is not None],
        key=lambda h: h.get("ban_rate") or 0,
        reverse=True,
    )
    # Hidden OP: high WR + low pick rate (<= 1.5%). Same heuristic as
    # patch_notes' "Under the Radar" but with a slightly looser cutoff so
    # we always have something to show even in homogenous metas.
    hidden_op = sorted(
        [
            h for h in enriched
            if (h.get("win_rate") or 0) >= 0.51
            and (h.get("pick_rate") or 1) <= 0.015
        ],
        key=lambda h: h.get("win_rate") or 0,
        reverse=True,
    )
    struggling = sorted(
        [h for h in enriched if h.get("win_rate") is not None],
        key=lambda h: h.get("win_rate") or 0,
    )

    cache_key = make_cache_key(
        "/api/heroes/rank",
        {"rank": "all", "days": 7, "sort_field": "win_rate", "size": 200},
    )
    updated = cache_age_text(cache_key)
    # Full ISO timestamp for schema.org dateModified — recency signal Google
    # uses for "now" queries.
    updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_month = datetime.now(timezone.utc).strftime("%B %Y")

    top_name = top_wr[0]["name"] if top_wr else "the meta"
    return render_template(
        "meta_now.html",
        top_wr=top_wr,
        top_ban=top_ban,
        hidden_op=hidden_op,
        struggling=struggling,
        total_heroes=len(enriched),
        updated=updated,
        updated_iso=updated_iso,
        page_title=(
            f"MLBB Meta Right Now — {current_month} Live Tier List, "
            f"Top Heroes & Bans | {SITE_NAME}"
        ),
        page_desc=(
            f"What's the meta in MLBB right now? Live tier list updated "
            f"every 6 hours from real ranked matches. {top_name} is leading "
            f"the {current_month} meta — see the full top-5 win rate, most "
            f"banned and hidden-OP picks. Updated {updated}."
        ),
        page_keywords=(
            f"mlbb meta now, mlbb meta {current_month.lower()}, current mlbb "
            "meta, mlbb meta this week, mlbb best heroes right now, mobile "
            "legends meta now, mlbb top heroes today, mlbb most banned now, "
            "hidden op mlbb"
        ),
        canonical="/meta-now",
    )


# --------------------------------------------------------------------------- #
# About
# --------------------------------------------------------------------------- #
@bp.route("/about")
def about_page() -> str:
    from app import SITE_NAME  # lazy

    return render_template(
        "about.html",
        page_title=f"About {SITE_NAME} — Singapore's #1 MLBB Community",
        page_desc="SGS is Singapore's largest verified Mobile Legends community. Practice, scrim and rank up with real players.",
        page_keywords="singapore gaming syndicate, mlbb community singapore, join mlbb team singapore",
        canonical="/about",
        hide_cta_band=True,
    )
