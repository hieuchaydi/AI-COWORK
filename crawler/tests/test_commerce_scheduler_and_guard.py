"""Tests for commerce scheduling, durable backoff, and delivery state."""

from pathlib import Path
from typing import Optional

import pytest

from crawler.commerce.delivery.dispatcher import AlertDispatcher
from crawler.commerce.interfaces import (
    CaptchaChallengeException,
    CommerceConnector,
    ProductIdentity,
    ProductSnapshot,
    RateLimitException,
    ReviewPage,
)
from crawler.commerce.monitoring.alert_rules import AlertTrigger
from crawler.commerce.scheduler.runner import CommerceMonitorScheduler
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


class FakeAuthorizedConnector(CommerceConnector):
    platform_name = "authorized_shop"

    def __init__(self) -> None:
        self.snapshot = ProductSnapshot(
            product_id="123",
            platform=self.platform_name,
            title="Authorized product",
            current_price=250_000.0,
            original_price=300_000.0,
            rating_score=4.9,
            url="https://api.vendor.example/products/123",
        )
        self.error: Exception | None = None

    async def resolve_product(self, url: str) -> ProductIdentity:
        return ProductIdentity(
            platform=self.platform_name,
            product_id="123",
            canonical_url="https://api.vendor.example/products/123",
        )

    async def fetch_product(self, identity: ProductIdentity) -> ProductSnapshot:
        if self.error:
            raise self.error
        return self.snapshot

    async def fetch_reviews(
        self,
        identity: ProductIdentity,
        cursor: Optional[str] = None,
    ) -> ReviewPage:
        return ReviewPage(product_id=identity.product_id)


@pytest.fixture
def scheduler_fixture(tmp_path: Path):
    store = CommerceSqliteStore(str(tmp_path / "test_sched.db"))
    dispatcher = AlertDispatcher()
    connector = FakeAuthorizedConnector()
    scheduler = CommerceMonitorScheduler(
        store=store,
        dispatcher=dispatcher,
        min_interval_seconds=0.01,
        connector_resolver=lambda _url: connector,
    )
    return scheduler, store, dispatcher, connector


@pytest.mark.asyncio
async def test_scheduler_tracks_checks_and_keeps_undelivered_alert(scheduler_fixture):
    scheduler, store, _dispatcher, _connector = scheduler_fixture
    url = "https://api.vendor.example/products/123"

    await scheduler.add_product(url, target_price=260_000.0)
    result = await scheduler.check_product(url)

    assert result["ok"] is True
    assert result["status"] == "checked"
    assert result["snapshot"]["current_price"] == 250_000.0
    assert result["alerts_triggered"] == ["target_price_reached"]
    assert result["alerts_pending_delivery"] == 1
    assert len(store.get_undelivered_alerts()) == 1


@pytest.mark.asyncio
async def test_successful_listener_marks_alert_delivered(scheduler_fixture):
    scheduler, store, dispatcher, _connector = scheduler_fixture
    dispatcher.register_listener(lambda _alert: True)
    url = "https://api.vendor.example/products/123"

    await scheduler.add_product(url, target_price=260_000.0)
    result = await scheduler.check_product(url)

    assert result["alerts_dispatched"] == 1
    assert result["alerts_pending_delivery"] == 0
    assert store.get_undelivered_alerts() == []


@pytest.mark.asyncio
async def test_challenge_stops_and_persists_backoff(scheduler_fixture):
    scheduler, store, dispatcher, connector = scheduler_fixture
    captured_alerts: list[AlertTrigger] = []
    dispatcher.register_listener(lambda alert: captured_alerts.append(alert))
    connector.error = CaptchaChallengeException(
        platform=connector.platform_name,
        url=connector.snapshot.url,
        message="Provider verification required",
    )

    result = await scheduler.check_product(connector.snapshot.url)

    assert result["status"] == "challenge_detected"
    assert captured_alerts[0].rule_name == "human_verification_required"
    assert "không tự động vượt CAPTCHA" in captured_alerts[0].message
    assert scheduler.is_platform_backed_off(connector.platform_name)

    restarted_scheduler = CommerceMonitorScheduler(
        store=store,
        connector_resolver=lambda _url: connector,
    )
    restarted_result = await restarted_scheduler.check_product(connector.snapshot.url)
    assert restarted_result["status"] == "backed_off"


@pytest.mark.asyncio
async def test_rate_limit_persists_provider_backoff(scheduler_fixture):
    scheduler, store, _dispatcher, connector = scheduler_fixture
    connector.error = RateLimitException(platform=connector.platform_name, retry_after=120.0)

    result = await scheduler.check_product(connector.snapshot.url)

    assert result["status"] == "rate_limited"
    assert result["retry_after"] == 120.0
    assert store.get_platform_backoff_until(connector.platform_name) is not None
