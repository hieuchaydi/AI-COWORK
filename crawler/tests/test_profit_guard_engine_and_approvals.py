"""Unit tests for Profit Guard Engine, Human-in-the-loop approvals, and audit logging."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from crawler.commerce.profit_guard.engine import ProfitGuardEngine
from crawler.commerce.profit_guard.models import (
    ApprovalStatus,
    AuditAction,
    CostProfile,
)
from crawler.commerce.profit_guard.seller_updater import (
    MockOfficialSellerApiProvider,
    SellerUpdateRegistry,
)
from crawler.commerce.storage.sqlite_store import CommerceSqliteStore


@pytest.fixture
def test_setup(tmp_path: Path):
    db_file = str(tmp_path / "test_profit_guard.db")
    store = CommerceSqliteStore(db_file)
    registry = SellerUpdateRegistry()
    mock_provider = MockOfficialSellerApiProvider("mock_seller_api")
    registry.register(mock_provider)

    engine = ProfitGuardEngine(store=store, registry=registry, default_expiry_hours=24)
    return engine, store, registry, mock_provider, db_file


@pytest.fixture
def configured_profile(test_setup) -> CostProfile:
    engine, store, _, _, _ = test_setup
    profile = CostProfile(
        sku_id="SKU-PHONE-01",
        platform="tiki",
        product_id="prod_phone",
        cost_price=Decimal("10000000"),
        platform_fee_pct=Decimal("0.08"),
        platform_fee_fixed=Decimal("10000"),
        tax_pct=Decimal("0.015"),
        additional_cost=Decimal("20000"),
        min_margin_pct=Decimal("0.15"),
        max_price_change_pct=Decimal("0.10"),
        cooldown_seconds=1800,
        authorization_ref="auth_token_valid_123",
        seller_provider="mock_seller_api",
    )
    engine.configure_sku(profile)
    return profile


def test_cost_profile_crud_and_persistence(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup
    loaded = store.get_cost_profile("SKU-PHONE-01")
    assert loaded is not None
    assert loaded.cost_price == Decimal("10000000")
    assert loaded.seller_provider == "mock_seller_api"
    assert loaded.authorization_ref == "auth_token_valid_123"


def test_recommendation_generation_and_cooldown(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup

    # 1. Generate recommendation
    res1 = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15500000"),
    )
    assert res1["ok"] is True
    assert res1["status"] == "pending_approval"
    rec1 = res1["recommendation"]
    assert rec1["recommended_price"] == "15500000"

    # 2. Duplicate check within cooldown (identical prices) -> Reuses existing
    res2 = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15500000"),
    )
    assert res2["ok"] is True
    assert res2.get("reused") is True
    assert res2["status"] == "cooldown_active"


@pytest.mark.asyncio
async def test_full_approval_and_apply_lifecycle(test_setup, configured_profile):
    engine, store, _, mock_provider, _ = test_setup

    res = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15200000"),
    )
    rec_id = res["recommendation"]["id"]

    # Check pending list
    pending = engine.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["id"] == rec_id

    # Approve and apply
    app_res = await engine.approve_price_change(
        recommendation_id=rec_id,
        approver="admin_alice",
        apply_immediately=True,
    )
    assert app_res["ok"] is True
    assert app_res["status"] == "applied"
    assert "Successfully updated price" in app_res["message"]

    # Verify mock provider received call
    assert len(mock_provider.call_history) == 1
    call = mock_provider.call_history[0]
    assert call["sku_id"] == "SKU-PHONE-01"
    assert call["new_price"] == Decimal("15200000")
    assert call["authorization_ref"] == "auth_token_valid_123"

    # Verify recommendation status in database
    rec_db = store.get_recommendation(rec_id)
    assert rec_db.status == ApprovalStatus.APPLIED
    assert rec_db.approver == "admin_alice"
    assert rec_db.applied_at is not None

    # Verify no more pending
    assert len(engine.list_pending_approvals()) == 0


@pytest.mark.asyncio
async def test_duplicate_apply_prevention(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup

    res = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15000000"),
    )
    rec_id = res["recommendation"]["id"]

    # First approve and apply
    await engine.approve_price_change(recommendation_id=rec_id, approver="admin_alice", apply_immediately=True)

    # Attempt second apply
    dup_res = await engine.apply_price_change(recommendation_id=rec_id)
    assert dup_res["ok"] is False
    assert dup_res["error"] == "ALREADY_APPLIED"


def test_rejection_flow(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup

    res = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("14000000"),
    )
    rec_id = res["recommendation"]["id"]

    rej_res = engine.reject_price_change(
        recommendation_id=rec_id,
        approver="manager_bob",
        reason="Market is expected to recover next week",
    )
    assert rej_res["ok"] is True
    assert rej_res["status"] == "rejected"

    rec_db = store.get_recommendation(rec_id)
    assert rec_db.status == ApprovalStatus.REJECTED
    assert rec_db.rejection_reason == "Market is expected to recover next week"


def test_expired_approval_handling(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup

    res = engine.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15000000"),
    )
    rec_id = res["recommendation"]["id"]

    # Manually backdate expiration in DB to simulate expired recommendation
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with store._get_connection() as conn:
        conn.execute("UPDATE price_recommendations SET expires_at = ? WHERE id = ?", (past_iso, rec_id))
        conn.commit()

    # Listing pending should clean up expired
    pending = engine.list_pending_approvals()
    assert len(pending) == 0

    rec_db = store.get_recommendation(rec_id)
    assert rec_db.status == ApprovalStatus.EXPIRED


@pytest.mark.asyncio
async def test_fail_closed_missing_authorization(test_setup):
    engine, store, _, _, _ = test_setup

    # Profile without authorization_ref
    profile = CostProfile(
        sku_id="SKU-NO-AUTH",
        platform="shopee",
        product_id="prod_shopee_1",
        cost_price=Decimal("500000"),
        min_margin_pct=Decimal("0.10"),
        seller_provider="mock_seller_api",
        authorization_ref=None,  # Missing!
    )
    engine.configure_sku(profile)

    res = engine.generate_recommendation(sku_id="SKU-NO-AUTH", current_price=Decimal("800000"))
    rec_id = res["recommendation"]["id"]

    # Approve
    store.update_recommendation_status(rec_id, status=ApprovalStatus.APPROVED)

    # Apply must fail-closed safely
    apply_res = await engine.apply_price_change(rec_id)
    assert apply_res["ok"] is False
    assert apply_res["error"] == "MISSING_AUTHORIZATION"

    rec_db = store.get_recommendation(rec_id)
    assert rec_db.status == ApprovalStatus.FAILED


@pytest.mark.asyncio
async def test_fail_closed_unregistered_seller_provider(test_setup):
    engine, store, _, _, _ = test_setup

    profile = CostProfile(
        sku_id="SKU-UNREG-PROV",
        platform="shopee",
        product_id="prod_shopee_2",
        cost_price=Decimal("500000"),
        min_margin_pct=Decimal("0.10"),
        seller_provider="unregistered_provider_xyz",  # Not registered!
        authorization_ref="token_abc",
    )
    engine.configure_sku(profile)

    res = engine.generate_recommendation(sku_id="SKU-UNREG-PROV", current_price=Decimal("800000"))
    rec_id = res["recommendation"]["id"]

    store.update_recommendation_status(rec_id, status=ApprovalStatus.APPROVED)
    apply_res = await engine.apply_price_change(rec_id)
    assert apply_res["ok"] is False
    assert apply_res["error"] == "PROVIDER_NOT_REGISTERED"


@pytest.mark.asyncio
async def test_transient_error_backoff_and_process_restart_persistence(test_setup, configured_profile):
    engine1, store1, registry1, mock_provider, db_file = test_setup
    mock_provider.simulate_transient_error = True  # Inject 503 error

    res = engine1.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15000000"),
    )
    rec_id = res["recommendation"]["id"]
    store1.update_recommendation_status(rec_id, status=ApprovalStatus.APPROVED)

    # 1. Apply triggers transient failure and sets backoff in SQLite
    apply_res = await engine1.apply_price_change(rec_id)
    assert apply_res["ok"] is False
    assert apply_res["error"] == "TRANSIENT_ERROR"
    assert "Backing off" in apply_res["message"]

    # 2. Simulate complete process restart with new engine instance pointing to same db_file
    new_store = CommerceSqliteStore(db_file)
    new_registry = SellerUpdateRegistry()
    new_mock_provider = MockOfficialSellerApiProvider("mock_seller_api")
    new_registry.register(new_mock_provider)
    engine2 = ProfitGuardEngine(store=new_store, registry=new_registry)

    # 3. Next attempt on new engine must be rejected immediately by persistent backoff
    res2 = engine2.generate_recommendation(
        sku_id="SKU-PHONE-01",
        current_price=Decimal("16000000"),
        market_price=Decimal("15000000"),
    )
    rec_id2 = res2["recommendation"]["id"]
    new_store.update_recommendation_status(rec_id2, status=ApprovalStatus.APPROVED)

    apply_res2 = await engine2.apply_price_change(rec_id2)
    assert apply_res2["ok"] is False
    assert apply_res2["error"] == "PROVIDER_BACKED_OFF"
    assert "in active backoff" in apply_res2["message"]


def test_pricing_audit_log_completeness(test_setup, configured_profile):
    engine, store, _, _, _ = test_setup

    audit_logs = engine.get_audit_history(sku_id="SKU-PHONE-01")
    assert len(audit_logs) >= 1
    # Check that entries have immutable timestamp and actor
    entry = audit_logs[0]
    assert entry["sku_id"] == "SKU-PHONE-01"
    assert "created_at" in entry
    assert "actor" in entry
