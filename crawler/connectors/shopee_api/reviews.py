"""
Shopee Reviews Sub-connector (§12.6).

Implements:
- Forward cursor sync (by create_time descending until known review hit)
- 30-day rolling re-scan (catch edits, replies, removals)
- Disappearance handling: status → "removed", never hard-delete
- Buyer PII: buyer_ref stored as salted SHA-256 hash (§12.6, C22)
- content_hash over rating_star + comment_text + reply_text for edit tracking

[VERIFY exact endpoint names and versions against current Open Platform docs]
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .source import ShopeeApiSource

logger = logging.getLogger(__name__)


class ShopeeReviewsConnector:
    """
    Syncs buyer reviews/ratings for authorized shops via the Shopee Open Platform.
    Attached to a ShopeeApiSource for signing and token management.
    """

    REVIEW_TTL_MONTHS = 24  # raw review text retention (§12.6 PII policy)

    def __init__(self, source: ShopeeApiSource, buyer_hash_salt: str):
        """
        :param source:           Configured ShopeeApiSource (handles auth + signing)
        :param buyer_hash_salt:  Secret salt for hashing buyer identifiers (store in secret store)
        """
        self._source = source
        self._salt = buyer_hash_salt.encode("utf-8")

    # ── PII ───────────────────────────────────────────────────────────────

    def hash_buyer(self, buyer_id: str) -> str:
        """
        HMAC-SHA256 (salted) hash of buyer identifier.
        Purpose: aggregate analytics don't need the raw ID.
        If you need to contact a buyer, use the order reference instead.
        """
        h = hashlib.sha256(self._salt + buyer_id.encode("utf-8"))
        return h.hexdigest()

    # ── Review Fetch ──────────────────────────────────────────────────────

    async def fetch_reviews(
        self,
        shop_id: int,
        item_id: int,
        cursor_offset: int = 0,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int, bool]:
        """
        Fetch a page of reviews for one item.
        Returns: (reviews, next_offset, has_more)

        [VERIFY path, params, response schema against current Open Platform docs]
        """
        access_token = await self._source.get_access_token(shop_id)
        path = "/api/v2/product/get_comment"
        params = self._source._signed_params(path, access_token, shop_id)
        params.update({
            "item_id": item_id,
            "offset": cursor_offset,
            "limit": page_size,
        })

        resp = await self._source.client.get(self._source.host + path, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            raise RuntimeError(f"RATE_LIMITED: retry after {retry_after}s")
        resp.raise_for_status()
        data = resp.json()

        response_data = data.get("response", {})
        reviews = response_data.get("item_comment_list", [])
        next_offset = response_data.get("next_offset", 0)
        has_more = response_data.get("has_next_page", False)

        return reviews, next_offset, has_more

    async def forward_sync(
        self, shop_id: int, item_id: int, known_review_ids: set
    ):
        """
        Forward sync: cursor descending by create_time until we hit a known review.
        Yields processed ShopReview records.
        """
        offset = 0
        while True:
            reviews, next_offset, has_more = await self.fetch_reviews(
                shop_id, item_id, cursor_offset=offset
            )
            for raw in reviews:
                review_id = str(raw.get("comment_id") or raw.get("review_id", ""))
                if review_id in known_review_ids:
                    # Hit a review we've already processed → stop descending
                    return
                yield self.process_review(raw, shop_id)

            if not has_more:
                break
            offset = next_offset

    async def rolling_rescan(
        self, shop_id: int, item_id: int, days: int = 30
    ):
        """
        Re-read reviews from the last `days` days to catch:
        - Buyer edits
        - Seller replies added later
        - Shopee-removed reviews (disappearance handling)

        Yields processed ShopReview records for comparison against stored versions.
        """
        cutoff_ts = int(time.time()) - days * 86400
        offset = 0
        while True:
            reviews, next_offset, has_more = await self.fetch_reviews(
                shop_id, item_id, cursor_offset=offset
            )
            for raw in reviews:
                # create_time is Unix epoch
                create_time = raw.get("create_time", 0)
                if create_time and create_time < cutoff_ts:
                    return  # past our window → stop
                yield self.process_review(raw, shop_id)

            if not has_more:
                break
            offset = next_offset

    def process_review(self, raw: Dict[str, Any], shop_id: int) -> Dict[str, Any]:
        """
        Transform raw API response → normalized ShopReview record.
        Hashes buyer_ref, computes content_hash for change detection.
        """
        review_id = str(raw.get("comment_id") or raw.get("review_id", ""))
        item_id = raw.get("item_id", 0)
        model_id = raw.get("model_id")
        rating_star = raw.get("rating_star", 0)
        comment_text = raw.get("comment", "") or ""
        create_time = raw.get("create_time", 0)
        edit_time = raw.get("comment_update_time") or raw.get("update_time", 0)

        # Reply fields
        reply = raw.get("reply") or {}
        reply_text = reply.get("reply", "") or ""
        reply_time = reply.get("create_time", 0)

        # Buyer identity — hash for PII compliance
        buyer_id = str(raw.get("userid", "") or raw.get("buyer_id", ""))
        buyer_ref = self.hash_buyer(buyer_id) if buyer_id else ""

        # Media
        media = [
            {"type": "image", "url": url}
            for url in (raw.get("images") or [])
        ]

        # Status: Shopee may omit removed reviews from results
        # Caller sets status="removed" when a previously known review disappears
        status = "visible"
        if raw.get("hidden", False):
            status = "hidden"

        # Content hash for edit tracking
        content_hash = hashlib.sha256(
            f"{rating_star}|{comment_text}|{reply_text}".encode("utf-8")
        ).hexdigest()

        return {
            "review_id": review_id,
            "shop_id": shop_id,
            "item_id": item_id,
            "model_id": model_id,
            "rating_star": rating_star,
            "comment_text": comment_text,
            "media": media,
            "buyer_ref": buyer_ref,
            "create_time": create_time,
            "edit_time": edit_time,
            "reply_text": reply_text,
            "reply_time": reply_time,
            "status": status,
            "content_hash": content_hash,
            "fetched_at": int(time.time()),
            "dedup_key": f"{shop_id}:{review_id}",
        }

    def mark_removed(self, stored_review: Dict[str, Any]) -> Dict[str, Any]:
        """
        Mark a review as removed when it disappears from the API.
        NEVER hard-delete — status → 'removed', last_seen_at frozen.
        """
        updated = dict(stored_review)
        updated["status"] = "removed"
        # last_seen_at is intentionally NOT updated → preserved at last real observation
        return updated
