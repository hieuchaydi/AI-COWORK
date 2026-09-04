"""Unit tests for SQLite Commerce Storage Engine."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import pytest

from crawler.commerce.interfaces import ProductIdentity, ProductSnapshot
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


@pytest.fixture
def temp_store(tmp_path: Path) -> CommerceSqliteStore:
    db_file = tmp_path / "test_commerce.db"
    return CommerceSqliteStore(str(db_file))


def test_upsert_and_list_monitored_products(temp_store: CommerceSqliteStore):
    identity = ProductIdentity(
        platform="tiki",
        product_id="123456",
        canonical_url="https://tiki.vn/p123456.html",
        title="Gaming Mouse",
    )
    temp_store.upsert_monitored_product(identity, target_price=500000.0)

    products = temp_store.list_monitored_products(active_only=True)
    assert len(products) == 1
    assert products[0]["product_id"] == "123456"
    assert products[0]["platform"] == "tiki"
    assert products[0]["target_price"] == 500000.0

    # Deactivate product
    temp_store.deactivate_product(identity.canonical_url)
    assert len(temp_store.list_monitored_products(active_only=True)) == 0
    assert len(temp_store.list_monitored_products(active_only=False)) == 1


def test_record_snapshots_and_history(temp_store: CommerceSqliteStore):
    snap1 = ProductSnapshot(
        product_id="999",
        platform="tiki",
        title="Mechanical Keyboard",
        current_price=1500000.0,
        url="https://tiki.vn/p999.html",
        timestamp=datetime.now(timezone.utc) - timedelta(days=2),
    )
    snap2 = ProductSnapshot(
        product_id="999",
        platform="tiki",
        title="Mechanical Keyboard",
        current_price=1200000.0,
        url="https://tiki.vn/p999.html",
        timestamp=datetime.now(timezone.utc),
    )

    temp_store.record_snapshot(snap1)
    temp_store.record_snapshot(snap2)

    latest = temp_store.get_latest_snapshot("999", "tiki")
    assert latest is not None
    assert latest.current_price == 1200000.0

    history = temp_store.get_snapshots_history("999", "tiki", limit=10)
    assert len(history) == 2

    min_30d = temp_store.get_lowest_price("999", "tiki", days=30)
    assert min_30d == 1200000.0


def test_record_and_deliver_alerts(temp_store: CommerceSqliteStore):
    alert_id = temp_store.record_alert(
        product_id="999",
        platform="tiki",
        rule="price_drop_pct",
        previous_value=1500000.0,
        current_value=1200000.0,
        message="Price dropped by 20%",
    )
    assert alert_id > 0

    undelivered = temp_store.get_undelivered_alerts()
    assert len(undelivered) == 1
    assert undelivered[0]["alert_rule"] == "price_drop_pct"

    temp_store.mark_alert_delivered(alert_id)
    assert len(temp_store.get_undelivered_alerts()) == 0
