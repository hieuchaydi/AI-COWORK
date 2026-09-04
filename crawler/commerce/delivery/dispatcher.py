"""Alert delivery boundary for commerce monitoring."""

import asyncio
import logging
from typing import Any, Callable, Dict, List

from ..monitoring.alert_rules import AlertTrigger

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Log alerts and hand them to application-owned delivery listeners.

    The commerce layer intentionally does not call Telegram or arbitrary webhooks
    itself. The application already owns authenticated connector tools, audit, and
    delivery policy; listeners are the narrow integration point into that layer.
    """

    def __init__(self) -> None:
        self.listeners: List[Callable[[AlertTrigger], Any]] = []

    def register_listener(self, listener: Callable[[AlertTrigger], Any]):
        """Adds a callback for real-time alert events."""
        self.listeners.append(listener)

    def format_alert_message(self, alert: AlertTrigger) -> str:
        """Formats alert into a human-friendly notification text."""
        platform_tag = alert.platform.upper()
        header = f"🔔 [AI-COWORK • THEO DÕI GIÁ - {platform_tag}]"

        lines = [
            header,
            "────────────────────────",
            alert.message,
            "────────────────────────",
            f"🔗 Link: {alert.url}",
        ]
        return "\n".join(lines)

    async def dispatch(self, alert: AlertTrigger) -> Dict[str, Any]:
        """Dispatch an alert to registered listeners and report durable outcome."""
        msg = self.format_alert_message(alert)
        results: Dict[str, Any] = {
            "logged": True,
            "delivered": False,
            "listeners_notified": 0,
            "listener_errors": [],
        }

        logger.info("[COMMERCE ALERT]\n%s", msg)

        for listener in self.listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    outcome = await listener(alert)
                else:
                    outcome = listener(alert)
                    if asyncio.iscoroutine(outcome):
                        outcome = await outcome
                if outcome is not False:
                    results["listeners_notified"] += 1
            except Exception as exc:
                logger.exception("Commerce alert listener failed")
                results["listener_errors"].append(str(exc))

        results["delivered"] = results["listeners_notified"] > 0
        return results
