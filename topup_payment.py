"""
Flask blueprint for the top-up checkout flow.

Routes (all prefixed with /api/topup/):
  POST  /validate      — verify MLBB user_id / server exists, return player name
  POST  /checkout      — create internal order + HitPay payment, return redirect URL
  POST  /webhook/hitpay — HitPay server-to-server callback (payment status)
  GET   /status/<ref>  — poll our order by reference (used by status page)

Storage: JSON lines file at cache/topup_orders.jsonl (simple, append-only).
When volume grows, migrate to SQLite/Postgres.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import uuid
from typing import Any

from flask import Blueprint, abort, jsonify, request

from hitpay import create_payment, get_payment, verify_webhook
from topup_supplier import OrderResult, get_supplier

log = logging.getLogger(__name__)

bp = Blueprint("topup_api", __name__, url_prefix="/api/topup")

ORDERS_PATH = pathlib.Path("cache/topup_orders.jsonl")
ORDERS_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Order persistence (simple JSONL; swap for DB later)
# ---------------------------------------------------------------------------


def _order_write(order: dict[str, Any]) -> None:
    with ORDERS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(order, separators=(",", ":")) + "\n")


def _order_find(ref: str) -> dict[str, Any] | None:
    if not ORDERS_PATH.exists():
        return None
    found: dict[str, Any] | None = None
    with ORDERS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("ref") == ref:
                found = row  # last write wins (we append status updates)
    return found


def _order_update(ref: str, **fields: Any) -> dict[str, Any] | None:
    existing = _order_find(ref) or {}
    existing.update(fields)
    existing["updated_at"] = int(time.time())
    _order_write(existing)
    return existing


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.post("/validate")
def validate_user() -> Any:
    """Check MLBB player exists — called on Step 1 of checkout."""
    data = request.get_json(silent=True) or {}
    user_id = str(data.get("user_id", "")).strip()
    server_id = str(data.get("server_id", "")).strip()
    game = data.get("game", "mobile-legends")

    if not user_id or not server_id:
        return jsonify({"ok": False, "error": "user_id and server_id required"}), 400

    supplier = get_supplier()
    result = supplier.validate_user(game, user_id, server_id)
    return jsonify(
        {
            "ok": result.ok,
            "player_name": result.player_name,
            "error": result.error,
        }
    )


@bp.post("/checkout")
def checkout() -> Any:
    """
    Create internal order + HitPay payment-request.
    Body: {game, user_id, server_id, sku, email, name?}
    Returns: {ref, checkout_url, amount}
    """
    data = request.get_json(silent=True) or {}
    game = data.get("game", "mobile-legends")
    user_id = str(data.get("user_id", "")).strip()
    server_id = str(data.get("server_id", "")).strip()
    sku = str(data.get("sku", "")).strip()
    email = str(data.get("email", "")).strip()
    name = str(data.get("name", "")).strip() or None

    if not all([user_id, server_id, sku, email]):
        return jsonify({"ok": False, "error": "missing fields"}), 400

    supplier = get_supplier()

    # Find product + its retail price
    products = supplier.list_products(game)
    product = next((p for p in products if p.sku == sku), None)
    if not product:
        return jsonify({"ok": False, "error": "invalid sku"}), 400

    # Revalidate user to protect against stale client state
    v = supplier.validate_user(game, user_id, server_id)
    if not v.ok:
        return jsonify({"ok": False, "error": v.error or "invalid user"}), 400

    # Generate unique reference
    ref = f"sgs-{uuid.uuid4().hex[:16]}"

    # Persist pending order
    _order_write(
        {
            "ref": ref,
            "status": "pending_payment",
            "game": game,
            "user_id": user_id,
            "server_id": server_id,
            "sku": sku,
            "amount_sgd": product.price_sgd,
            "player_name": v.player_name,
            "email": email,
            "supplier": supplier.name,
            "created_at": int(time.time()),
        }
    )

    # Create HitPay payment
    try:
        payment = create_payment(
            amount=product.price_sgd,
            reference=ref,
            email=email,
            name=name,
            purpose=f"{product.name} — MLBB UID {user_id}",
        )
    except Exception as e:
        log.exception("HitPay payment creation failed")
        _order_update(ref, status="failed_to_create", error=str(e))
        return jsonify({"ok": False, "error": "payment gateway error"}), 502

    _order_update(ref, hitpay_id=payment.id)

    return jsonify(
        {
            "ok": True,
            "ref": ref,
            "checkout_url": payment.url,
            "amount": payment.amount,
            "currency": payment.currency,
        }
    )


@bp.post("/webhook/hitpay")
def hitpay_webhook() -> Any:
    """HitPay server-to-server notification — verify HMAC, fulfil if paid."""
    form = request.form.to_dict()
    log.info("hitpay webhook: %s", form)

    if not verify_webhook(form):
        log.warning("hitpay webhook HMAC failed: %s", form)
        abort(400)

    ref = form.get("reference_number", "")
    status = (form.get("status") or "").lower()
    payment_id = form.get("payment_id") or form.get("id")

    order = _order_find(ref)
    if not order:
        log.warning("unknown order ref from hitpay: %s", ref)
        return "", 200  # don't retry

    if status != "completed":
        _order_update(ref, status=f"hitpay_{status}", hitpay_payment_id=payment_id)
        return "", 200

    # Already processed? idempotency
    if order.get("status") in ("fulfilled", "supplier_pending"):
        return "", 200

    supplier = get_supplier()
    result: OrderResult = supplier.create_order(
        sku=order["sku"],
        user_id=order["user_id"],
        server_id=order["server_id"],
        partner_ref=ref,
    )

    _order_update(
        ref,
        status="fulfilled" if result.ok and result.status == "success" else "supplier_pending",
        hitpay_payment_id=payment_id,
        supplier_order_id=result.supplier_order_id,
        supplier_status=result.status,
        supplier_error=result.error,
    )

    return "", 200


@bp.get("/status/<ref>")
def order_status(ref: str) -> Any:
    """Used by the /topup/status/<ref> page to poll for final state."""
    order = _order_find(ref)
    if not order:
        return jsonify({"ok": False, "error": "not found"}), 404
    # Don't leak internal fields
    public = {
        k: order.get(k)
        for k in (
            "ref",
            "status",
            "amount_sgd",
            "player_name",
            "user_id",
            "sku",
            "supplier_status",
            "supplier_order_id",
            "created_at",
            "updated_at",
        )
        if k in order
    }
    return jsonify({"ok": True, "order": public})


@bp.get("/products")
def list_products() -> Any:
    """Frontend calls this to hydrate denomination buttons."""
    game = request.args.get("product", "mobile-legends")
    # map legacy "mobilelegends" -> "mobile-legends"
    if game == "mobilelegends":
        game = "mobile-legends"
    supplier = get_supplier()
    products = supplier.list_products(game)
    return jsonify(
        {
            "ok": True,
            "supplier": supplier.name,
            "products": [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "amount": p.amount,
                    "price_sgd": p.price_sgd,
                }
                for p in products
            ],
        }
    )
