"""
Tiki Commerce Connector.
Uses official/public catalog endpoints with polite rate-limiting, error handling,
and zero bypass techniques.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from ..interfaces import (
    CaptchaChallengeException,
    ProductIdentity,
    ProductNotFoundException,
    ProductSnapshot,
    RateLimitException,
    ReviewItem,
    ReviewPage,
)
from .session_base import BrowserSessionCommerceConnector

logger = logging.getLogger(__name__)


class TikiCommerceConnector(BrowserSessionCommerceConnector):
    """Compliant connector for Tiki.vn catalog and reviews."""

    BASE_PRODUCT_API = "https://tiki.vn/api/v2/products"
    BASE_REVIEWS_API = "https://tiki.vn/api/v2/reviews"

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client

    @property
    def platform_name(self) -> str:
        return "tiki"

    async def resolve_product(self, url: str) -> ProductIdentity:
        """Parses Tiki URL to extract clean product_id and optional spid."""
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Match pattern: ...-p1234567.html or /p1234567.html
        match = re.search(r"-p(\d+)\.html", path) or re.search(r"/p(\d+)\.html", path) or re.search(r"p(\d+)", path)
        if not match:
            raise ValueError(f"Could not extract Tiki product ID from URL: {url}")

        product_id = match.group(1)
        sku_id = query.get("spid", [None])[0]

        canonical = f"https://tiki.vn/product-p{product_id}.html"
        if sku_id:
            canonical += f"?spid={sku_id}"

        return ProductIdentity(
            platform=self.platform_name,
            product_id=product_id,
            sku_id=sku_id,
            canonical_url=canonical,
        )

    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        """Fetches product details from Tiki public catalog API."""
        endpoint = f"{self.BASE_PRODUCT_API}/{identity.product_id}"
        params: Dict[str, Any] = {"platform": "web"}
        if identity.sku_id:
            params["spid"] = identity.sku_id

        async with self._get_client() as client:
            try:
                response = await client.get(endpoint, params=params, headers=self.DEFAULT_HEADERS)
            except httpx.HTTPError as exc:
                logger.error(f"[tiki] Network request failed for {endpoint}: {exc}")
                raise

            # Safety check: inspect status and challenge indicators
            self.check_for_challenges_or_auth(str(response.url), response.status_code, response.text)

            if response.status_code == 404:
                raise ProductNotFoundException(f"[tiki] Product {identity.product_id} not found (404).")
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 60.0))
                raise RateLimitException("tiki", retry_after=retry_after)
            if response.status_code != 200:
                raise ProductNotFoundException(f"[tiki] Unexpected status code {response.status_code}: {response.text}")

            data = response.json()
            return self._parse_product_data(data, identity)

    async def fetch_reviews(self, identity: ProductIdentity, cursor: Optional[str] = None) -> ReviewPage:
        """Fetches paginated reviews from Tiki reviews API."""
        page = int(cursor) if cursor and cursor.isdigit() else 1
        params = {
            "product_id": identity.product_id,
            "include": "comments",
            "page": page,
            "limit": 20,
        }
        if identity.sku_id:
            params["spid"] = identity.sku_id

        async with self._get_client() as client:
            response = await client.get(self.BASE_REVIEWS_API, params=params, headers=self.DEFAULT_HEADERS)
            self.check_for_challenges_or_auth(str(response.url), response.status_code, response.text)

            if response.status_code == 429:
                raise RateLimitException("tiki", retry_after=60.0)
            if response.status_code != 200:
                return ReviewPage(product_id=identity.product_id, reviews=[], has_next=False)

            result = response.json()
            items_raw = result.get("data", [])
            reviews: list[ReviewItem] = []

            for item in items_raw:
                media_urls = [img.get("full_path") or img.get("url") for img in item.get("images", []) if isinstance(img, dict)]
                media_urls = [u for u in media_urls if u]

                dt = None
                created_at_ts = item.get("created_at")
                if isinstance(created_at_ts, (int, float)):
                    dt = datetime.fromtimestamp(created_at_ts, tz=timezone.utc)

                reviews.append(
                    ReviewItem(
                        review_id=str(item.get("id", "")),
                        author=item.get("created_by", {}).get("name") if isinstance(item.get("created_by"), dict) else None,
                        rating=item.get("rating", 5),
                        content=item.get("content"),
                        created_at=dt,
                        media_urls=media_urls,
                    )
                )

            paging = result.get("paging", {})
            current_page = paging.get("current_page", page)
            last_page = paging.get("last_page", page)
            has_next = current_page < last_page
            next_cursor = str(current_page + 1) if has_next else None

            return ReviewPage(
                product_id=identity.product_id,
                reviews=reviews,
                next_cursor=next_cursor,
                has_next=has_next,
            )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    def _parse_product_data(self, data: Dict[str, Any], identity: ProductIdentity) -> ProductSnapshot:
        current_price = float(data.get("price", 0.0))
        original_price = float(data["original_price"]) if data.get("original_price") is not None else None
        discount_rate = float(data["discount_rate"]) if data.get("discount_rate") is not None else None

        # Stock determination
        inv_status = data.get("inventory_status", "available")
        in_stock = inv_status != "out_of_stock"
        stock_item = data.get("stock_item") or {}
        stock_qty = stock_item.get("qty") if isinstance(stock_item, dict) else None

        # Seller
        seller = data.get("current_seller") or {}
        seller_name = seller.get("name") if isinstance(seller, dict) else None

        # Image
        images = data.get("images") or []
        img_url = images[0].get("base_url") if images and isinstance(images[0], dict) else None

        return ProductSnapshot(
            product_id=identity.product_id,
            platform=self.platform_name,
            title=data.get("name", ""),
            current_price=current_price,
            original_price=original_price,
            discount_rate=discount_rate,
            currency="VND",
            in_stock=in_stock,
            stock_quantity=stock_qty,
            rating_score=float(data["rating_average"]) if data.get("rating_average") is not None else None,
            review_count=int(data["review_count"]) if data.get("review_count") is not None else None,
            seller_name=seller_name,
            image_url=img_url,
            url=identity.canonical_url,
            raw_attributes={
                "sku": data.get("sku"),
                "brand": data.get("brand", {}).get("name") if isinstance(data.get("brand"), dict) else None,
                "categories": data.get("categories", {}).get("name") if isinstance(data.get("categories"), dict) else None,
            },
        )
