"""Heroes blueprint — homepage, hero detail, tier list, role + lane pages.

Sprint 4 / Phase C extraction. The route-heavy template-rendering routes
that drive ~95% of organic search traffic to the site live here:

    /                       index           homepage / hero index
    /hero/<slug>            hero_page       per-hero deep guide
    /tier-list              tier_list_page  cross-verified tier list
    /role/<role>            role_page       role landing pages
    /lane/<lane>            lane_page       lane landing pages

Registered in app.py via:
    from blueprints.heroes import bp as heroes_bp
    app.register_blueprint(heroes_bp)

Lazy-imports app-level helpers and constants (`ROLE_COLORS`, `LANE_META`,
`ROLE_INTRO`, `hero_tips`, `build_hero_faqs`, `build_hero_pool`,
`get_counter_items`, `role_color`, `SITE_NAME`) so this module can be
imported by app.py during initialisation without a circular-import
explosion. By the time any of these routes actually fire, app.py has
finished defining all the helpers.

Future Sprint 5 cleanup may move some of those helpers into lib/ — at
which point the lazy imports here should be swapped for direct imports.
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, abort, render_template, request

from lib.openmlbb import (
    _EXECUTOR,
    cache_age_text,
    classify_counter,
    get_academy_builds,
    get_all_heroes,
    get_equipment_map,
    get_hero_combos,
    get_hero_compat,
    get_hero_counters,
    get_hero_detail,
    get_hero_stats,
    get_tier_list,
    make_cache_key,
    pct,
    primary_role,
    tier_from_winrate,
)

bp = Blueprint("heroes", __name__)


# --------------------------------------------------------------------------- #
# Homepage
# --------------------------------------------------------------------------- #
@bp.route("/")
def index() -> str:
    from app import SITE_NAME  # lazy — see module docstring

    heroes = get_all_heroes()
    tier_list = get_tier_list("all")
    # Enrich heroes with win rate from tier list where available
    wr_by_id = {h["id"]: h for h in tier_list}
    for h in heroes:
        wr = wr_by_id.get(h["id"])
        if wr:
            h["win_rate"] = wr["win_rate"]
            h["tier"] = wr["tier"]
    top_meta = tier_list[:10]

    # "Trending this patch" driven by ban rate — the single strongest market
    # signal for which heroes are considered overpowered. Research (April 2026)
    # shows Gloo, Julian, Kalea etc. with 60-80% ban rates in Mythic Glory.
    # Top-ranked MLBB guide sites (BitTopup, mlbbmeta.com) lead with this.
    catalog = {h["id"]: h for h in heroes}
    trending = sorted(
        [h for h in tier_list if h.get("ban_rate") is not None],
        key=lambda h: h.get("ban_rate") or 0,
        reverse=True,
    )[:6]
    for t in trending:
        meta = catalog.get(t["id"])
        if meta:
            t["role"] = meta.get("role")

    updated = cache_age_text("/api/heroes?size=200")
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "index.html",
        heroes=heroes,
        top_meta=top_meta,
        trending=trending,
        updated=updated,
        page_title=(
            f"MLBB Tier List {current_month} — Live Hero Guides & "
            f"Best Builds | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB tier list for {current_month}: {len(heroes)} heroes "
            f"ranked by real ranked win rate, with builds, counters & combos. "
            f"Singapore's #1 Mobile Legends community — join on Telegram."
        ),
        page_keywords=(
            f"mlbb tier list {current_month.lower()}, mobile legends tier "
            "list, best mlbb heroes, mlbb meta 2026, mobile legends singapore, "
            "hero build 2026, mlbb counter picks"
        ),
        canonical="/",
    )


# --------------------------------------------------------------------------- #
# Hero detail page
# --------------------------------------------------------------------------- #
@bp.route("/hero/<slug>")
def hero_page(slug: str) -> str:
    from app import build_hero_faqs, get_counter_items, hero_tips  # lazy

    heroes = get_all_heroes()
    hero = next((h for h in heroes if h["slug"] == slug), None)
    if not hero:
        abort(404)

    # Fan-out: all 6 downstream endpoints in parallel. On a cold cache this
    # drops first-view latency from ~sum(requests) to ~max(requests) — typically
    # 5x speed-up against the OpenMLBB API.
    hid = hero["id"]
    f_detail    = _EXECUTOR.submit(get_hero_detail,   hid)
    f_stats     = _EXECUTOR.submit(get_hero_stats,    hid)
    f_counters  = _EXECUTOR.submit(get_hero_counters, hid)
    f_synergies = _EXECUTOR.submit(get_hero_compat,   hid)
    f_combos    = _EXECUTOR.submit(get_hero_combos,   hid)
    f_builds    = _EXECUTOR.submit(get_academy_builds, hid)

    detail = f_detail.result()
    if not detail:
        abort(502)
    stats     = f_stats.result()     or {}
    counters  = (f_counters.result()  or [])[:3]
    synergies = (f_synergies.result() or [])[:3]
    combos    = f_combos.result()    or []
    builds    = f_builds.result()    or []

    # Counter items — derived from speciality tags + equipment catalogue
    counter_items = get_counter_items(detail, get_equipment_map())

    # Cross-source consensus for this hero (if available). The tier list
    # endpoint already runs the cross-check at warm time; we just look up
    # the cached entry here so the hero page can render the same badge
    # and editorial note as the tier list.
    tier_data = get_tier_list("all")
    cc_entry = next((h for h in tier_data if h["slug"] == slug), {})
    detail["editorial_note"]    = cc_entry.get("editorial_note", "")
    detail["conflict_severity"] = cc_entry.get("conflict_severity", "none")
    detail["consensus_tier"]    = cc_entry.get("consensus_tier", "")
    detail["confidence"]        = cc_entry.get("confidence", 0)

    # Resolve related hero names/slugs
    by_id = {h["id"]: h for h in heroes}
    def resolve(ids: list[int]) -> list[dict]:
        return [by_id[i] for i in ids if i in by_id][:3]

    strong_against = resolve(detail.get("relation_strong_ids") or [])
    weak_against = resolve(detail.get("relation_weak_ids") or [])
    works_with = resolve(detail.get("relation_assist_ids") or [])

    # Enrich counters/synergies with names
    for lst in (counters, synergies):
        for item in lst:
            meta = by_id.get(item.get("id"))
            if meta:
                item["name"] = meta["name"]
                item["slug"] = meta["slug"]
                item["head"] = item.get("head") or meta.get("head")
            else:
                item["name"] = f"Hero #{item.get('id')}"
                item["slug"] = ""

    # Tag each counter with hard/soft/minor classification — this is the single
    # strongest SEO/UX signal for counter guides (Boostroom/ONE Esports use it).
    for c in counters:
        c["strength"], c["strength_explain"] = classify_counter(c)
    counter_tally = {
        "hard":  sum(1 for c in counters if c.get("strength") == "hard"),
        "soft":  sum(1 for c in counters if c.get("strength") == "soft"),
        "minor": sum(1 for c in counters if c.get("strength") == "minor"),
    }

    role = primary_role(detail)
    tier = tier_from_winrate(stats.get("win_rate") or 0)
    tips = hero_tips(role, detail["name"])
    wr_pct = pct(stats.get("win_rate"))
    pr_pct = pct(stats.get("pick_rate"))
    br_pct = pct(stats.get("ban_rate"))

    name = detail["name"]
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    page_title = (
        f"{name} Guide {current_month} — Best Build, Counter & "
        f"Combo | SGS Singapore"
    )
    # Kept under Google's ~155-char limit even for long hero names like
    # "Yi Sun-shin" / "Popol and Kupa" — avoids mid-sentence SERP truncation.
    page_desc = (
        f"{name} MLBB guide for {current_month}: best build, emblem, spell, "
        f"combos and counters. Live {wr_pct} win rate from SG ranked. "
        f"Updated daily."
    )
    page_keywords = ", ".join([
        f"{name} guide", f"{name} build {current_month.lower()}",
        f"{name} mlbb", f"how to play {name}",
        f"{name} combo", f"{name} tips singapore",
        f"{name} counter", f"best {name} build", f"{name} emblem",
        f"mobile legends {name}",
    ])

    updated = cache_age_text(f"/api/heroes/{hero['id']}/stats?rank=all")

    faqs = build_hero_faqs(
        name=name,
        role=role,
        tier=tier,
        wr_pct=wr_pct,
        counters=counters,
        synergies=synergies,
        difficulty=detail.get("difficulty_label"),
        lanes=detail.get("lanes") or [],
    )

    # Related heroes — 6 same-role heroes for hero-to-hero internal linking.
    # Uses a slug-sorted rotation (not a top-N by WR) so every hero in the
    # role gets an equal share of inbound links from sibling pages, instead
    # of concentrating all crawl signal on the top-WR heroes. This is how
    # we move the long-tail hero pages out of "Discovered – currently not
    # indexed" in Search Console.
    same_role = sorted(
        (h for h in heroes if (h.get("role") or "").lower() == role.lower()),
        key=lambda h: h.get("slug") or "",
    )
    related_heroes: list[dict] = []
    if len(same_role) > 1:
        slugs = [h.get("slug") for h in same_role]
        try:
            self_idx = slugs.index(slug)
        except ValueError:
            self_idx = 0
        n = len(same_role)
        related_heroes = [
            same_role[(self_idx + 1 + i) % n]
            for i in range(min(6, n - 1))
        ]

    return render_template(
        "hero.html",
        hero=hero,
        detail=detail,
        stats=stats,
        counters=counters,
        counter_tally=counter_tally,
        counter_items=counter_items,
        synergies=synergies,
        combos=combos,
        builds=builds,
        strong_against=strong_against,
        weak_against=weak_against,
        works_with=works_with,
        role=role,
        tier=tier,
        tips=tips,
        wr_pct=wr_pct,
        pr_pct=pr_pct,
        br_pct=br_pct,
        updated=updated,
        faqs=faqs,
        related_heroes=related_heroes,
        page_title=page_title,
        page_desc=page_desc,
        page_keywords=page_keywords,
        canonical=f"/hero/{slug}",
        hide_cta_band=True,
    )


# --------------------------------------------------------------------------- #
# Role landing pages
# --------------------------------------------------------------------------- #
@bp.route("/role/<role>")
def role_page(role: str) -> str:
    """Role landing page — keyword-rich index of every hero in a class.
    Targets queries like 'best assassins mlbb 2026', 'top tank heroes', etc.
    """
    from app import ROLE_COLORS, ROLE_INTRO, build_hero_pool, role_color  # lazy

    role_norm = role.strip().lower()
    canonical_name = next(
        (r for r in ROLE_COLORS if r.lower() == role_norm), None
    )
    if not canonical_name:
        abort(404)

    all_heroes = get_tier_list("all")
    # get_all_heroes() gives us role+lane; tier list gives us win rate. Merge.
    catalog = {h["id"]: h for h in get_all_heroes()}
    filtered: list[dict] = []
    for h in all_heroes:
        meta = catalog.get(h["id"])
        if not meta:
            continue
        if (meta.get("role") or "").lower() == role_norm:
            merged = dict(meta)
            merged.update({
                "win_rate":  h.get("win_rate"),
                "pick_rate": h.get("pick_rate"),
                "ban_rate":  h.get("ban_rate"),
                "tier":      h.get("tier"),
            })
            filtered.append(merged)
    filtered.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)

    top10 = filtered[:10]
    updated = cache_age_text("/api/heroes?size=200")

    tier_buckets: dict[str, list[dict]] = {k: [] for k in ("SS", "S", "A", "B", "C", "D")}
    for h in filtered:
        tier_buckets.setdefault(h.get("tier") or "C", []).append(h)

    # 3-hero pool recommendation — the "comfort / counter / safe blind"
    # framework used by every high-ranking MLBB guide site (Boostroom,
    # ONE Esports). Gives users an actionable takeaway and packs in
    # long-tail keywords like "best blind pick tank mlbb".
    hero_pool = build_hero_pool(filtered)

    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "role.html",
        role=canonical_name,
        role_color=role_color(canonical_name),
        intro=ROLE_INTRO.get(role_norm, ""),
        heroes=filtered,
        top10=top10,
        tier_buckets=tier_buckets,
        hero_pool=hero_pool,
        updated=updated,
        page_title=(
            f"Best {canonical_name}s in MLBB {current_month} — Live Tier "
            f"List, Builds & Counters | SGS"
        ),
        page_desc=(
            f"Every Mobile Legends {canonical_name.lower()} hero ranked by "
            f"live win rate ({current_month}) — {len(filtered)} heroes, "
            f"updated every 6 hours from real ranked data. Find the best "
            f"{canonical_name.lower()} to climb to Mythic in 2026."
        ),
        page_keywords=(
            f"best {canonical_name.lower()} mlbb {current_month.lower()}, "
            f"top {canonical_name.lower()} mobile legends, "
            f"{canonical_name.lower()} tier list, mlbb "
            f"{canonical_name.lower()} guide singapore, best blind pick "
            f"{canonical_name.lower()}"
        ),
        canonical=f"/role/{role_norm}",
    )


# --------------------------------------------------------------------------- #
# Lane landing pages
# --------------------------------------------------------------------------- #
@bp.route("/lane/<lane>")
def lane_page(lane: str) -> str:
    """Lane landing page — targets queries like 'best jungler mlbb',
    'best mid laner mobile legends 2026', 'exp lane tier list'. Lanes cut
    *across* roles (a jungler might be Assassin, Fighter or even Tank), so
    this is a genuinely different axis of navigation from /role/*.
    """
    from app import LANE_META, build_hero_pool  # lazy

    slug = (lane or "").strip().lower()
    meta = LANE_META.get(slug)
    if not meta:
        abort(404)

    canonical_name = meta["canonical"]  # "Jungle" / "Mid Lane" / …
    label = meta["label"]                # "Junglers" / "Mid-laner" / …

    tier_data = get_tier_list("all")
    catalog = {h["id"]: h for h in get_all_heroes()}

    filtered: list[dict] = []
    for h in tier_data:
        hero_meta = catalog.get(h["id"])
        if not hero_meta:
            continue
        hero_lanes = hero_meta.get("lanes") or []
        if canonical_name not in hero_lanes:
            continue
        merged = dict(hero_meta)
        merged.update({
            "win_rate":  h.get("win_rate"),
            "pick_rate": h.get("pick_rate"),
            "ban_rate":  h.get("ban_rate"),
            "tier":      h.get("tier"),
        })
        filtered.append(merged)
    filtered.sort(key=lambda x: x.get("win_rate") or 0, reverse=True)

    top10 = filtered[:10]
    tier_buckets: dict[str, list[dict]] = {k: [] for k in ("SS", "S", "A", "B", "C", "D")}
    for h in filtered:
        tier_buckets.setdefault(h.get("tier") or "C", []).append(h)

    # Role breakdown — "Jungle is 60% assassins, 30% fighters, 10% tanks"
    # is exactly the kind of snippet that wins long-tail roles-per-lane searches.
    role_counts: dict[str, int] = {}
    for h in filtered:
        r = h.get("role") or "Unknown"
        role_counts[r] = role_counts.get(r, 0) + 1
    role_breakdown = sorted(role_counts.items(), key=lambda kv: -kv[1])

    # Same 3-hero pool framework as /role/* — works for lanes too.
    hero_pool = build_hero_pool(filtered)

    updated = cache_age_text("/api/heroes?size=200")
    current_month = datetime.now(timezone.utc).strftime("%B %Y")

    return render_template(
        "lane.html",
        lane_slug=slug,
        lane_name=canonical_name,
        lane_label=label,
        lane_color=meta["color"],
        intro=meta["intro"],
        tips=meta["tips"],
        heroes=filtered,
        top10=top10,
        tier_buckets=tier_buckets,
        role_breakdown=role_breakdown,
        hero_pool=hero_pool,
        updated=updated,
        page_title=(
            f"Best {label}s in MLBB {current_month} — "
            f"{canonical_name} Tier List, Builds & Tips | SGS"
        ),
        page_desc=(
            f"Every Mobile Legends {canonical_name.lower()} hero ranked "
            f"by live win rate ({current_month}) — {len(filtered)} heroes, "
            f"updated every 6 hours from real ranked data. Find the best "
            f"{canonical_name.lower()} pick to climb in 2026."
        ),
        page_keywords=(
            f"best {canonical_name.lower()} mlbb {current_month.lower()}, "
            f"top {label.lower()}s mobile legends, "
            f"{canonical_name.lower()} tier list, mlbb "
            f"{canonical_name.lower()} heroes, best {label.lower()} to climb, "
            f"{canonical_name.lower()} meta {current_month.lower()}"
        ),
        canonical=f"/lane/{slug}",
    )


# --------------------------------------------------------------------------- #
# Tier list page
# --------------------------------------------------------------------------- #
@bp.route("/tier-list")
def tier_list_page() -> str:
    from app import SITE_NAME  # lazy

    rank = request.args.get("rank", "all").lower()
    allowed = {"all", "epic", "legend", "mythic", "honor", "glory"}
    if rank not in allowed:
        rank = "all"
    heroes = get_tier_list(rank)

    # Bucket by consensus_tier (cross-checked across 4 sources) rather than
    # the raw OpenMLBB win-rate cutoff. The latter put high-WR but unpicked
    # niche heroes (Masha, Lolita) into SS while burying meta-defining picks
    # like Harley that have a slightly lower WR but heavy pick + ban presence.
    buckets = {"SS": [], "S": [], "A": [], "B": [], "C": [], "D": []}
    for h in heroes:
        buckets[h.get("consensus_tier") or h["tier"]].append(h)

    updated = cache_age_text(make_cache_key(
        "/api/heroes/rank",
        {"rank": rank, "days": 7, "sort_field": "win_rate", "size": 200},
    ))
    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    return render_template(
        "tier_list.html",
        rank=rank,
        buckets=buckets,
        total=len(heroes),
        updated=updated,
        page_title=(
            f"MLBB Tier List {current_month} — Cross-Verified, "
            f"SS–D Ranked Heroes | {SITE_NAME}"
        ),
        page_desc=(
            f"Live MLBB tier list for {current_month}: {len(heroes)} heroes "
            f"ranked SS through D by 4-source consensus — live ranked win "
            f"rate cross-checked against mlbb.gg, mlbbhub and mlbb.io. "
            f"Updated every 6 hours from the latest Epic–Mythic Glory games."
        ),
        page_keywords=(
            f"mlbb tier list {current_month.lower()}, mobile legends tier "
            f"list, best heroes mlbb, mlbb meta singapore, "
            f"{rank} tier list mlbb, ss tier mlbb, mlbb patch tier list"
        ),
        # Rank-specific canonical — treats each ?rank=… as a distinct page for
        # Google, so queries like "mlbb mythic tier list" resolve to the
        # mythic view rather than being merged into the default "all ranks".
        canonical=("/tier-list" if rank == "all" else f"/tier-list?rank={rank}"),
    )


# --------------------------------------------------------------------------- #
# Counter pages — /counter/<slug>
# --------------------------------------------------------------------------- #
# 132 dedicated landing pages, one per hero, targeting:
#   "[hero] counter" / "how to counter [hero]" / "best counter for [hero] mlbb"
# These are LONG-TAIL queries — low volume each, but cumulatively significant
# and weakly contested. Existing /hero/<slug> pages mention counters but they
# rank for "[hero] guide" / "[hero] build", not "counter [hero]". Splitting
# the intent into its own URL lets Google match the right page to the right
# query.
#
# Same data layer as hero_page (cached), no new upstream calls.

# Generic per-role strategic advice — the kind of content that turns a
# bare counter list into a real guide. Auto-selected from the target's role.
_COUNTER_STRATEGY: dict[str, list[str]] = {
    "Assassin": [
        "Build defensive items early — Athena's Shield, Radiant Armor or Antique Cuirass before your second damage item.",
        "Ward jungle entrances and river crossings to deny the gank setup assassins rely on.",
        "Group with your team — assassins thrive when carries are alone.",
        "Save your hard-CC for their dive — burst windows are short, lock them down before they get out.",
    ],
    "Mage": [
        "Force them out of mana — most mages run dry under sustained pressure.",
        "Stack magic resist (Athena's, Radiant Armor) in the second item slot.",
        "Dive when their key skills are on cooldown — most mages have a 6-15s rotation gap.",
        "Avoid clumping in lane — most mage damage is AoE.",
    ],
    "Marksman": [
        "Dive their backline early — pick assassins or fighters who can close the gap.",
        "Build attack-speed reduction (Dominance Ice) and burst — out-trade their DPS before they scale.",
        "Gank bot/gold lane before 7 minutes — marksmen need time to ramp.",
        "Ban their support if it's a peel-heavy pick (Estes, Mathilda, Floryn).",
    ],
    "Tank": [
        "Don't fight them 1v1 — focus their carries first.",
        "Build penetration items (Malefic Roar, Divine Glaive) so their defensive stacking matters less.",
        "Kite their initiation — most tanks have one big engage tool, dodge it and the fight is yours.",
        "Anti-heal items (Sea Halberd, Necklace of Durance) shut down regen-tank kits.",
    ],
    "Support": [
        "Catch them out of position when rotating — supports are squishy alone.",
        "Force fights when they're separated from their carry.",
        "Target their carry first to bait the support's CDs.",
        "Vision denial — sweep wards, deny their setup advantage.",
    ],
    "Fighter": [
        "Kite them with ranged heroes — most fighters are short-range.",
        "Build defensive items in mid-game (Blade Armor, Radiant Armor depending on damage type).",
        "Don't fight in narrow choke points — fighters thrive there.",
        "Anti-heal if they have lifesteal-based sustain (Yu Zhong, X.Borg, Alucard).",
    ],
}


@bp.route("/counter/<slug>")
def counter_page(slug: str) -> str:
    """Counter guide for a specific hero. One page per hero, targeting the
    long-tail "[hero] counter" / "how to counter [hero]" search cluster."""
    from app import SITE_NAME, get_counter_items  # lazy

    heroes = get_all_heroes()
    target = next((h for h in heroes if h["slug"] == slug), None)
    if not target:
        abort(404)

    hid = target["id"]

    # Parallel fan-out — same shape as hero_page, just trimmed to what we need.
    f_detail   = _EXECUTOR.submit(get_hero_detail, hid)
    f_stats    = _EXECUTOR.submit(get_hero_stats, hid)
    f_counters = _EXECUTOR.submit(get_hero_counters, hid)

    detail = f_detail.result()
    if not detail:
        abort(502)
    stats = f_stats.result() or {}
    raw_counters = f_counters.result() or []

    # Only keep entries with positive `increase` — those are the heroes
    # actually doing better than usual against the target. Negative-increase
    # entries are heroes the target counters, which belongs on a different
    # page concept. Then take top 8 — enough depth to feel comprehensive,
    # short enough to keep above the fold on mobile.
    by_id = {h["id"]: h for h in heroes}
    counters = []
    for c in raw_counters:
        meta = by_id.get(c.get("id"))
        if not meta:
            continue
        if (c.get("increase") or 0) <= 0:
            continue
        counters.append({
            **c,
            "name": meta["name"],
            "slug": meta["slug"],
            "head": c.get("head") or meta.get("head"),
            "role": meta.get("role"),
        })
    # Tag each with strength classification (hard / soft / minor).
    for c in counters:
        c["strength"], c["strength_explain"] = classify_counter(c)
    counters = counters[:8]

    role = primary_role(detail)
    name = target["name"]

    # Strategy bullets pulled from the role-keyed table above. Falls back
    # to the Fighter set since fighters cover the broadest play patterns.
    strategy_tips = _COUNTER_STRATEGY.get(role, _COUNTER_STRATEGY["Fighter"])

    # Reuse the existing item-counter logic — derives 4 items from
    # speciality tags + role.
    counter_items = get_counter_items(detail, get_equipment_map())

    wr_pct = pct(stats.get("win_rate"))
    pr_pct = pct(stats.get("pick_rate"))
    br_pct = pct(stats.get("ban_rate"))
    tier = tier_from_winrate(stats.get("win_rate") or 0)

    current_month = datetime.now(timezone.utc).strftime("%B %Y")
    updated = cache_age_text(f"/api/heroes/{hid}/stats?rank=all")

    top_counter = counters[0]["name"] if counters else None

    return render_template(
        "counter.html",
        target=target,
        detail=detail,
        name=name,
        role=role,
        tier=tier,
        wr_pct=wr_pct,
        pr_pct=pr_pct,
        br_pct=br_pct,
        counters=counters,
        counter_items=counter_items,
        strategy_tips=strategy_tips,
        top_counter=top_counter,
        updated=updated,
        current_month=current_month,
        page_title=(
            f"How to Counter {name} in MLBB ({current_month}) — "
            f"Best Counter Picks & Items | {SITE_NAME}"
        ),
        page_desc=(
            f"How to counter {name} in MLBB ({current_month}). "
            + (f"{top_counter} is the strongest counter pick. " if top_counter else "")
            + f"Live counter data from real ranked matches — top counter "
            f"heroes, items, and strategy. Updated {updated}."
        ),
        page_keywords=(
            f"{name} counter, how to counter {name}, {name} counter mlbb, "
            f"best counter for {name}, counter pick {name}, "
            f"how to beat {name} mlbb, {name} weakness, "
            f"who counters {name.lower()}, {name} counter {current_month.lower()}"
        ),
        canonical=f"/counter/{slug}",
        hide_cta_band=True,
    )
