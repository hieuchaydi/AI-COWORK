"""
Commerce Monitoring Tools for Coworker Agent.
Allows agents to track product prices, check stock/rating changes, and query price histories
using compliant, sustainable methods.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
root_dir = str(Path(__file__).resolve().parents[3])
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from crawler.commerce.connectors.registry import get_connector_for_url
from crawler.commerce.scheduler.runner import CommerceMonitorScheduler
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


def _get_scheduler() -> CommerceMonitorScheduler:
    store = CommerceSqliteStore("outputs/commerce/commerce_monitor.db")
    return CommerceMonitorScheduler(store=store)


def commerce_monitor_track(url: str, target_price: Optional[float] = None) -> Dict[str, Any]:
    """Register a product URL (e.g. Tiki, Shopee) for price and stock monitoring.

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
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


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


def commerce_monitor_list() -> Dict[str, Any]:
    """List all currently active monitored products and their last check timestamps."""
    store = CommerceSqliteStore("outputs/commerce/commerce_monitor.db")
    products = store.list_monitored_products(active_only=True)
    return {
        "ok": True,
        "count": len(products),
        "products": products,
    }


def commerce_monitor_history(url: str, days: int = 30) -> Dict[str, Any]:
    """Retrieve historical price snapshots and 30-day minimum price for a product.

    Args:
        url: Direct URL to the product.
        days: Historical lookback window in days (default: 30).
    """
    connector = get_connector_for_url(url)
    identity = asyncio.run(connector.resolve_product(url))
    store = CommerceSqliteStore("outputs/commerce/commerce_monitor.db")

    snapshots = store.get_snapshots_history(identity.product_id, identity.platform, limit=50, days=days)
    lowest_price = store.get_lowest_price(identity.product_id, identity.platform, days=days)

    return {
        "ok": True,
        "product_id": identity.product_id,
        "platform": identity.platform,
        "canonical_url": identity.canonical_url,
        "lowest_price": lowest_price,
        "snapshot_count": len(snapshots),
        "history": [s.model_dump(mode="json") for s in snapshots],
    }


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
