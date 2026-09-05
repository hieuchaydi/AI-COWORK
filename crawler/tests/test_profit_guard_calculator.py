"""Unit tests for Profit Guard pricing calculator and financial formulas."""

from decimal import Decimal
import pytest

from crawler.commerce.profit_guard.calculator import ProfitGuardCalculator
from crawler.commerce.profit_guard.models import CostProfile


@pytest.fixture
def sample_profile() -> CostProfile:
    return CostProfile(
        sku_id="SKU-TEST-001",
        platform="tiki",
        product_id="prod_123",
        cost_price=Decimal("100000"),  # 100,000 VND
        platform_fee_pct=Decimal("0.08"),  # 8%
        platform_fee_fixed=Decimal("2000"),  # 2,000 VND
        tax_pct=Decimal("0.015"),  # 1.5%
        additional_cost=Decimal("3000"),  # 3,000 VND
        min_margin_pct=Decimal("0.15"),  # 15% minimum net margin
        max_price_change_pct=Decimal("0.10"),  # 10% max step
    )


def test_cost_profile_validation_invalid_margin():
    # Fee + tax + margin >= 100% must fail validation
    with pytest.raises(ValueError) as exc:
        CostProfile(
            sku_id="SKU-BAD",
            platform="tiki",
            product_id="prod_bad",
            cost_price=Decimal("100000"),
            platform_fee_pct=Decimal("0.70"),
            tax_pct=Decimal("0.15"),
            min_margin_pct=Decimal("0.20"),  # 70 + 15 + 20 = 105% > 100%
        )
    assert "mathematically impossible" in str(exc.value)


def test_breakeven_price_calculation(sample_profile: CostProfile):
    # Fixed costs: 100,000 + 2,000 + 3,000 = 105,000 VND
    # Variable rate: 0.08 + 0.015 = 0.095 (9.5%)
    # Retained ratio: 1 - 0.095 = 0.905
    # Breakeven = 105,000 / 0.905 = 116,022.099... -> CEIL = 116,023 VND
    breakeven = ProfitGuardCalculator.calculate_breakeven_price(sample_profile)
    assert breakeven == Decimal("116023")

    # Profit at breakeven price must be >= 0
    profit, margin = ProfitGuardCalculator.calculate_profit(breakeven, sample_profile)
    assert profit >= Decimal("0")


def test_floor_price_calculation(sample_profile: CostProfile):
    # Fixed costs = 105,000 VND
    # Variable rate + min margin = 0.095 + 0.15 = 0.245
    # Retained margin ratio = 1 - 0.245 = 0.755
    # Floor price = 105,000 / 0.755 = 139,072.847... -> CEIL = 139,073 VND
    floor_price = ProfitGuardCalculator.calculate_floor_price(sample_profile)
    assert floor_price == Decimal("139073")

    # Margin at floor price must be >= 15% (0.1500)
    profit, margin = ProfitGuardCalculator.calculate_profit(floor_price, sample_profile)
    assert margin >= Decimal("0.1500")


def test_competitor_dumps_below_floor_never_underbid(sample_profile: CostProfile):
    """
    CRITICAL ACCEPTANCE CRITERIA:
    Competitor sells at 120,000 VND (which is below our floor 139,073 VND).
    Profit Guard MUST NEVER recommend below floor price!
    """
    current_price = Decimal("160000")
    competitor_price = Decimal("120000")

    rec = ProfitGuardCalculator.calculate_recommendation(
        profile=sample_profile,
        current_price=current_price,
        market_price=competitor_price,
    )

    floor_price = ProfitGuardCalculator.calculate_floor_price(sample_profile)
    assert rec["floor_price"] == floor_price
    assert rec["recommended_price"] == floor_price
    assert rec["recommended_price"] >= floor_price
    assert "Kích hoạt Profit Guard" in rec["reason"]
    assert rec["profit_margin_pct"] >= Decimal("0.1500")


def test_competitive_match_within_safe_margin(sample_profile: CostProfile):
    current_price = Decimal("160000")
    competitor_price = Decimal("150000")  # Above floor (139,073), drop is (160k - 150k)/160k = 6.25% <= 10%

    rec = ProfitGuardCalculator.calculate_recommendation(
        profile=sample_profile,
        current_price=current_price,
        market_price=competitor_price,
    )

    assert rec["recommended_price"] == Decimal("150000")
    assert rec["profit_margin_pct"] >= Decimal("0.1500")
    assert "khớp giá cạnh tranh" in rec["reason"].lower()


def test_competitive_step_drop_limit(sample_profile: CostProfile):
    current_price = Decimal("200000")
    # Competitor is at 145,000 VND (above floor 139k), but drop is 27.5% > max_step (10%)
    # Max drop allowed is 10%: 200,000 * 0.9 = 180,000 VND
    competitor_price = Decimal("145000")

    rec = ProfitGuardCalculator.calculate_recommendation(
        profile=sample_profile,
        current_price=current_price,
        market_price=competitor_price,
    )

    assert rec["recommended_price"] == Decimal("180000")
    assert "tối đa cho phép" in rec["reason"]


def test_market_price_increase_captures_margin(sample_profile: CostProfile):
    current_price = Decimal("150000")
    # Competitor increases to 162,000 (8% increase <= 10% max step)
    competitor_price = Decimal("162000")

    rec = ProfitGuardCalculator.calculate_recommendation(
        profile=sample_profile,
        current_price=current_price,
        market_price=competitor_price,
    )

    assert rec["recommended_price"] == Decimal("162000")
    assert "Thị trường tăng giá" in rec["reason"]


def test_current_price_below_floor_without_market_data(sample_profile: CostProfile):
    # If seller current price is 130,000 (below floor 139,073)
    current_price = Decimal("130000")
    floor_price = ProfitGuardCalculator.calculate_floor_price(sample_profile)

    rec = ProfitGuardCalculator.calculate_recommendation(
        profile=sample_profile,
        current_price=current_price,
        market_price=None,
    )

    assert rec["recommended_price"] == floor_price
    assert "thấp hơn giá sàn bảo vệ" in rec["reason"]
