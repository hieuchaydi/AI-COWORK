"""
Commerce Monitoring Tools for Coworker Agent.
Allows agents to track product prices, check stock/rating changes, and query price histories
using compliant, sustainable methods.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .base import coworker_tool

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parents[3]
root_dir = str(project_root)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from crawler.commerce.connectors.registry import get_connector_for_url
from crawler.commerce.interfaces import AccessDeniedException
from crawler.commerce.scheduler.runner import CommerceMonitorScheduler
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore

_DB_PATH = project_root / "outputs" / "commerce" / "commerce_monitor.db"


def _get_store() -> CommerceSqliteStore:
    return CommerceSqliteStore(str(_DB_PATH))


def _get_scheduler() -> CommerceMonitorScheduler:
    return CommerceMonitorScheduler(
        store=_get_store(),
        connector_resolver=get_connector_for_url,
    )


@coworker_tool(category="crawl")
def commerce_monitor_track(url: str, target_price: Optional[float] = None) -> Dict[str, Any]:
    """Register a product from a configured official API/feed for monitoring.

    Args:
        url: Direct URL to the product.
        target_price: Optional target price to trigger notification when reached.

    Returns:
        Dictionary with status, platform, product_id, and canonical URL.
    """
    scheduler = _get_scheduler()
    try:
        identity = asyncio.run(scheduler.add_product(url, target_price=target_price))
        return {
            "ok": True,
            "platform": identity.platform,
            "product_id": identity.product_id,
            "canonical_url": identity.canonical_url,
            "target_price": target_price,
            "message": f"Successfully registered {identity.canonical_url} for monitoring.",
        }
    except AccessDeniedException as exc:
        return {"ok": False, "status": "unsupported", "error": str(exc), "url": url}
    except ValueError as exc:
        return {"ok": False, "status": "invalid_url", "error": str(exc), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


@coworker_tool(category="crawl")
def commerce_monitor_check(url: str) -> Dict[str, Any]:
    """Fetch current price, stock, and rating for a product URL and compute diffs/alerts.

    Args:
        url: Direct URL to the product.

    Returns:
        Snapshot details, price deltas, and any triggered alert rules.
    """
    scheduler = _get_scheduler()
    try:
        return asyncio.run(scheduler.check_product(url))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


@coworker_tool(category="crawl")
def commerce_monitor_list() -> Dict[str, Any]:
    """List all currently active monitored products and their last check timestamps."""
    try:
        products = _get_store().list_monitored_products(active_only=True)
        return {
            "ok": True,
            "count": len(products),
            "products": products,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@coworker_tool(category="crawl")
def commerce_monitor_history(url: str, days: int = 30) -> Dict[str, Any]:
    """Retrieve historical price snapshots and 30-day minimum price for a product.

    Args:
        url: Direct URL to the product.
        days: Historical lookback window in days (default: 30).
    """
    if not 1 <= days <= 3650:
        return {"ok": False, "error": "days must be between 1 and 3650", "url": url}
    try:
        connector = get_connector_for_url(url)
        identity = asyncio.run(connector.resolve_product(url))
        store = _get_store()
        snapshots = store.get_snapshots_history(
            identity.product_id,
            identity.platform,
            limit=50,
            days=days,
        )
        lowest_price = store.get_lowest_price(
            identity.product_id,
            identity.platform,
            days=days,
        )
        return {
            "ok": True,
            "product_id": identity.product_id,
            "platform": identity.platform,
            "canonical_url": identity.canonical_url,
            "lowest_price": lowest_price,
            "snapshot_count": len(snapshots),
            "history": [snapshot.model_dump(mode="json") for snapshot in snapshots],
        }
    except AccessDeniedException as exc:
        return {"ok": False, "status": "unsupported", "error": str(exc), "url": url}
    except ValueError as exc:
        return {"ok": False, "status": "invalid_url", "error": str(exc), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


@coworker_tool(category="crawl")
def commerce_monitor_check_all() -> Dict[str, Any]:
    """Execute scheduled check across all active registered products."""
    scheduler = _get_scheduler()
    try:
        results = asyncio.run(scheduler.check_all())
        return {
            "ok": True,
            "total_checked": len(results),
            "results": results,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def make_commerce_monitor_tools() -> list[Any]:
    """Return the commerce tools registered by the Coworker runtime."""

    return [
        commerce_monitor_track,
        commerce_monitor_check,
        commerce_monitor_list,
        commerce_monitor_history,
        commerce_monitor_check_all,
    ]
