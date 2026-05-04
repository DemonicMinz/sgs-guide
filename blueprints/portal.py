"""SGS — MLBB verification portal blueprint.

Routes:
    GET  /register          — verification UI. Reached via Telegram login_url
                              button on @SGS_MLBOT, which appends a signed
                              payload (id, first_name, ..., hash). We verify
                              the HMAC and trust the resulting telegram_id.
    POST /api/send-vc       — proxies OpenMLBB /auth/send-vc (mails the code).
    POST /api/verify        — proxies /auth/login + /user/info, then notifies
                              the ml_bot internal webhook with the verified
                              profile + JWT, so the bot can unmute the user.

Replaces the standalone Flask app that used to live at
`ArlottBot/sgs_portal/` on port 8080. With sgslah.com on HTTPS, this lives
under the main site, the bot's `login_url` button works, and we don't need
a separate service.

Required env vars:
    BOT_TOKEN          @SGS_MLBOT's token (used as HMAC secret per
                       https://core.telegram.org/widgets/login#checking-authorization)
    BOT_WEBHOOK_URL    e.g. http://127.0.0.1:8090/webhook/register
    WEBHOOK_SECRET     shared secret with ml_bot (must match)
    OPENMLBB_BASE      defaults to https://openmlbb.fastapicloud.dev
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict, deque
from functools import wraps

import httpx
from flask import Blueprint, abort, jsonify, render_template, request

bp = Blueprint("portal", __name__)
log = logging.getLogger("portal")

# --------------------------------------------------------------------------- #
# Config (read at request time so .env reloads work without restart in dev)
# --------------------------------------------------------------------------- #
def _bot_token() -> str:
    return os.getenv("BOT_TOKEN", "").strip()

def _bot_webhook_url() -> str:
    return os.getenv("BOT_WEBHOOK_URL", "http://127.0.0.1:8090/webhook/register").strip()

def _webhook_secret() -> str:
    return os.getenv("WEBHOOK_SECRET", "sgs_internal_secret_2026").strip()

def _openmlbb_base() -> str:
    return os.getenv("OPENMLBB_BASE", "https://openmlbb.fastapicloud.dev").rstrip("/")


UPSTREAM_MSG = (
    "⚠️ MLBB verification servers are busy right now. "
    "Please wait a few minutes and try again."
)
# Telegram login data older than this is rejected (replay protection).
LOGIN_MAX_AGE_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------------- #
# Telegram login_url HMAC validation
# --------------------------------------------------------------------------- #
# https://core.telegram.org/widgets/login#checking-authorization
#
# Telegram appends to the URL: id, first_name, last_name, username, photo_url,
# auth_date, hash. We rebuild the data_check_string (sorted "k=v\nk=v\n…",
# excluding `hash`), HMAC-sign it with sha256(bot_token).digest() as the key,
# and compare. Constant-time compare to avoid timing attacks. If anything is
# off — bad signature, missing fields, stale timestamp — we refuse.
def verify_telegram_login(args: dict) -> int | None:
    """Return the verified telegram_id, or None if the payload is invalid."""
    token = _bot_token()
    if not token:
        log.error("BOT_TOKEN not configured — refusing all Telegram logins")
        return None

    received_hash = args.get("hash", "")
    if not received_hash:
        return None

    # Build data_check_string: every field except `hash`, sorted by key,
    # joined with newlines.
    fields = {k: v for k, v in args.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))

    secret_key = hashlib.sha256(token.encode()).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        log.warning("HMAC mismatch on /register login payload")
        return None

    # Reject stale payloads — limits the window for a leaked URL to be reused.
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or (time.time() - auth_date) > LOGIN_MAX_AGE_SECONDS:
        log.warning("Stale auth_date on /register login payload")
        return None

    try:
        return int(fields.get("id", "0")) or None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Rate limiting — 5 requests / IP / minute on the verify endpoints. Same
# limits the standalone portal had. In-memory only, fine for one Gunicorn
# worker; if WORKERS > 1, swap for Redis later.
# --------------------------------------------------------------------------- #
_rate_buckets: dict[str, deque] = defaultdict(deque)
RATE_LIMIT = 5
RATE_WINDOW = 60


def rate_limited(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.headers.get(
            "X-Forwarded-For", request.remote_addr or "unknown"
        ).split(",")[0].strip()
        now = time.time()
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return jsonify(
                {"ok": False, "error": "Too many requests. Please wait a minute."}
            ), 429
        bucket.append(now)
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# OpenMLBB helpers
# --------------------------------------------------------------------------- #
def _is_upstream_failure(resp_text: str, payload: dict | None = None) -> bool:
    blob = (resp_text or "") + " " + str(payload or "")
    markers = (
        "UPSTREAM_REQUEST_FAILED", "upstream request failed", "Bad Gateway",
        "Service Unavailable", "Gateway Timeout", "upstream connect error",
    )
    return any(m.lower() in blob.lower() for m in markers)


def rank_from_level(rank_level: int) -> str:
    if rank_level <= 10:  return "Warrior"
    if rank_level <= 25:  return "Elite"
    if rank_level <= 45:  return "Master"
    if rank_level <= 75:  return "Grandmaster"
    if rank_level <= 105: return "Epic"
    if rank_level <= 135: return "Legend"
    if rank_level <= 160: return "Mythic"
    if rank_level <= 185: return "Mythical Honor"
    if rank_level <= 235: return "Mythical Glory"
    return "Mythical Immortal"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@bp.route("/register")
def register_page():
    """Verification UI. Reached via the bot's login_url button — Telegram
    appends a signed payload identifying the user. We verify it and pass the
    trusted telegram_id to the template."""
    tid = verify_telegram_login(request.args.to_dict())
    if tid is None:
        # Most common cause: user opened the URL directly without going via
        # the bot button. Show a friendly message instead of a 400 wall.
        return render_template(
            "register.html",
            error=(
                "This page must be opened via the SGS bot. "
                "Open Telegram, find @SGS_MLBOT, and tap the Verify button."
            ),
            tid="",
        ), 400
    return render_template("register.html", tid=str(tid), error="")


@bp.route("/api/send-vc", methods=["POST"])
@rate_limited
def api_send_vc():
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tid", "")).strip()
    role_id = str(data.get("role_id", "")).strip()
    zone_id = str(data.get("zone_id", "")).strip()

    if not tid.isdigit():
        return jsonify({"ok": False, "error": "Invalid Telegram ID"}), 400
    if not role_id.isdigit() or not zone_id.isdigit():
        return jsonify(
            {"ok": False, "error": "Player ID and Zone ID must be numbers"}
        ), 400

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{_openmlbb_base()}/api/user/auth/send-vc",
                json={"role_id": role_id, "zone_id": zone_id},
            )
            raw = r.text
            try:
                payload = r.json()
            except Exception:
                payload = {}
            if _is_upstream_failure(raw, payload):
                log.warning("send-vc upstream failure: %s", raw[:300])
                return jsonify(
                    {"ok": False, "upstream": True, "error": UPSTREAM_MSG}
                ), 503
    except Exception:
        log.exception("send-vc failed")
        return jsonify({"ok": False, "upstream": True, "error": UPSTREAM_MSG}), 503

    if payload.get("code") != 0:
        return jsonify(
            {"ok": False,
             "error": payload.get("msg") or "Failed to send verification code. "
                                            "Check your Player ID / Zone ID."}
        ), 400

    log.info("VC sent tid=%s role=%s zone=%s", tid, role_id, zone_id)
    return jsonify({"ok": True, "msg": "Verification code sent to your in-game mailbox."})


@bp.route("/api/verify", methods=["POST"])
@rate_limited
def api_verify():
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tid", "")).strip()
    role_id = str(data.get("role_id", "")).strip()
    zone_id = str(data.get("zone_id", "")).strip()
    vc = str(data.get("vc", "")).strip()

    if not tid.isdigit():
        return jsonify({"ok": False, "error": "Invalid Telegram ID"}), 400
    if not role_id.isdigit() or not zone_id.isdigit():
        return jsonify({"ok": False, "error": "Invalid Player ID / Zone ID"}), 400
    if not vc or len(vc) < 4:
        return jsonify({"ok": False, "error": "Invalid verification code"}), 400

    try:
        with httpx.Client(timeout=20.0) as client:
            # Step 1: exchange (role_id, zone_id, vc) for a JWT.
            login_url = f"{_openmlbb_base()}/api/user/auth/login"
            login_body = {"role_id": role_id, "zone_id": zone_id, "vc": vc}
            log.info("[verify] POST %s body=%s", login_url, login_body)
            r = client.post(login_url, json=login_body)
            log.info("[verify] login status=%s body=%s", r.status_code, r.text[:1000])
            raw = r.text
            try:
                login = r.json()
            except Exception:
                login = {}
            if _is_upstream_failure(raw, login):
                return jsonify(
                    {"ok": False, "upstream": True, "error": UPSTREAM_MSG}
                ), 503
            if not login:
                return jsonify(
                    {"ok": False, "error": f"Login returned non-JSON: {raw[:200]}"}
                ), 502
            if login.get("code") != 0:
                return jsonify(
                    {"ok": False, "error": login.get("msg") or "Invalid verification code."}
                ), 400

            # OpenMLBB returns the JWT under "jwt"; "token" is a non-JWT
            # session id and is not interchangeable.
            data_block = login.get("data") or {}
            token = (
                data_block.get("jwt")
                or data_block.get("token")
                or data_block.get("access_token")
                or login.get("jwt")
            )
            log.info("[verify] extracted token=%s",
                     (token[:30] + "…") if token else None)
            if not token:
                return jsonify(
                    {"ok": False, "error": f"Login ok but no token in response: {login}"}
                ), 502

            # Step 2: pull profile (name, level, rank).
            info_url = f"{_openmlbb_base()}/api/user/info"
            r = client.get(info_url, headers={"Authorization": f"Bearer {token}"})
            log.info("[verify] info status=%s body=%s", r.status_code, r.text[:1000])
            raw = r.text
            try:
                info = r.json()
            except Exception:
                info = {}
            if _is_upstream_failure(raw, info):
                return jsonify(
                    {"ok": False, "upstream": True, "error": UPSTREAM_MSG}
                ), 503
            if not info:
                return jsonify(
                    {"ok": False, "error": f"Profile returned non-JSON: {raw[:200]}"}
                ), 502
            if info.get("code") != 0:
                msg = info.get("msg") or info.get("message") or "Failed to fetch profile."
                log.warning("[verify] info non-zero code: %s", info)
                return jsonify({"ok": False, "error": f"{msg} (raw: {info})"}), 502

            profile = info.get("data") or {}
            log.info("[verify] profile=%s", profile)
    except Exception:
        log.exception("verify failed")
        return jsonify({"ok": False, "upstream": True, "error": UPSTREAM_MSG}), 503

    ign = profile.get("name") or "Unknown"
    level = int(profile.get("level") or 0)
    rank_level = int(profile.get("rank_level") or 0)
    rank = rank_from_level(rank_level)

    # Notify ml_bot. We include the JWT so /profile, /update, /sendupdate can
    # call OpenMLBB on the user's behalf — the standalone portal omitted this,
    # which is why /sendupdate exists as a "re-verify to get stats" workaround.
    # rank_level is forwarded so the bot can render "Mythic 14★" style displays
    # without re-querying the API.
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                _bot_webhook_url(),
                json={
                    "telegram_id": int(tid),
                    "ign": ign,
                    "rank": rank,
                    "rank_level": rank_level,
                    "level": level,
                    "mlbb_id": role_id,
                    "zone_id": zone_id,
                    "jwt": token,
                    "secret": _webhook_secret(),
                },
            )
    except Exception as e:
        log.warning("bot webhook failed: %s", e)
        # Don't block the user — verification still succeeded.

    log.info("verified tid=%s ign=%s rank=%s level=%s", tid, ign, rank, level)
    return jsonify({
        "ok": True,
        "ign": ign,
        "rank": rank,
        "level": level,
        "mlbb_id": role_id,
        "zone_id": zone_id,
    })
