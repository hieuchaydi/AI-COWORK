"""
Extension Queue Commerce Connector Bridge.
Delegates heavy anti-bot protected sites to the user's real Chrome extension.
Ensures zero CDP-fingerprint detection risk and respects human-in-the-loop verification.
"""

import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from ..interfaces import (
    CaptchaChallengeException,
    CommerceConnector,
    ProductIdentity,
    ProductSnapshot,
    ReviewPage,
)

logger = logging.getLogger(__name__)


class ExtensionQueueCommerceConnector(CommerceConnector):
    """
    Connector using the local helper ingest queue (http://127.0.0.1:8766/ingest).
    The user's real browser extension executes the read with authentic cookies.
    """

    INGEST_JOB_URL = "http://127.0.0.1:8766/ingest/job"
    INGEST_RESULT_URL = "http://127.0.0.1:8766/ingest/result"

    def __init__(self, platform: str = "shopee", timeout_seconds: float = 30.0):
        self._platform = platform
        self._timeout_seconds = timeout_seconds

    @property
    def platform_name(self) -> str:
        return self._platform

    async def resolve_product(self, url: str) -> ProductIdentity:
        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Extract ID heuristic
        parts = [p for p in parsed.path.split("/") if p]
        product_id = parts[-1] if parts else "unknown"

        return ProductIdentity(
            platform=self.platform_name,
            product_id=product_id,
            canonical_url=clean_url,
        )

    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        """Submits job to local ingest queue and waits for real extension execution."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # 1. Enqueue job
                post_resp = await client.get(self.INGEST_JOB_URL, params={"url": identity.canonical_url})
                if post_resp.status_code != 200:
                    raise RuntimeError(f"Failed to enqueue ingest job: {post_resp.text}")
                job_data = post_resp.json()
                job_id = job_data.get("id") or job_data.get("job_id")
                if not job_id:
                    raise RuntimeError(f"Ingest queue returned no job ID: {job_data}")
            except httpx.ConnectError:
                raise RuntimeError(
                    "Local helper (http://127.0.0.1:8766) is not running. "
                    "Start AI-COWORK helper to use extension queue."
                )

            # 2. Poll for completion
            start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < self._timeout_seconds:
                await asyncio.sleep(2.0)
                res_resp = await client.get(self.INGEST_RESULT_URL, params={"id": job_id})
                if res_resp.status_code != 200:
                    continue

                res_data = res_resp.json()
                if res_data.get("ok"):
                    # Check for captcha or verify error in result
                    if "verify" in str(res_data).lower() or "captcha" in str(res_data).lower():
                        raise CaptchaChallengeException(
                            platform=self.platform_name,
                            url=identity.canonical_url,
                            message="Extension encountered verification wall. Please solve in Chrome.",
                        )

                    # Extract price/stock
                    item_data = res_data.get("data", {})
                    price = float(item_data.get("price", 0.0))
                    title = item_data.get("title") or identity.title or "Product"
                    return ProductSnapshot(
                        product_id=identity.product_id,
                        platform=self.platform_name,
                        title=title,
                        current_price=price,
                        url=identity.canonical_url,
                    )

                if res_data.get("error"):
                    err_msg = str(res_data["error"])
                    if "traffic/error" in err_msg or "verify" in err_msg:
                        raise CaptchaChallengeException(
                            platform=self.platform_name,
                            url=identity.canonical_url,
                            message="Bot challenge triggered in browser. Please solve manually.",
                        )
                    raise RuntimeError(f"Ingest queue execution error: {err_msg}")

            raise TimeoutError(f"Extension queue timed out waiting for {identity.canonical_url}.")

    async def fetch_reviews(self, identity: ProductIdentity, cursor: Optional[str] = None) -> ReviewPage:
        # Reviews can be queried similarly or fetched from CSV output
        return ReviewPage(product_id=identity.product_id, reviews=[], has_next=False)
