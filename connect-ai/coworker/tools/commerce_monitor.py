"""
Commerce Monitoring & Profit Guard Tools for Coworker Agent.
Allows agents to:
1. Track product prices, stock/rating changes, and query histories.
2. Configure Profit Guard cost profiles, generate competitive margin-protected price recommendations,
   manage human-in-the-loop approvals, and update prices via official Seller APIs.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Ensure project root is in sys.path
root_dir = str(Path(__file__).resolve().parents[3])
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from crawler.commerce.connectors.registry import get_connector_for_url
from crawler.commerce.profit_guard.calculator import ProfitGuardCalculator
from crawler.commerce.profit_guard.engine import ProfitGuardEngine
from crawler.commerce.profit_guard.models import CostProfile
from crawler.commerce.profit_guard.seller_updater import (
    MockOfficialSellerApiProvider,
    SellerUpdateRegistry,
)
from crawler.commerce.scheduler.runner import CommerceMonitorScheduler
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


def _get_store() -> CommerceSqliteStore:
    return CommerceSqliteStore()


def _get_scheduler() -> CommerceMonitorScheduler:
    store = _get_store()
    return CommerceMonitorScheduler(store=store)


def _get_profit_guard_engine() -> ProfitGuardEngine:
    store = _get_store()
    registry = SellerUpdateRegistry()
    # Register mock official seller API provider for hermetic execution / testing
    registry.register(MockOfficialSellerApiProvider("mock_seller_api"))
    return ProfitGuardEngine(store=store, registry=registry)


# ── Commerce Monitoring Tools ───────────────────────────────────────────


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
    store = _get_store()
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
    store = _get_store()

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


# ── Profit Guard Pricing & Approval Tools ───────────────────────────────


def configure_profit_guard(
    sku_id: str,
    platform: str,
    product_id: str,
    cost_price: Union[float, int, str],
    platform_fee_pct: Union[float, int, str] = 0.08,
    platform_fee_fixed: Union[float, int, str] = 0.0,
    tax_pct: Union[float, int, str] = 0.015,
    additional_cost: Union[float, int, str] = 0.0,
    min_margin_pct: Union[float, int, str] = 0.15,
    max_price_change_pct: Union[float, int, str] = 0.10,
    cooldown_seconds: int = 3600,
    authorization_ref: Optional[str] = None,
    seller_provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Configures Profit Guard cost profile and pricing boundaries for a SKU.

    Args:
        sku_id: Unique identifier for the SKU.
        platform: E-commerce platform (e.g. 'tiki', 'shopee').
        product_id: Platform item/product ID.
        cost_price: Base Cost of Goods Sold (COGS).
        platform_fee_pct: Platform commission/take-rate (default: 0.08 = 8%).
        platform_fee_fixed: Fixed fee per transaction in VND (default: 0).
        tax_pct: Taxes on selling price (default: 0.015 = 1.5%).
        additional_cost: Additional packaging, handling or shipping cost (default: 0).
        min_margin_pct: Minimum net profit margin on selling price (default: 0.15 = 15%).
        max_price_change_pct: Maximum allowed single adjustment percentage (default: 0.10 = 10%).
        cooldown_seconds: Minimum seconds between recommendations (default: 3600).
        authorization_ref: Official Seller API token or credential reference.
        seller_provider: Registered official Seller API provider name.

    Returns:
        Confirmation dictionary with calculated breakeven and floor protection prices.
    """
    try:
        profile = CostProfile(
            sku_id=sku_id,
            platform=platform,
            product_id=product_id,
            cost_price=cost_price,
            platform_fee_pct=platform_fee_pct,
            platform_fee_fixed=platform_fee_fixed,
            tax_pct=tax_pct,
            additional_cost=additional_cost,
            min_margin_pct=min_margin_pct,
            max_price_change_pct=max_price_change_pct,
            cooldown_seconds=cooldown_seconds,
            authorization_ref=authorization_ref,
            seller_provider=seller_provider,
        )
        engine = _get_profit_guard_engine()
        res = engine.configure_sku(profile)
        if res.get("ok"):
            breakeven = ProfitGuardCalculator.calculate_breakeven_price(profile)
            floor = ProfitGuardCalculator.calculate_floor_price(profile)
            res["breakeven_price"] = str(breakeven)
            res["floor_protection_price"] = str(floor)
            res["min_margin_pct"] = f"{float(profile.min_margin_pct) * 100:.1f}%"
        return res
    except Exception as exc:
        return {"ok": False, "error": "VALIDATION_ERROR", "message": str(exc)}


