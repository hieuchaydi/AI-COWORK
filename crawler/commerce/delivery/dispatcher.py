"""
Alert Dispatcher for Commerce Monitoring.
Formats notifications and dispatches to Telegram, Webhooks, and registered listeners.
"""

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..monitoring.alert_rules import AlertTrigger

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Dispatches alerts across multiple channels (Telegram, Webhook, Console, In-Memory)."""

    def __init__(self, telegram_token: Optional[str] = None, default_chat_id: Optional[str] = None):
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.default_chat_id = default_chat_id or os.getenv("TELEGRAM_CHAT_ID")
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

    async def dispatch(self, alert: AlertTrigger, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """Dispatches an alert to all configured channels."""
        msg = self.format_alert_message(alert)
        results: Dict[str, Any] = {"console": True, "telegram": False, "listeners_notified": 0}

        # 1. Console log
        logger.info(f"\n[ALERT DISPATCHED]\n{msg}\n")

        # 2. Trigger registered callbacks
        for listener in self.listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(alert)
                else:
                    listener(alert)
                results["listeners_notified"] += 1
            except Exception as e:
                logger.error(f"Error calling alert listener: {e}")

        # 3. Telegram dispatch if configured
        target_chat_id = chat_id or self.default_chat_id
        if self.telegram_token and target_chat_id:
            tg_ok = await self.send_telegram(msg, chat_id=target_chat_id)
            results["telegram"] = tg_ok

        return results

    async def send_telegram(self, message: str, chat_id: str) -> bool:
        """Sends a message via Telegram Bot API."""
        if not self.telegram_token:
            return False

        endpoint = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code == 200:
                    logger.info(f"Telegram notification sent to chat {chat_id}")
                    return True
                else:
                    logger.warning(f"Telegram API responded with {resp.status_code}: {resp.text}")
                    return False
        except Exception as exc:
            logger.error(f"Failed to send Telegram message: {exc}")
            return False

    async def send_webhook(self, webhook_url: str, alert: AlertTrigger) -> bool:
        """Sends alert as JSON to a custom webhook URL."""
        payload = {
            "event": "commerce_price_alert",
            "rule": alert.rule_name,
            "product_id": alert.product_id,
            "platform": alert.platform,
            "title": alert.title,
            "url": alert.url,
            "previous_value": alert.previous_value,
            "current_value": alert.current_value,
            "severity": alert.severity,
            "message": alert.message,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                return resp.status_code < 400
        except Exception as exc:
            logger.error(f"Webhook dispatch failed to {webhook_url}: {exc}")
            return False
