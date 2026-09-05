"""Unit tests for Profit Guard Coworker Tools."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import pytest

from coworker.tools.commerce_monitor import (
    approve_price_change,
    configure_profit_guard,
    get_pricing_audit_history,
    get_pricing_recommendation,
    list_pending_price_approvals,
    reject_price_change,
)
from crawler.commerce.profit_guard.engine import ProfitGuardEngine
from crawler.commerce.profit_guard.seller_updater import (
    MockOfficialSellerApiProvider,
    SellerUpdateRegistry,
)
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


@pytest.fixture
def mock_engine_fixture(tmp_path: Path):
    db_file = str(tmp_path / "tools_pg.db")
    test_store = CommerceSqliteStore(db_file)
    registry = SellerUpdateRegistry()
    registry.register(MockOfficialSellerApiProvider("mock_seller_api"))
    engine = ProfitGuardEngine(store=test_store, registry=registry)

    with patch("coworker.tools.commerce_monitor._get_profit_guard_engine", return_value=engine), \
         patch("coworker.tools.commerce_monitor._get_store", return_value=test_store):
        yield engine, test_store


def test_profit_guard_coworker_tools_flow(mock_engine_fixture):
    engine, store = mock_engine_fixture

    # 1. configure_profit_guard
    cfg_res = configure_profit_guard(
        sku_id="SKU-TOOL-TEST",
        platform="tiki",
        product_id="prod_tool_1",
        cost_price=200000.0,
        platform_fee_pct=0.08,
        tax_pct=0.015,
        min_margin_pct=0.15,
        max_price_change_pct=0.10,
        authorization_ref="valid_tool_auth",
        seller_provider="mock_seller_api",
    )
    assert cfg_res["ok"] is True
    assert "floor_protection_price" in cfg_res
    assert "breakeven_price" in cfg_res

    # 2. get_pricing_recommendation (Competitor is slightly cheaper, within margin)
    rec_res = get_pricing_recommendation(
        sku_id="SKU-TOOL-TEST",
        current_price=300000.0,
        market_price=285000.0,
    )
    assert rec_res["ok"] is True
    assert rec_res["status"] == "pending_approval"
    rec_id = rec_res["recommendation"]["id"]

    # 3. list_pending_price_approvals
    pending_res = list_pending_price_approvals()
    assert pending_res["ok"] is True
    assert pending_res["count"] == 1
    assert pending_res["pending_approvals"][0]["id"] == rec_id

    # 4. approve_price_change with apply_immediately=True
    app_res = approve_price_change(recommendation_id=rec_id, approver="test_user", apply_immediately=True)
    assert app_res["ok"] is True
    assert app_res["status"] == "applied"
    assert app_res["applied_price"] == "285000"

    # 5. verify list_pending is now 0
    pending_after = list_pending_price_approvals()
    assert pending_after["count"] == 0

    # 6. get_pricing_audit_history
    audit_res = get_pricing_audit_history(sku_id="SKU-TOOL-TEST")
    assert audit_res["ok"] is True
    assert audit_res["count"] >= 2  # config + rec + apply


def test_reject_price_change_tool(mock_engine_fixture):
    engine, store = mock_engine_fixture

    configure_profit_guard(
        sku_id="SKU-REJECT-TEST",
        platform="tiki",
        product_id="prod_tool_2",
        cost_price=100000.0,
        min_margin_pct=0.15,
    )
    rec_res = get_pricing_recommendation(
        sku_id="SKU-REJECT-TEST",
        current_price=150000.0,
        market_price=140000.0,
    )
    rec_id = rec_res["recommendation"]["id"]

    rej_res = reject_price_change(recommendation_id=rec_id, approver="test_user", reason="Not matching today")
    assert rej_res["ok"] is True
    assert rej_res["status"] == "rejected"
