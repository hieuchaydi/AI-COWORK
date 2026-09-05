"""
Profit Guard Engine and Human-in-the-Loop Coordinator.
Manages cost profiles, evaluates pricing recommendations, handles approvals,
records audit history, and coordinates official Seller API updates.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import time
from typing import Any, Dict, List, Optional

from .calculator import ProfitGuardCalculator
from .models import (
    ApprovalStatus,
    AuditAction,
    CostProfile,
    PriceRecommendation,
    PriceUpdateResult,
    PricingAuditLogEntry,
)
from .seller_updater import SellerUpdateRegistry
from ..delivery.dispatcher import AlertDispatcher
from ..storage.sqlite_store import CommerceSqliteStore

logger = logging.getLogger(__name__)


class ProfitGuardEngine:
    """
    Coordinates Profit Guard business logic, lifecycle transitions,
    and official seller integration.
    """

    def __init__(
        self,
        store: Optional[CommerceSqliteStore] = None,
        registry: Optional[SellerUpdateRegistry] = None,
        dispatcher: Optional[AlertDispatcher] = None,
        default_expiry_hours: int = 24,
    ):
        self.store = store or CommerceSqliteStore()
        self.registry = registry or SellerUpdateRegistry()
        self.dispatcher = dispatcher or AlertDispatcher()
        self.default_expiry_hours = default_expiry_hours

    def configure_sku(self, profile: CostProfile) -> Dict[str, Any]:
        """Saves or updates the cost profile for a SKU."""
        self.store.upsert_cost_profile(profile)
        self.store.record_audit_log(
            PricingAuditLogEntry(
                sku_id=profile.sku_id,
                action=AuditAction.RECOMMENDATION_CREATED,
                actor="system",
                input_snapshot={"configured_profile": profile.model_dump(mode="json")},
                formula_metadata={"action": "configure_sku"},
                result={"status": "profile_configured"},
            )
        )
        return {
            "ok": True,
            "sku_id": profile.sku_id,
            "message": f"Cost profile configured successfully for SKU '{profile.sku_id}'.",
        }

    def generate_recommendation(
        self,
        sku_id: str,
        current_price: Decimal,
        market_price: Optional[Decimal] = None,
        actor: str = "scheduler",
    ) -> Dict[str, Any]:
        """
        Generates a competitive pricing recommendation for a SKU.
        Validates cost profile, enforces cooldown and deduplication,
        and creates a PENDING approval record.
        """
        profile = self.store.get_cost_profile(sku_id)
        if not profile or not profile.is_active:
            return {
                "ok": False,
                "error": "PROFILE_NOT_FOUND",
                "message": f"No active Profit Guard profile found for SKU '{sku_id}'. Please configure it first.",
            }

        now = datetime.now(timezone.utc)

        # Cooldown & Deduplication check
        latest_rec = self.store.get_latest_recommendation(sku_id)
        if latest_rec:
            # Check if within cooldown
            time_since_last = (now - latest_rec.created_at).total_seconds()
            data_unchanged = (
                latest_rec.current_price == current_price
                and latest_rec.market_price == market_price
            )

            if time_since_last < profile.cooldown_seconds and data_unchanged:
                return {
                    "ok": True,
                    "status": "cooldown_active",
                    "reused": True,
                    "message": (
                        f"Cooldown active ({profile.cooldown_seconds - int(time_since_last)}s remaining) "
                        "and market data has not changed. Reusing existing recommendation."
                    ),
                    "recommendation": latest_rec.model_dump(mode="json"),
                }

        # Calculate recommendation
        calc_result = ProfitGuardCalculator.calculate_recommendation(
            profile=profile,
            current_price=current_price,
            market_price=market_price,
        )

        expires_at = now + timedelta(hours=self.default_expiry_hours)
        rec = PriceRecommendation(
            sku_id=sku_id,
            platform=profile.platform,
            current_price=current_price,
            market_price=market_price,
            breakeven_price=calc_result["breakeven_price"],
            floor_price=calc_result["floor_price"],
            recommended_price=calc_result["recommended_price"],
            profit_amount=calc_result["profit_amount"],
            profit_margin_pct=calc_result["profit_margin_pct"],
            reason=calc_result["reason"],
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
            created_at=now,
        )

        rec_id = self.store.save_recommendation(rec)
        rec.id = rec_id

        # Record audit entry
        self.store.record_audit_log(
            PricingAuditLogEntry(
                recommendation_id=rec_id,
                sku_id=sku_id,
                action=AuditAction.RECOMMENDATION_CREATED,
                actor=actor,
                input_snapshot={
                    "current_price": str(current_price),
                    "market_price": str(market_price) if market_price else None,
                    "cost_price": str(profile.cost_price),
                    "platform_fee_pct": str(profile.platform_fee_pct),
                    "min_margin_pct": str(profile.min_margin_pct),
                },
                formula_metadata={
                    "formula": "P_floor = (Cost + FixedFees) / (1 - VariableFees - MinMargin)",
                    "breakeven_price": str(rec.breakeven_price),
                    "floor_price": str(rec.floor_price),
                },
                result={
                    "recommended_price": str(rec.recommended_price),
                    "profit_amount": str(rec.profit_amount),
                    "profit_margin_pct": str(rec.profit_margin_pct),
                },
            )
        )

        return {
            "ok": True,
            "status": "pending_approval",
            "recommendation": rec.model_dump(mode="json"),
        }

    def list_pending_approvals(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Lists pending approvals and automatically marks expired ones as EXPIRED.
        """
        now = datetime.now(timezone.utc)
        all_pending = self.store.list_recommendations(status=ApprovalStatus.PENDING)
        valid_pending = []

        for rec in all_pending:
            if platform and rec.platform.lower() != platform.lower():
                continue

            if rec.expires_at <= now:
                # Mark expired
                self.store.update_recommendation_status(
                    rec.id,
                    status=ApprovalStatus.EXPIRED,
                    expected_status=ApprovalStatus.PENDING,
                )
                self.store.record_audit_log(
                    PricingAuditLogEntry(
                        recommendation_id=rec.id,
                        sku_id=rec.sku_id,
                        action=AuditAction.EXPIRED,
                        actor="system",
                        input_snapshot={"expired_at_threshold": rec.expires_at.isoformat()},
                        formula_metadata={},
                        result={"status": "EXPIRED"},
                    )
                )
            else:
                valid_pending.append(rec.model_dump(mode="json"))

        return valid_pending

    async def approve_price_change(
        self,
        recommendation_id: int,
        approver: str = "seller_admin",
        apply_immediately: bool = True,
    ) -> Dict[str, Any]:
        """
        Approves a pending price recommendation.
        If apply_immediately is True, attempts to update price via official Seller API.
        """
        rec = self.store.get_recommendation(recommendation_id)
        if not rec:
            return {"ok": False, "error": "NOT_FOUND", "message": f"Recommendation #{recommendation_id} not found."}

        now = datetime.now(timezone.utc)
        if rec.expires_at <= now:
            self.store.update_recommendation_status(
                rec.id, status=ApprovalStatus.EXPIRED, expected_status=ApprovalStatus.PENDING
            )
            return {"ok": False, "error": "EXPIRED", "message": f"Recommendation #{recommendation_id} has expired."}

        if rec.status != ApprovalStatus.PENDING:
            return {
                "ok": False,
                "error": "INVALID_STATUS",
                "message": f"Cannot approve recommendation with status '{rec.status.value}'. Must be 'pending'.",
            }

        # Atomically transition to APPROVED
        updated = self.store.update_recommendation_status(
            rec.id,
            status=ApprovalStatus.APPROVED,
            approver=approver,
            expected_status=ApprovalStatus.PENDING,
        )
        if not updated:
            return {"ok": False, "error": "CONCURRENT_UPDATE", "message": "Recommendation was already processed."}

        self.store.record_audit_log(
            PricingAuditLogEntry(
                recommendation_id=rec.id,
                sku_id=rec.sku_id,
                action=AuditAction.APPROVED,
                actor=approver,
                input_snapshot={"recommended_price": str(rec.recommended_price)},
                formula_metadata={},
                result={"status": "APPROVED", "approver": approver},
            )
        )

        if apply_immediately:
            return await self.apply_price_change(recommendation_id, actor=approver)

        return {
            "ok": True,
            "status": "approved",
            "message": f"Recommendation #{recommendation_id} approved. Pending Seller API application.",
        }

    def reject_price_change(
        self,
        recommendation_id: int,
        approver: str = "seller_admin",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Rejects a pending price recommendation with an optional reason."""
        rec = self.store.get_recommendation(recommendation_id)
        if not rec:
            return {"ok": False, "error": "NOT_FOUND", "message": f"Recommendation #{recommendation_id} not found."}

        if rec.status != ApprovalStatus.PENDING:
            return {
                "ok": False,
                "error": "INVALID_STATUS",
                "message": f"Cannot reject recommendation with status '{rec.status.value}'. Must be 'pending'.",
            }

        updated = self.store.update_recommendation_status(
            rec.id,
            status=ApprovalStatus.REJECTED,
            approver=approver,
            rejection_reason=reason,
            expected_status=ApprovalStatus.PENDING,
        )
        if not updated:
            return {"ok": False, "error": "CONCURRENT_UPDATE", "message": "Recommendation was already processed."}

        self.store.record_audit_log(
            PricingAuditLogEntry(
                recommendation_id=rec.id,
                sku_id=rec.sku_id,
                action=AuditAction.REJECTED,
                actor=approver,
                input_snapshot={"recommended_price": str(rec.recommended_price)},
                formula_metadata={"rejection_reason": reason},
                result={"status": "REJECTED", "approver": approver},
            )
        )

        return {
            "ok": True,
            "status": "rejected",
            "message": f"Recommendation #{recommendation_id} rejected successfully.",
        }

    async def apply_price_change(self, recommendation_id: int, actor: str = "system") -> Dict[str, Any]:
        """
        Applies an approved price change to the official Seller API.
        Enforces fail-closed authorization checks and persistent backoff.
        """
        rec = self.store.get_recommendation(recommendation_id)
        if not rec:
            return {"ok": False, "error": "NOT_FOUND", "message": f"Recommendation #{recommendation_id} not found."}

        if rec.status == ApprovalStatus.APPLIED:
            return {
                "ok": False,
                "error": "ALREADY_APPLIED",
                "message": f"Recommendation #{recommendation_id} has already been applied.",
            }

        if rec.status != ApprovalStatus.APPROVED:
            return {
                "ok": False,
                "error": "NOT_APPROVED",
                "message": (
                    f"Cannot apply recommendation with status '{rec.status.value}'. "
                    "Only 'approved' recommendations can be applied."
                ),
            }

        profile = self.store.get_cost_profile(rec.sku_id)
        if not profile:
            return {
                "ok": False,
                "error": "PROFILE_NOT_FOUND",
                "message": f"Missing cost profile for SKU '{rec.sku_id}'.",
            }

        # 1. Fail-closed: Must have designated seller provider
        provider_name = profile.seller_provider
        if not provider_name:
            err_msg = f"No Seller API provider configured for SKU '{rec.sku_id}'."
            self._record_apply_failure(rec, actor, "NO_SELLER_PROVIDER", err_msg)
            return {"ok": False, "error": "NO_SELLER_PROVIDER", "message": err_msg}

        provider = self.registry.get(provider_name)
        if not provider:
            err_msg = f"Designated Seller API provider '{provider_name}' is not registered in registry."
            self._record_apply_failure(rec, actor, "PROVIDER_NOT_REGISTERED", err_msg)
            return {"ok": False, "error": "PROVIDER_NOT_REGISTERED", "message": err_msg}

        # 2. Fail-closed: Must have valid authorization_ref
        auth_ref = profile.authorization_ref
        if not auth_ref:
            err_msg = f"Missing authorization reference for official Seller API provider '{provider_name}'."
            self._record_apply_failure(rec, actor, "MISSING_AUTHORIZATION", err_msg)
            return {"ok": False, "error": "MISSING_AUTHORIZATION", "message": err_msg}

        # 3. Persistent backoff check (stored in SQLite, persists across process restarts)
        backoff_until, failure_count = self.store.get_provider_backoff(provider_name)
        now_ts = time.time()
        if now_ts < backoff_until:
            wait_s = int(backoff_until - now_ts)
            err_msg = f"Provider '{provider_name}' is in active backoff for {wait_s}s due to recent transient errors."
            return {"ok": False, "error": "PROVIDER_BACKED_OFF", "message": err_msg, "retry_after": wait_s}

        # 4. Execute price update
        idempotency_key = f"rec_{rec.id}_{int(rec.recommended_price)}"
        try:
            result = await provider.update_price(
                sku_id=rec.sku_id,
                new_price=rec.recommended_price,
                authorization_ref=auth_ref,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            # Transient error caught -> compute exponential backoff and record in SQLite
            new_failure_count = failure_count + 1
            backoff_duration = min(3600.0, (2 ** new_failure_count) * 10.0)
            new_backoff_until = now_ts + backoff_duration
            self.store.record_provider_backoff(
                provider_name,
                backoff_until=new_backoff_until,
                failure_count=new_failure_count,
                last_error=str(exc),
            )
            err_msg = f"Transient provider failure: {exc}. Backing off for {int(backoff_duration)}s."
            self._record_apply_failure(rec, actor, "TRANSIENT_ERROR", err_msg)
            return {"ok": False, "error": "TRANSIENT_ERROR", "message": err_msg}

        if not result.success:
            self._record_apply_failure(rec, actor, result.error_code or "PROVIDER_ERROR", result.error or "Unknown error")
            return {
                "ok": False,
                "error": result.error_code or "PROVIDER_ERROR",
                "message": result.error,
            }

        # 5. Success: Clear provider backoff and transition to APPLIED
        self.store.clear_provider_backoff(provider_name)
        applied_at = datetime.now(timezone.utc)
        self.store.update_recommendation_status(
            rec.id,
            status=ApprovalStatus.APPLIED,
            applied_at=applied_at,
            expected_status=ApprovalStatus.APPROVED,
        )

        self.store.record_audit_log(
            PricingAuditLogEntry(
                recommendation_id=rec.id,
                sku_id=rec.sku_id,
                action=AuditAction.APPLIED,
                actor=actor,
                input_snapshot={"applied_price": str(rec.recommended_price), "provider": provider_name},
                formula_metadata={"transaction_id": result.transaction_id},
                result={"status": "APPLIED", "transaction_id": result.transaction_id},
            )
        )

        return {
            "ok": True,
            "status": "applied",
            "applied_price": str(rec.recommended_price),
            "provider": provider_name,
            "transaction_id": result.transaction_id,
            "message": f"Successfully updated price for SKU '{rec.sku_id}' to {rec.recommended_price:,}đ.",
        }

    def get_audit_history(self, sku_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves audit trail entries."""
        logs = self.store.get_audit_history(sku_id=sku_id, limit=limit)
        return [entry.model_dump(mode="json") for entry in logs]

    def _record_apply_failure(self, rec: PriceRecommendation, actor: str, error_code: str, error_msg: str):
        self.store.update_recommendation_status(rec.id, status=ApprovalStatus.FAILED)
        self.store.record_audit_log(
            PricingAuditLogEntry(
                recommendation_id=rec.id,
                sku_id=rec.sku_id,
                action=AuditAction.APPLY_FAILED,
                actor=actor,
                input_snapshot={"target_price": str(rec.recommended_price)},
                formula_metadata={"error_code": error_code},
                result={"status": "FAILED", "error": error_msg},
            )
        )