def get_pricing_recommendation(
    sku_id: str,
    current_price: Optional[Union[float, int, str]] = None,
    market_price: Optional[Union[float, int, str]] = None,
) -> Dict[str, Any]:
    """Evaluates competitive market price and generates a margin-protected price recommendation.

    Args:
        sku_id: Unique SKU identifier.
        current_price: Current selling price. If omitted, attempts lookup from latest snapshot.
        market_price: Competitor or target market price.

    Returns:
        Recommended price, cash profit, profit margin %, and reason.
    """
    engine = _get_profit_guard_engine()
    profile = engine.store.get_cost_profile(sku_id)
    if not profile:
        return {"ok": False, "error": "PROFILE_NOT_FOUND", "message": f"SKU '{sku_id}' is not configured."}

    # If current_price not passed, resolve from latest product snapshot
    if current_price is None:
        snap = engine.store.get_latest_snapshot(profile.product_id, profile.platform)
        if snap:
            current_price = snap.current_price
        else:
            return {
                "ok": False,
                "error": "PRICE_MISSING",
                "message": f"No current price provided and no snapshot found for SKU '{sku_id}'.",
            }

    curr_dec = Decimal(str(current_price))
    mkt_dec = Decimal(str(market_price)) if market_price is not None else None

    return engine.generate_recommendation(
        sku_id=sku_id,
        current_price=curr_dec,
        market_price=mkt_dec,
        actor="coworker_tool",
    )


def list_pending_price_approvals(platform: Optional[str] = None) -> Dict[str, Any]:
    """Lists all pending pricing recommendations waiting for human-in-the-loop approval.

    Args:
        platform: Optional platform filter (e.g. 'tiki', 'shopee').

    Returns:
        List of pending approval records with expiration status and margin details.
    """
    engine = _get_profit_guard_engine()
    pending = engine.list_pending_approvals(platform=platform)
    return {
        "ok": True,
        "count": len(pending),
        "pending_approvals": pending,
    }


def approve_price_change(
    recommendation_id: int,
    approver: str = "seller_admin",
    apply_immediately: bool = True,
) -> Dict[str, Any]:
    """Approves a price change recommendation and optionally applies it via official Seller API.

    Args:
        recommendation_id: The ID of the pending recommendation.
        approver: Name or identifier of the approving user/admin.
        apply_immediately: If True, calls official Seller API immediately upon approval.

    Returns:
        Approval status, transaction details, and execution result.
    """
    engine = _get_profit_guard_engine()
    return asyncio.run(
        engine.approve_price_change(
            recommendation_id=recommendation_id,
            approver=approver,
            apply_immediately=apply_immediately,
        )
    )


def reject_price_change(
    recommendation_id: int,
    approver: str = "seller_admin",
    reason: str = "",
) -> Dict[str, Any]:
    """Rejects a pending price change recommendation with an optional reason.

    Args:
        recommendation_id: The ID of the pending recommendation.
        approver: Name or identifier of the rejecting user/admin.
        reason: Optional justification for rejection.

    Returns:
        Rejection status confirmation.
    """
    engine = _get_profit_guard_engine()
    return engine.reject_price_change(
        recommendation_id=recommendation_id,
        approver=approver,
        reason=reason,
    )


def get_pricing_audit_history(
    sku_id: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Retrieves immutable audit log history for pricing recommendations and approvals.

    Args:
        sku_id: Optional SKU filter.
        limit: Maximum number of audit records to return (default: 50).

    Returns:
        Chronological list of audit trail entries.
    """
    engine = _get_profit_guard_engine()
    logs = engine.get_audit_history(sku_id=sku_id, limit=limit)
    return {
        "ok": True,
        "count": len(logs),
        "audit_logs": logs,
    }
