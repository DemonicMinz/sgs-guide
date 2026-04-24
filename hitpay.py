"""
HitPay Payment Gateway Client — Singapore-focused PSP with native PayNow QR.

Docs: https://hitpay.com/docs

Flow:
  1. Frontend POSTs to /api/topup/checkout with {game, server_id, user_id, sku, email}
  2. Server validates, calls MooGold.validate() to verify player exists
  3. Server calls HitPay.create_payment() -> returns HitPay checkout URL
  4. User pays via PayNow QR / card
  5. HitPay calls our /api/topup/webhook with payment status
  6. Webhook verifies HMAC, triggers MooGold.create_order() for fulfillment

Env vars required:
  HITPAY_API_KEY         — live/sandbox API key
  HITPAY_SALT            — webhook HMAC secret ("Salt" in HitPay dashboard)
  HITPAY_SANDBOX=1       — set to use sandbox.hit-pay.com
  SITE_URL               — e.g. https://sgslah.com (for redirect/webhook URLs)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _base_url() -> str:
    return (
        "https://api.sandbox.hit-pay.com/v1"
        if os.getenv("HITPAY_SANDBOX") == "1"
        else "https://api.hit-pay.com/v1"
    )


def _api_key() -> str:
    k = os.getenv("HITPAY_API_KEY")
    if not k:
        raise RuntimeError("HITPAY_API_KEY not configured")
    return k


def _salt() -> str:
    s = os.getenv("HITPAY_SALT")
    if not s:
        raise RuntimeError("HITPAY_SALT not configured")
    return s


@dataclass
class HitPayPayment:
    """Subset of HitPay's payment-request response we actually need."""

    id: str          # HitPay payment request ID — pass back with orders for reconciliation
    status: str      # "pending" | "completed" | "failed" | ...
    url: str         # Checkout URL — redirect customer here
    amount: str      # Decimal string (e.g. "12.80")
    currency: str    # "SGD"

    @classmethod
    def from_response(cls, d: dict[str, Any]) -> "HitPayPayment":
        return cls(
            id=d["id"],
            status=d.get("status", "pending"),
            url=d["url"],
            amount=str(d.get("amount", "")),
            currency=d.get("currency", "SGD"),
        )


def create_payment(
    *,
    amount: float,
    reference: str,
    email: str,
    name: str | None = None,
    currency: str = "SGD",
    purpose: str = "MLBB Diamonds Top-Up",
    redirect_url: str | None = None,
    webhook_url: str | None = None,
) -> HitPayPayment:
    """
    Create a HitPay Payment Request (hosted checkout).
    Reference should be unique per order (we use our internal order_id).
    """
    site_url = os.getenv("SITE_URL", "https://sgslah.com").rstrip("/")
    payload = {
        "amount": f"{amount:.2f}",
        "currency": currency,
        "email": email,
        "purpose": purpose,
        "reference_number": reference,
        "redirect_url": redirect_url or f"{site_url}/topup/status/{reference}",
        "webhook": webhook_url or f"{site_url}/api/topup/webhook/hitpay",
        # PayNow is SG-native; leave payment_methods empty to accept card+PayNow
        "payment_methods": ["paynow_online", "card"],
    }
    if name:
        payload["name"] = name

    headers = {
        "X-BUSINESS-API-KEY": _api_key(),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    log.info("hitpay.create_payment ref=%s amount=%s", reference, payload["amount"])
    with httpx.Client(timeout=15.0) as client:
        r = client.post(f"{_base_url()}/payment-requests", data=payload, headers=headers)
    if r.status_code >= 400:
        log.error("hitpay error %s: %s", r.status_code, r.text)
        r.raise_for_status()
    return HitPayPayment.from_response(r.json())


def verify_webhook(form: dict[str, Any]) -> bool:
    """
    Verify HitPay webhook HMAC.
    HitPay posts x-www-form-urlencoded. Signature is HMAC-SHA256(salt, sorted_keyvals).
    """
    sig = form.get("hmac")
    if not sig:
        return False
    # HitPay rule: sort all other keys alphabetically, concatenate as k=v
    parts = [f"{k}{form[k]}" for k in sorted(form.keys()) if k != "hmac"]
    msg = "".join(parts).encode()
    expected = hmac.new(_salt().encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def get_payment(payment_id: str) -> dict[str, Any]:
    """Fetch a payment-request for status reconciliation."""
    headers = {
        "X-BUSINESS-API-KEY": _api_key(),
        "X-Requested-With": "XMLHttpRequest",
    }
    with httpx.Client(timeout=15.0) as client:
        r = client.get(f"{_base_url()}/payment-requests/{payment_id}", headers=headers)
    r.raise_for_status()
    return r.json()
