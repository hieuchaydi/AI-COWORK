"""Unit tests for Commerce Scheduler and Human-in-the-loop Safety Guard."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from crawler.commerce.connectors.tiki import TikiCommerceConnector
from crawler.commerce.delivery.dispatcher import AlertDispatcher
from crawler.commerce.interfaces import (
    CaptchaChallengeException,
    ProductIdentity,
    ProductSnapshot,
    RateLimitException,
)
from crawler.commerce.monitoring.alert_rules import AlertTrigger
from crawler.commerce.scheduler.runner import CommerceMonitorScheduler
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


@pytest.fixture
def scheduler_fixture(tmp_path: Path):
    db_file = tmp_path / "test_sched.db"
    store = CommerceSqliteStore(str(db_file))
    dispatcher = AlertDispatcher()
    sched = CommerceMonitorScheduler(store=store, dispatcher=dispatcher, min_interval_seconds=0.01)
    return sched, store, dispatcher


@pytest.mark.asyncio
async def test_scheduler_add_and_check_product_success(scheduler_fixture):
    sched, store, dispatcher = scheduler_fixture

    # Mock Tiki connector fetch_product
    mock_snapshot = ProductSnapshot(
        product_id="274819231",
        platform="tiki",
        title="Samsung Galaxy S24 Ultra",
        current_price=25000000.0,
        original_price=30000000.0,
        in_stock=True,
        rating_score=4.9,
        url="https://tiki.vn/product-p274819231.html",
    )

    with patch.object(TikiCommerceConnector, "fetch_product", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_snapshot

        url = "https://tiki.vn/product-p274819231.html"
        await sched.add_product(url, target_price=26000000.0)

        # Check product
        res = await sched.check_product(url)
        assert res["ok"] is True
        assert res["status"] == "checked"
        assert res["snapshot"]["current_price"] == 25000000.0
        # Target price 26m was reached (25m <= 26m)
        assert "target_price_reached" in res["alerts_triggered"]


@pytest.mark.asyncio
async def test_human_in_the_loop_guard_on_captcha(scheduler_fixture):
    """
    CRITICAL TEST: Verifies that when a bot challenge / captcha is encountered,
    the system DOES NOT attempt bypass; instead it activates human-in-the-loop:
    1. Dispatches an urgent user verification alert
    2. Puts the platform into temporary backoff
    """
    sched, store, dispatcher = scheduler_fixture

    captured_alerts: list[AlertTrigger] = []
    dispatcher.register_listener(lambda a: captured_alerts.append(a))

    with patch.object(TikiCommerceConnector, "fetch_product", new_callable=AsyncMock) as mock_fetch:
        # Simulate bot wall / Turnstile challenge
        mock_fetch.side_effect = CaptchaChallengeException(
            platform="tiki",
            url="https://tiki.vn/product-p111.html",
            message="Cloudflare Turnstile challenge active",
        )

        url = "https://tiki.vn/product-p111.html"
        res = await sched.check_product(url)

        # 1. Returned challenge_detected status
        assert res["ok"] is False
        assert res["status"] == "challenge_detected"

        # 2. Urgent Human-in-the-loop notification was dispatched
        assert len(captured_alerts) == 1
        alert = captured_alerts[0]
        assert alert.rule_name == "human_verification_required"
        assert alert.severity == "urgent"
        assert "mở link" in alert.message
        assert "Không dùng bot bypass" in alert.message

        # 3. Platform was put into backoff
        assert sched.is_platform_backed_off("tiki") is True

        # 4. Immediate second check should be politely skipped
        res2 = await sched.check_product(url)
        assert res2["ok"] is False
        assert res2["status"] == "backed_off"


@pytest.mark.asyncio
async def test_scheduler_rate_limit_backoff(scheduler_fixture):
    sched, store, dispatcher = scheduler_fixture

    with patch.object(TikiCommerceConnector, "fetch_product", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = RateLimitException(platform="tiki", retry_after=120.0)

        url = "https://tiki.vn/product-p222.html"
        res = await sched.check_product(url)

        assert res["ok"] is False
        assert res["status"] == "rate_limited"
        assert sched.is_platform_backed_off("tiki") is True
