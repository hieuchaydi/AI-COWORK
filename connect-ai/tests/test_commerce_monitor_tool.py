"""Tests for commerce_monitor Coworker tool."""

from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from coworker.tools.commerce_monitor import (
    commerce_monitor_check,
    commerce_monitor_history,
    commerce_monitor_list,
    commerce_monitor_track,
)
from crawler.commerce.connectors.tiki import TikiCommerceConnector
from crawler.commerce.interfaces import ProductIdentity, ProductSnapshot
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


def test_commerce_monitor_tool_integration(tmp_path: Path):
    db_file = str(tmp_path / "tool_test.db")

    with patch("coworker.tools.commerce_monitor.CommerceSqliteStore") as mock_store_cls:
        test_store = CommerceSqliteStore(db_file)
        mock_store_cls.return_value = test_store

        mock_snap = ProductSnapshot(
            product_id="12345",
            platform="tiki",
            title="Gaming Keyboard",
            current_price=990000.0,
            original_price=1200000.0,
            url="https://tiki.vn/p12345.html",
        )

        with patch.object(TikiCommerceConnector, "fetch_product", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_snap

            url = "https://tiki.vn/p12345.html"

            # 1. Track
            track_res = commerce_monitor_track(url, target_price=1000000.0)
            assert track_res["ok"] is True
            assert track_res["product_id"] == "12345"

            # 2. List
            list_res = commerce_monitor_list()
            assert list_res["ok"] is True
            assert list_res["count"] == 1

            # 3. Check
            check_res = commerce_monitor_check(url)
            assert check_res["ok"] is True
            assert check_res["status"] == "checked"
            assert "target_price_reached" in check_res["alerts_triggered"]

            # 4. History
            hist_res = commerce_monitor_history(url)
            assert hist_res["ok"] is True
            assert hist_res["lowest_price"] == 990000.0
            assert hist_res["snapshot_count"] >= 1
