"""
Domain models for Profit Guard pricing protection and approvals.
All financial calculations use Decimal with strict Pydantic validation.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    APPLIED = "applied"
    FAILED = "failed"


class AuditAction(str, Enum):
    RECOMMENDATION_CREATED = "RECOMMENDATION_CREATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"
    EXPIRED = "EXPIRED"


class CostProfile(BaseModel):
    """
    Cost and profit parameters for a specific product SKU.
    """

    model_config = ConfigDict(extra="forbid")

    sku_id: str = Field(..., min_length=1, description="Unique SKU identifier")
    platform: str = Field(..., min_length=1, description="E-commerce platform (e.g., tiki, shopee)")
    product_id: str = Field(..., min_length=1, description="Platform product ID")
    cost_price: Decimal = Field(..., gt=Decimal("0"), description="Cost of Goods Sold (COGS)")
    platform_fee_pct: Decimal = Field(
        default=Decimal("0.08"),
        ge=Decimal("0"),
        lt=Decimal("1"),
        description="Platform take-rate percentage (e.g. 0.08 for 8%)",
    )
    platform_fee_fixed: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="Fixed platform fee per transaction (e.g. 5000 VND)",
    )
    tax_pct: Decimal = Field(
        default=Decimal("0.015"),
        ge=Decimal("0"),
        lt=Decimal("1"),
        description="Tax percentage on selling price (e.g. 0.015 for 1.5% TNCN/VAT)",
    )
    additional_cost: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="Additional packaging, handling or shipping cost per item",
    )
    min_margin_pct: Decimal = Field(
        default=Decimal("0.15"),
        ge=Decimal("0"),
        lt=Decimal("1"),
        description="Minimum net profit margin on selling price (e.g. 0.15 for 15%)",
    )
    max_price_change_pct: Decimal = Field(
        default=Decimal("0.10"),
        gt=Decimal("0"),
        le=Decimal("1"),
        description="Maximum allowed price change percentage per adjustment (e.g. 0.10 for 10%)",
    )
    cooldown_seconds: int = Field(
        default=3600,
        ge=0,
        description="Minimum cooldown in seconds between recommendations",
    )
    authorization_ref: Optional[str] = Field(
        default=None,
        description="Authorization reference or API token ID for official Seller API updates",
    )
    seller_provider: Optional[str] = Field(
        default=None,
        description="Designated seller API provider name (e.g. 'shopee_open_api')",
    )
    is_active: bool = Field(default=True, description="Whether Profit Guard is active for this SKU")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("cost_price", "platform_fee_pct", "platform_fee_fixed", "tax_pct", "additional_cost", "min_margin_pct", "max_price_change_pct", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @model_validator(mode="after")
    def validate_variable_fees_and_margin(self) -> "CostProfile":
        total_rate = self.platform_fee_pct + self.tax_pct + self.min_margin_pct
        if total_rate >= Decimal("1.0"):
            raise ValueError(
                f"Sum of platform fee ({self.platform_fee_pct}), tax ({self.tax_pct}), "
                f"and minimum margin ({self.min_margin_pct}) is {total_rate:.4f} >= 1.0 (100%). "
                "It is mathematically impossible to achieve this profit margin."
            )
        return self


class PriceRecommendation(BaseModel):
    """
    Generated price recommendation pending human-in-the-loop review.
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[int] = None
    sku_id: str
    platform: str
    current_price: Decimal
    market_price: Optional[Decimal] = None
    breakeven_price: Decimal
    floor_price: Decimal
    recommended_price: Decimal
    profit_amount: Decimal
    profit_margin_pct: Decimal
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    expires_at: datetime
    approver: Optional[str] = None
    rejection_reason: Optional[str] = None
    applied_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("current_price", "market_price", "breakeven_price", "floor_price", "recommended_price", "profit_amount", "profit_margin_pct", mode="before")
    @classmethod
    def parse_decimal(cls, v: Any) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @model_validator(mode="after")
    def validate_floor_protection(self) -> "PriceRecommendation":
        if self.recommended_price < self.floor_price:
            raise ValueError(
                f"CRITICAL: Recommended price ({self.recommended_price}) is strictly lower than "
                f"profit floor protection price ({self.floor_price})."
            )
        return self


class PricingAuditLogEntry(BaseModel):
    """
    Immutable audit log entry for price recommendation lifecycle events.
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[int] = None
    recommendation_id: Optional[int] = None
    sku_id: str
    action: AuditAction
    actor: str
    input_snapshot: Dict[str, Any] = Field(default_factory=dict)
    formula_metadata: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PriceUpdateResult(BaseModel):
    """
    Result of an official Seller API price update execution.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    sku_id: str
    applied_price: Optional[Decimal] = None
    provider: str = ""
    error: Optional[str] = None
    error_code: Optional[str] = None
    transaction_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
