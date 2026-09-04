"""
Commerce Monitor Scheduler & Human-in-the-Loop Guard.
Coordinates periodic checks, enforces non-bypass compliance, records snapshots,
runs diff analysis, and dispatches verified alerts.
"""

import asyncio
import logging
import random
import time
from typing import Any, Dict, List, Optional

from ..connectors.registry import get_connector_for_url
from ..delivery.dispatcher import AlertDispatcher
from ..interfaces import (
    AccessDeniedException,
    CaptchaChallengeException,
    CommerceException,
    ProductIdentity,
    ProductNotFoundException,
    ProductSnapshot,
    RateLimitException,
    SessionExpiredException,
)
from ..monitoring.alert_rules import AlertTrigger
from ..monitoring.diff_engine import DiffEngine
from ..storage.sqlite_store import CommerceSqliteStore

logger = logging.getLogger(__name__)


class CommerceMonitorScheduler:
    """
    Coordinates commerce monitoring runs with sustainable compliance,
    rate-limit backoff, and human-in-the-loop challenge handling.
    """

    def __init__(
        self,
        store: Optional[CommerceSqliteStore] = None,
        diff_engine: Optional[DiffEngine] = None,
        dispatcher: Optional[AlertDispatcher] = None,
        min_interval_seconds: float = 2.0,
    ):
        self.store = store or CommerceSqliteStore()
        self.diff_engine = diff_engine or DiffEngine()
        self.dispatcher = dispatcher or AlertDispatcher()
        self.min_interval_seconds = min_interval_seconds
        self._platform_backoff_until: Dict[str, float] = {}

    def is_platform_backed_off(self, platform: str) -> bool:
        """Checks if a platform is currently in a polite backoff state."""
        until = self._platform_backoff_until.get(platform, 0.0)
        return time.time() < until

    def set_platform_backoff(self, platform: str, duration_seconds: float):
        """Sets a mandatory backoff duration for a target platform."""
        self._platform_backoff_until[platform] = time.time() + duration_seconds
        logger.warning(f"[{platform}] Backing off requests for {duration_seconds:.1f}s.")

    async def add_product(self, url: str, target_price: Optional[float] = None) -> ProductIdentity:
        """Resolves product and registers it for monitoring."""
        connector = get_connector_for_url(url)
        identity = await connector.resolve_product(url)
        self.store.upsert_monitored_product(identity, target_price=target_price)
        logger.info(f"Registered monitored product: {identity.canonical_url} (Platform: {identity.platform})")
        return identity

    async def check_product(self, url: str) -> Dict[str, Any]:
        """
        Executes a single check on a product:
        - Resolves identity and connector
        - Checks platform backoff state
        - Fetches fresh snapshot safely
        - Persists snapshot to SQLite
        - Evaluates diff engine rules
        - Dispatches alerts if triggered
        """
        connector = get_connector_for_url(url)
        identity = await connector.resolve_product(url)
        platform = identity.platform

        if self.is_platform_backed_off(platform):
            remaining = self._platform_backoff_until[platform] - time.time()
            return {
                "ok": False,
                "status": "backed_off",
                "message": f"Platform '{platform}' is backed off ({remaining:.0f}s remaining). Skipping.",
                "url": url,
            }

        # Retrieve previous snapshot and history
        prev_snapshot = self.store.get_latest_snapshot(identity.product_id, platform)
        history = self.store.get_snapshots_history(identity.product_id, platform, limit=50, days=30)

        # Retrieve target price if configured
        products = self.store.list_monitored_products(active_only=False)
        target_price = None
        for p in products:
            if p["canonical_url"] == identity.canonical_url:
                target_price = p["target_price"]
                break

        # Fetch fresh snapshot with safety handling
        try:
            snapshot = await connector.fetch_product(identity)
        except CaptchaChallengeException as exc:
            # Human-in-the-loop: Back off and request human assistance
            self.set_platform_backoff(platform, duration_seconds=600.0)  # 10 min pause
            human_alert = AlertTrigger(
                rule_name="human_verification_required",
                product_id=identity.product_id,
                platform=platform,
                title=f"Xác thực người dùng cần thiết: {identity.canonical_url}",
                url=url,
                previous_value=None,
                current_value=0.0,
                message=(
                    f"⚠️ Sàn {platform.upper()} yêu cầu giải Captcha / Cloudflare Challenge.\n"
                    f"Vui lòng mở link {url} trong trình duyệt để hoàn thành xác thực. Không dùng bot bypass."
                ),
                severity="urgent",
            )
            await self.dispatcher.dispatch(human_alert)
            return {
                "ok": False,
                "status": "challenge_detected",
                "error": str(exc),
                "url": url,
            }
        except RateLimitException as exc:
            self.set_platform_backoff(platform, duration_seconds=exc.retry_after)
            return {
                "ok": False,
                "status": "rate_limited",
                "error": str(exc),
                "retry_after": exc.retry_after,
                "url": url,
            }
        except SessionExpiredException as exc:
            human_alert = AlertTrigger(
                rule_name="session_login_required",
                product_id=identity.product_id,
                platform=platform,
                title=f"Cần đăng nhập: {platform}",
                url=url,
                previous_value=None,
                current_value=0.0,
                message=f"🔒 Phiên đăng nhập {platform} đã hết hạn. Vui lòng đăng nhập lại.",
                severity="warning",
            )
            await self.dispatcher.dispatch(human_alert)
            return {
                "ok": False,
                "status": "login_required",
                "error": str(exc),
                "url": url,
            }
        except ProductNotFoundException as exc:
            return {"ok": False, "status": "not_found", "error": str(exc), "url": url}
        except Exception as exc:
            logger.error(f"Failed to check product {url}: {exc}")
            return {"ok": False, "status": "error", "error": str(exc), "url": url}

        # Record snapshot in SQLite store
        self.store.record_snapshot(snapshot)
        self.store.update_last_checked(identity.canonical_url)

        # Run DiffEngine to detect alerts
        triggers = self.diff_engine.evaluate(snapshot, prev_snapshot, history, target_price=target_price)

        # Dispatch and record any triggered alerts
        dispatched_count = 0
        for trigger in triggers:
            self.store.record_alert(
                product_id=trigger.product_id,
                platform=trigger.platform,
                rule=trigger.rule_name,
                previous_value=trigger.previous_value,
                current_value=trigger.current_value,
                message=trigger.message,
            )
            await self.dispatcher.dispatch(trigger)
            dispatched_count += 1

        summary = self.diff_engine.calculate_summary(snapshot, prev_snapshot, history)
        return {
            "ok": True,
            "status": "checked",
            "snapshot": snapshot.model_dump(mode="json"),
            "summary": summary,
            "alerts_triggered": [t.rule_name for t in triggers],
            "alerts_dispatched": dispatched_count,
        }

    async def check_all(self) -> List[Dict[str, Any]]:
        """
        Monitors all active registered products in sequence with randomized delays
        to prevent request bursts or bot-like behavior.
        """
        products = self.store.list_monitored_products(active_only=True)
        results = []

        for prod in products:
            url = prod["canonical_url"]
            res = await self.check_product(url)
            results.append(res)

            # Introduce polite randomized delay between checks
            jitter = random.uniform(self.min_interval_seconds, self.min_interval_seconds + 1.5)
            await asyncio.sleep(jitter)

        return results
