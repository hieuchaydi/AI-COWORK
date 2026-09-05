"""Tests for registered commerce-monitor Coworker tools."""

from pathlib import Path
from typing import Optional
from unittest.mock import patch

from coworker.agent import build_engine
from coworker.agents import chat_agent
from coworker.providers import ModelCapabilities
from coworker.tools.commerce_monitor import (
    commerce_monitor_check,
    commerce_monitor_history,
    commerce_monitor_list,
    commerce_monitor_track,
    make_commerce_monitor_tools,
)
from crawler.commerce.interfaces import (
    CommerceConnector,
    ProductIdentity,
    ProductSnapshot,
    ReviewPage,
)
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


class FakeAuthorizedConnector(CommerceConnector):
    platform_name = "authorized_shop"

    async def resolve_product(self, url: str) -> ProductIdentity:
        return ProductIdentity(
            platform=self.platform_name,
            product_id="12345",
            canonical_url="https://api.vendor.example/products/12345",
        )

    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        return ProductSnapshot(
            product_id=identity.product_id,
            platform=identity.platform,
            title="Gaming Keyboard",
            current_price=990_000.0,
            original_price=1_200_000.0,
            url=identity.canonical_url,
        )

    async def fetch_reviews(
        self,
        identity: ProductIdentity,
        cursor: Optional[str] = None,
    ) -> ReviewPage:
        return ReviewPage(product_id=identity.product_id)


class StubProvider:
    def complete(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()


def test_commerce_monitor_tools_are_exposed():
    assert {tool.__name__ for tool in make_commerce_monitor_tools()} == {
        "commerce_monitor_track",
        "commerce_monitor_check",
        "commerce_monitor_list",
        "commerce_monitor_history",
        "commerce_monitor_check_all",
        "configure_profit_guard",
        "get_pricing_recommendation",
        "list_pending_price_approvals",
        "approve_price_change",
        "reject_price_change",
        "get_pricing_audit_history",
    }


def test_commerce_monitor_tools_are_registered_in_engine():
    engine = build_engine(agent=chat_agent(), provider=StubProvider())

    assert "commerce_monitor_track" in engine.registry.names()
    assert "commerce_monitor_check_all" in engine.registry.names()
    assert "configure_profit_guard" in engine.registry.names()
    assert "approve_price_change" in engine.registry.names()


def test_commerce_monitor_tool_integration(tmp_path: Path):
    store = CommerceSqliteStore(str(tmp_path / "tool_test.db"))
    connector = FakeAuthorizedConnector()

    with (
        patch("coworker.tools.commerce_monitor._get_store", return_value=store),
        patch(
            "coworker.tools.commerce_monitor.get_connector_for_url",
            return_value=connector,
        ),
    ):
        url = "https://api.vendor.example/products/12345"

        track_result = commerce_monitor_track(url, target_price=1_000_000.0)
        assert track_result["ok"] is True
        assert track_result["product_id"] == "12345"

        list_result = commerce_monitor_list()
        assert list_result["ok"] is True
        assert list_result["count"] == 1

        check_result = commerce_monitor_check(url)
        assert check_result["ok"] is True
        assert check_result["status"] == "checked"
        assert "target_price_reached" in check_result["alerts_triggered"]

        history_result = commerce_monitor_history(url)
        assert history_result["ok"] is True
        assert history_result["lowest_price"] == 990_000.0
        assert history_result["snapshot_count"] == 1


def test_history_rejects_unbounded_window():
    result = commerce_monitor_history("https://api.vendor.example/products/1", days=0)

    assert result["ok"] is False
    assert "between 1 and 3650" in result["error"]


def test_storefront_url_fails_closed_without_authorized_connector(tmp_path: Path):
    store = CommerceSqliteStore(str(tmp_path / "tool_test.db"))
    with patch("coworker.tools.commerce_monitor._get_store", return_value=store):
        result = commerce_monitor_track("https://shopee.vn/product/1/2")

    assert result["ok"] is False
    assert result["status"] == "unsupported"
    assert "Storefront scraping" in result["error"]
