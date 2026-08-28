"""
Shopee API Source Connector.

Implements §12.3 integration outline:
- HMAC-SHA256 request signing (shop-level endpoints)
- Proactive token refresh with single-flight asyncio.Lock
- Cursor-based product sync driven to exhaustion
- Checkpoint cursor per shop

References: Shopee Open Platform v2 (verify exact endpoint names at implementation time).
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Record Models ──────────────────────────────────────────────────────────


class ShopProductRecord(BaseModel):
    item_id: int
    shop_id: int
    name: str
    status: str
    price: Optional[float] = None
    stock: Optional[int] = None
    category_id: Optional[int] = None
    create_time: int
    update_time: int


class StockSnapshot(BaseModel):
    item_id: int
    shop_id: int
    stock: int
    price: float
    snapshotted_at: int  # Unix timestamp


# ── Token Store ────────────────────────────────────────────────────────────


class TokenStore:
    """
    Minimal in-memory token store.
    Production: replace with encrypted secret store (e.g. Vault, KMS).
    NEVER store tokens in config files or logs.
    """

    def __init__(self):
        self._tokens: Dict[int, Dict[str, Any]] = {}  # shop_id → {access_token, refresh_token, expires_at}

    def get(self, shop_id: int) -> Optional[Dict[str, Any]]:
        entry = self._tokens.get(shop_id)
        if entry and time.time() < entry["expires_at"] - 60:  # 60s safety margin
            return entry
        return None

    def set(self, shop_id: int, access_token: str, refresh_token: str, expires_in: int):
        self._tokens[shop_id] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
        }

    def get_refresh_token(self, shop_id: int) -> Optional[str]:
        entry = self._tokens.get(shop_id)
        return entry["refresh_token"] if entry else None


# ── Main Connector ─────────────────────────────────────────────────────────


class ShopeeApiSource:
    """
    Shopee Open Platform v2 connector (access_class: api).

    Constructor args:
      partner_id    int    — from Shopee partner app
      partner_key   str    — HMAC signing key (keep in secret store)
      host          str    — e.g. "https://partner.shopeemobile.com" (prod)
                             or  "https://partner.test-stable.shopeemobile.com" (sandbox)
      client        httpx.AsyncClient (optional, created if not passed)
    """

    def __init__(
        self,
        partner_id: int,
        partner_key: str,
        host: str = "https://partner.shopeemobile.com",
        client: Optional[httpx.AsyncClient] = None,
        token_store: Optional[TokenStore] = None,
    ):
        self.partner_id = partner_id
        self._partner_key = partner_key.encode("utf-8")
        self.host = host.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.token_store = token_store or TokenStore()
        # Per-shop refresh lock to prevent refresh storms
        self._refresh_locks: Dict[int, "asyncio.Lock"] = {}

    def _get_refresh_lock(self, shop_id: int):
        import asyncio
        if shop_id not in self._refresh_locks:
            self._refresh_locks[shop_id] = asyncio.Lock()
        return self._refresh_locks[shop_id]

    # ── Signing ────────────────────────────────────────────────────────────

    def sign_request(
        self, path: str, timestamp: int, access_token: str = "", shop_id: int = 0
    ) -> str:
        """
        HMAC-SHA256 over base string for shop-level endpoints.
        Base string: partner_id | path | timestamp | access_token | shop_id
        [VERIFY exact composition per endpoint class against current Open Platform docs]
        """
        base = f"{self.partner_id}{path}{timestamp}{access_token}{shop_id}"
        return hmac.new(self._partner_key, base.encode("utf-8"), hashlib.sha256).hexdigest()

    def _signed_params(
        self, path: str, access_token: str, shop_id: int
    ) -> Dict[str, Any]:
        """Build common signed query params."""
        ts = int(time.time())
        sign = self.sign_request(path, ts, access_token, shop_id)
        return {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "sign": sign,
            "access_token": access_token,
            "shop_id": shop_id,
        }

    # ── OAuth / Token Management ────────────────────────────────────────────

    def get_auth_url(self, redirect_uri: str) -> str:
        """
        Step 1: Generate the URL to redirect the shop owner to for authorization.
        [VERIFY exact path and params against current Open Platform docs]
        """
        ts = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = self.sign_request(path, ts)
        return (
            f"{self.host}{path}"
            f"?partner_id={self.partner_id}"
            f"&timestamp={ts}"
            f"&sign={sign}"
            f"&redirect={redirect_uri}"
        )

    async def exchange_code(self, code: str, shop_id: int) -> Dict[str, Any]:
        """
        Step 2: Exchange auth code for access_token + refresh_token.
        [VERIFY path and response fields against current Open Platform docs]
        """
        path = "/api/v2/auth/token/get"
        ts = int(time.time())
        sign = self.sign_request(path, ts)
        payload = {
            "code": code,
            "shop_id": shop_id,
            "partner_id": self.partner_id,
        }
        params = {"partner_id": self.partner_id, "timestamp": ts, "sign": sign}
        resp = await self.client.post(
            self.host + path, params=params, json=payload
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Shopee auth error: {data['error']} — {data.get('message')}")
        # Store tokens
        self.token_store.set(
            shop_id,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=data.get("expire_in", 14400),
        )
        return data

    async def get_access_token(self, shop_id: int) -> str:
        """
        Get a valid access token, proactively refreshing if needed.
        Uses a per-shop lock to prevent refresh storms.
        """
        entry = self.token_store.get(shop_id)
        if entry:
            return entry["access_token"]

        async with self._get_refresh_lock(shop_id):
            # Double-check after acquiring lock (another coroutine may have refreshed)
            entry = self.token_store.get(shop_id)
            if entry:
                return entry["access_token"]

            refresh_token = self.token_store.get_refresh_token(shop_id)
            if not refresh_token:
                raise RuntimeError(
                    f"No refresh token for shop {shop_id}. Run OAuth flow first."
                )
            return await self._do_refresh(shop_id, refresh_token)

    async def _do_refresh(self, shop_id: int, refresh_token: str) -> str:
        """
        Exchange a refresh_token for a new access_token.
        [VERIFY path and response fields against current Open Platform docs]
        """
        path = "/api/v2/auth/access_token/get"
        ts = int(time.time())
        sign = self.sign_request(path, ts)
        payload = {
            "refresh_token": refresh_token,
            "shop_id": shop_id,
            "partner_id": self.partner_id,
        }
        params = {"partner_id": self.partner_id, "timestamp": ts, "sign": sign}
        resp = await self.client.post(self.host + path, params=params, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(
                f"Shopee token refresh error: {data['error']} — {data.get('message')}"
            )
        new_token = data["access_token"]
        self.token_store.set(
            shop_id,
            access_token=new_token,
            refresh_token=data.get("refresh_token", refresh_token),
            expires_in=data.get("expire_in", 14400),
        )
        logger.info("Refreshed access token for shop %s", shop_id)
        return new_token

    # ── Product Sync ───────────────────────────────────────────────────────

    async def sync_products(
        self,
        shop_id: int,
        item_status: str = "NORMAL",
        page_size: int = 100,
        cursor: str = "",
    ) -> Tuple[List[Dict[str, Any]], str, bool]:
        """
        Cursor-paginated product list.
        Returns: (items, next_cursor, has_more)

        Callers should loop until has_more=False, checkpointing cursor each iteration.
        [VERIFY path, params, and response schema against current Open Platform docs]
        """
        access_token = await self.get_access_token(shop_id)
        path = "/api/v2/product/get_item_list"
        params = self._signed_params(path, access_token, shop_id)
        params.update({
            "offset": cursor or 0,
            "page_size": page_size,
            "item_status": item_status,
        })

        resp = await self.client.get(self.host + path, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RuntimeError(f"RATE_LIMITED: retry after {retry_after}s")
        resp.raise_for_status()
        data = resp.json()

        response_data = data.get("response", {})
        items = response_data.get("item", [])
        next_cursor = str(response_data.get("next_offset", ""))
        has_more = response_data.get("has_next_page", False)

        return items, next_cursor, has_more

    async def sync_all_products(
        self, shop_id: int, checkpoint_cursor: str = ""
    ):
        """
        Drive product sync to exhaustion, yielding pages.
        Checkpoint the cursor after each page to allow resume on restart.
        """
        cursor = checkpoint_cursor
        while True:
            items, next_cursor, has_more = await self.sync_products(
                shop_id, cursor=cursor
            )
            yield items, next_cursor
            if not has_more:
                break
            cursor = next_cursor

    async def get_item_detail(
        self, shop_id: int, item_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """
        Fetch detailed product info (price, stock, etc.) for a batch of item IDs.
        [VERIFY batch size limit against current Open Platform docs]
        """
        access_token = await self.get_access_token(shop_id)
        path = "/api/v2/product/get_item_base_info"
        params = self._signed_params(path, access_token, shop_id)
        params["item_id_list"] = ",".join(str(i) for i in item_ids)

        resp = await self.client.get(self.host + path, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", {}).get("item_list", [])
