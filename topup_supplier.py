"""
Top-up supplier abstraction + MooGold implementation.

Design goal: frontend + checkout logic stay identical regardless of backend
supplier (MooGold, Smile.one, UniPin, BangJeff). Each supplier is a thin
adapter that maps our canonical requests to their API.

Canonical interface (see `Supplier` protocol):
    list_products(game)        -> list[Product]
    validate_user(game, uid, sid) -> (ok, player_name)
    create_order(sku, uid, sid, partner_ref) -> order_id
    get_order(order_id)        -> OrderStatus

Env vars required for MooGold:
    MOOGOLD_PARTNER_ID   — your reseller partner ID
    MOOGOLD_SECRET_KEY   — your API secret (HMAC signing key)
    MOOGOLD_AUTH_USER    — basic-auth username (optional, some endpoints)
    MOOGOLD_AUTH_PASS    — basic-auth password (optional)
    MOOGOLD_SANDBOX=1    — use sandbox base URL when supported

Notes:
  * Real MooGold signing scheme is confirmed on credential delivery. We
    implement the most commonly documented variant (HMAC-SHA256 over
    timestamp + path + body) and wrap it in `_signed_post()` so swapping is
    a one-line change if they use a different scheme.
  * The `validate_user` call uses MooGold's `product/validate` endpoint —
    this lets us show the player's in-game name before charging (huge UX
    win, matches what codashop/zxshark do).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical domain types
# ---------------------------------------------------------------------------


@dataclass
class Product:
    """One purchasable SKU (denomination) for a given game."""

    sku: str            # supplier's internal SKU / product_id
    name: str           # "86 Diamonds"
    amount: int         # unit count (diamonds/UC/etc.)
    price_sgd: float    # our retail price in SGD (includes margin)
    cost_usd: float | None = None  # wholesale cost for reconciliation


@dataclass
class ValidateResult:
    ok: bool
    player_name: str | None = None
    error: str | None = None


@dataclass
class OrderResult:
    ok: bool
    supplier_order_id: str | None = None
    status: str = "unknown"   # "pending" | "processing" | "success" | "failed"
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Supplier(Protocol):
    name: str

    def list_products(self, game: str) -> list[Product]: ...
    def validate_user(self, game: str, user_id: str, server_id: str | None) -> ValidateResult: ...
    def create_order(
        self, *, sku: str, user_id: str, server_id: str | None, partner_ref: str
    ) -> OrderResult: ...
    def get_order(self, supplier_order_id: str) -> OrderResult: ...


# ---------------------------------------------------------------------------
# MooGold implementation
# ---------------------------------------------------------------------------


def _env(key: str, required: bool = True) -> str:
    v = os.getenv(key, "")
    if required and not v:
        raise RuntimeError(f"{key} not configured")
    return v


def _moogold_base() -> str:
    return "https://moogold.com/wp-json/v1/api"


# MLBB product IDs on MooGold — populated from their catalog (cached).
# We'll hydrate this via list_product() call and cache results; defaults here
# are placeholders matching MooGold's public MLBB SKU naming.
MLBB_PRODUCT_ID = os.getenv("MOOGOLD_MLBB_PRODUCT_ID", "1")  # MooGold uses numeric IDs


class MooGold:
    """Adapter for https://moogold.com reseller API."""

    name = "moogold"

    def _signed_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        POST to MooGold with auth + signature.
        Actual signature scheme per MooGold docs:
            timestamp = unix seconds
            string_to_sign = json(body) + timestamp + path
            signature = HMAC-SHA256(secret, string_to_sign).hex
        """
        secret = _env("MOOGOLD_SECRET_KEY").encode()
        partner = _env("MOOGOLD_PARTNER_ID")

        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
        ts = str(int(time.time()))
        to_sign = payload + ts + path
        sig = hmac.new(secret, to_sign.encode(), hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "timestamp": ts,
            "auth": sig,
        }
        # Some endpoints also require Basic Auth
        auth_user = os.getenv("MOOGOLD_AUTH_USER")
        auth_pass = os.getenv("MOOGOLD_AUTH_PASS")
        auth = (auth_user, auth_pass) if auth_user and auth_pass else None

        url = f"{_moogold_base()}{path}"
        log.info("moogold POST %s", path)
        with httpx.Client(timeout=20.0) as client:
            r = client.post(url, headers=headers, content=payload, auth=auth)
        if r.status_code >= 400:
            log.error("moogold %s -> %s: %s", path, r.status_code, r.text)
            r.raise_for_status()
        return r.json()

    # ---------- Supplier interface ----------

    def list_products(self, game: str = "mobile-legends") -> list[Product]:
        """Return available SKUs for the game."""
        # MooGold's product_detail returns the variations array.
        body = {"path": "product/product_detail", "product_id": MLBB_PRODUCT_ID}
        try:
            resp = self._signed_post("/product/product_detail", body)
        except Exception as e:
            log.error("moogold list_products failed: %s", e)
            return []

        # Response shape (per MooGold v1 docs):
        #   {"Variation": [{"variation_id": ..., "variation_name": "86 Diamonds", "variation_price": "1.25"}]}
        out: list[Product] = []
        for v in resp.get("Variation", []):
            name = v.get("variation_name", "")
            cost = float(v.get("variation_price", 0))
            # Extract diamond count from name (best effort)
            amount = 0
            for tok in name.split():
                if tok.isdigit():
                    amount = int(tok)
                    break
            # Retail = cost * 1.15 + 0.20 SGD padding for payment fees
            retail_sgd = round(cost * 1.35 + 0.20, 2)  # adjust margin here
            out.append(
                Product(
                    sku=str(v.get("variation_id", "")),
                    name=name,
                    amount=amount,
                    price_sgd=retail_sgd,
                    cost_usd=cost,
                )
            )
        return out

    def validate_user(
        self, game: str, user_id: str, server_id: str | None
    ) -> ValidateResult:
        """Verify the player ID exists in-game and return their display name."""
        body = {
            "path": "product/validate",
            "product_id": MLBB_PRODUCT_ID,
            "server": server_id or "",
            "user_id": user_id,
        }
        try:
            resp = self._signed_post("/product/validate", body)
        except Exception as e:
            return ValidateResult(ok=False, error=f"validation failed: {e}")

        if resp.get("status") == "success":
            return ValidateResult(ok=True, player_name=resp.get("username"))
        return ValidateResult(ok=False, error=resp.get("message", "Invalid user"))

    def create_order(
        self,
        *,
        sku: str,
        user_id: str,
        server_id: str | None,
        partner_ref: str,
    ) -> OrderResult:
        body = {
            "path": "order/create_order",
            "category": "1",   # 1 = game top-up
            "product-id": MLBB_PRODUCT_ID,
            "quantity": 1,
            "Partner_order_id": partner_ref,
            "User_ID": user_id,
            "Server": server_id or "",
            "variation_id": sku,
        }
        try:
            resp = self._signed_post("/order/create_order", body)
        except Exception as e:
            return OrderResult(ok=False, error=str(e))

        oid = resp.get("order_id") or resp.get("id")
        status = (resp.get("status") or "pending").lower()
        return OrderResult(
            ok=bool(oid),
            supplier_order_id=str(oid) if oid else None,
            status=status,
            raw=resp,
        )

    def get_order(self, supplier_order_id: str) -> OrderResult:
        body = {"path": "order/order_detail", "order_id": supplier_order_id}
        try:
            resp = self._signed_post("/order/order_detail", body)
        except Exception as e:
            return OrderResult(ok=False, error=str(e))

        status = (resp.get("status") or "unknown").lower()
        return OrderResult(
            ok=True,
            supplier_order_id=supplier_order_id,
            status=status,
            raw=resp,
        )


# ---------------------------------------------------------------------------
# Mock supplier — used when credentials aren't configured yet
# ---------------------------------------------------------------------------


class MockSupplier:
    """Returns fake successful data — swap in when you're building UI without API keys."""

    name = "mock"

    def list_products(self, game: str = "mobile-legends") -> list[Product]:
        return [
            Product(sku="mlbb-86",   name="86 Diamonds",   amount=86,   price_sgd=2.80),
            Product(sku="mlbb-172",  name="172 Diamonds",  amount=172,  price_sgd=5.40),
            Product(sku="mlbb-257",  name="257 Diamonds",  amount=257,  price_sgd=7.90),
            Product(sku="mlbb-344",  name="344 Diamonds",  amount=344,  price_sgd=10.50),
            Product(sku="mlbb-429",  name="429 Diamonds",  amount=429,  price_sgd=12.80),
            Product(sku="mlbb-706",  name="706 Diamonds",  amount=706,  price_sgd=20.90),
            Product(sku="mlbb-1412", name="1412 Diamonds", amount=1412, price_sgd=40.50),
            Product(sku="mlbb-2195", name="2195 Diamonds", amount=2195, price_sgd=62.00),
            Product(sku="mlbb-3688", name="3688 Diamonds", amount=3688, price_sgd=103.00),
            Product(sku="mlbb-5532", name="5532 Diamonds", amount=5532, price_sgd=155.00),
            Product(sku="mlbb-wp",   name="Weekly Pass",   amount=0,    price_sgd=4.20),
        ]

    def validate_user(self, game, user_id, server_id):
        if not user_id:
            return ValidateResult(ok=False, error="User ID required")
        return ValidateResult(ok=True, player_name=f"Player_{user_id[:4]}")

    def create_order(self, *, sku, user_id, server_id, partner_ref):
        return OrderResult(
            ok=True,
            supplier_order_id=f"mock-{uuid.uuid4().hex[:12]}",
            status="success",
        )

    def get_order(self, supplier_order_id: str) -> OrderResult:
        return OrderResult(ok=True, supplier_order_id=supplier_order_id, status="success")


# ---------------------------------------------------------------------------
# Factory — call this from routes
# ---------------------------------------------------------------------------


def get_supplier() -> Supplier:
    """
    Return the configured supplier. Priority:
      1. TOPUP_SUPPLIER env ("moogold" | "mock")
      2. If MOOGOLD_PARTNER_ID + MOOGOLD_SECRET_KEY present -> MooGold
      3. Otherwise -> MockSupplier
    """
    explicit = os.getenv("TOPUP_SUPPLIER", "").lower()
    if explicit == "moogold":
        return MooGold()
    if explicit == "mock":
        return MockSupplier()
    if os.getenv("MOOGOLD_PARTNER_ID") and os.getenv("MOOGOLD_SECRET_KEY"):
        return MooGold()
    log.warning("No topup supplier configured; using MockSupplier")
    return MockSupplier()
