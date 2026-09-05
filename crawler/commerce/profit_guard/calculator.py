"""
Pricing Calculator for Profit Guard.
Performs exact Decimal arithmetic for breakeven, floor price, and competitive pricing.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple


from .models import CostProfile


class ProfitGuardCalculator:
    """
    Handles financial calculations for price floor protection,
    break-even thresholds, and competitive adjustments.
    """

    CURRENCY_UNIT = Decimal("1")  # Integer unit for VND prices

    @classmethod
    def calculate_fixed_costs(cls, profile: CostProfile) -> Decimal:
        """Total fixed cost per unit: Cost Price + Fixed Fee + Additional Cost."""
        return profile.cost_price + profile.platform_fee_fixed + profile.additional_cost

    @classmethod
    def calculate_variable_rate(cls, profile: CostProfile) -> Decimal:
        """Total variable percentage deduction from selling price: Platform Fee % + Tax %."""
        return profile.platform_fee_pct + profile.tax_pct

    @classmethod
    def calculate_breakeven_price(cls, profile: CostProfile) -> Decimal:
        """
        Price at which net profit is zero:
        P_breakeven = Fixed_Costs / (1 - Variable_Rate)
        Uses ROUND_CEILING so net profit is guaranteed >= 0.
        """
        fixed_costs = cls.calculate_fixed_costs(profile)
        variable_rate = cls.calculate_variable_rate(profile)
        net_retained_ratio = Decimal("1.0") - variable_rate

        if net_retained_ratio <= Decimal("0"):
            raise ValueError(f"Variable fee rate ({variable_rate}) must be strictly less than 1.0 (100%).")

        breakeven = fixed_costs / net_retained_ratio
        return breakeven.quantize(cls.CURRENCY_UNIT, rounding=ROUND_CEILING)

    @classmethod
    def calculate_floor_price(cls, profile: CostProfile) -> Decimal:
        """
        Minimum selling price required to achieve at least min_margin_pct:
        P_floor = Fixed_Costs / (1 - Variable_Rate - Min_Margin_Pct)
        Uses ROUND_CEILING so margin is guaranteed >= min_margin_pct.
        """
        fixed_costs = cls.calculate_fixed_costs(profile)
        variable_rate = cls.calculate_variable_rate(profile)
        retained_margin_ratio = Decimal("1.0") - variable_rate - profile.min_margin_pct

        if retained_margin_ratio <= Decimal("0"):
            raise ValueError(
                f"Variable fees ({variable_rate}) + min margin ({profile.min_margin_pct}) "
                f"exceed 1.0. Impossible to achieve required margin."
            )

        floor = fixed_costs / retained_margin_ratio
        return floor.quantize(cls.CURRENCY_UNIT, rounding=ROUND_CEILING)

    @classmethod
    def calculate_profit(cls, price: Decimal, profile: CostProfile) -> Tuple[Decimal, Decimal]:
        """
        Calculates net cash profit and profit margin percentage for a given selling price.
        Returns: (profit_amount, profit_margin_pct)
        """
        fixed_costs = cls.calculate_fixed_costs(profile)
        variable_rate = cls.calculate_variable_rate(profile)

        net_revenue = price * (Decimal("1.0") - variable_rate)
        profit_amount = net_revenue - fixed_costs

        margin_pct = Decimal("0")
        if price > Decimal("0"):
            margin_pct = profit_amount / price

        return (
            profit_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            margin_pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        )

    @classmethod
    def calculate_recommendation(
        cls,
        profile: CostProfile,
        current_price: Decimal,
        market_price: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """
        Calculates competitive recommendation while guaranteeing profit protection.
        Never recommends a price below floor_price.
        """
        breakeven_price = cls.calculate_breakeven_price(profile)
        floor_price = cls.calculate_floor_price(profile)

        max_step_pct = profile.max_price_change_pct
        max_allowed_drop = current_price * (Decimal("1.0") - max_step_pct)
        max_allowed_increase = current_price * (Decimal("1.0") + max_step_pct)

        # Base case: No market/competitor price provided
        if market_price is None or market_price <= Decimal("0"):
            if current_price < floor_price:
                recommended = floor_price
                reason = (
                    f"Giá hiện tại ({current_price:,}đ) thấp hơn giá sàn bảo vệ lợi nhuận ({floor_price:,}đ). "
                    f"Đề xuất tăng giá lên mức sàn để đảm bảo biên lợi nhuận tối thiểu {profile.min_margin_pct * 100:.1f}%."
                )
            else:
                recommended = current_price
                reason = "Không có dữ liệu đối thủ cạnh tranh; giá hiện tại đạt chuẩn bảo vệ biên lợi nhuận."
        else:
            # Case A: Market/competitor price is below our current price
            if market_price < current_price:
                if market_price < floor_price:
                    # Competitor is dumping or selling below our protected floor
                    recommended = floor_price
                    reason = (
                        f"Giá thị trường/đối thủ ({market_price:,}đ) thấp hơn giá sàn an toàn ({floor_price:,}đ). "
                        f"Kích hoạt Profit Guard: Khóa giá ở mức sàn để bảo vệ biên lợi nhuận {profile.min_margin_pct * 100:.1f}%, "
                        f"không phá giá theo đối thủ."
                    )
                else:
                    # We can compete, subject to max step drop
                    target = market_price
                    if target < max_allowed_drop:
                        recommended = max(floor_price, max_allowed_drop.quantize(cls.CURRENCY_UNIT, rounding=ROUND_CEILING))
                        reason = (
                            f"Giá thị trường ({market_price:,}đ) thấp hơn hiện tại. "
                            f"Đề xuất giảm mức tối đa cho phép ({max_step_pct * 100:.0f}%) xuống {recommended:,}đ."
                        )
                    else:
                        recommended = target.quantize(cls.CURRENCY_UNIT, rounding=ROUND_HALF_UP)
                        reason = f"Đề xuất khớp giá cạnh tranh thị trường ({market_price:,}đ) trong ngưỡng an toàn."

            # Case B: Market price is higher than our current price
            elif market_price > current_price:
                target = min(market_price, max_allowed_increase)
                recommended = target.quantize(cls.CURRENCY_UNIT, rounding=ROUND_HALF_UP)
                reason = f"Thị trường tăng giá ({market_price:,}đ). Đề xuất tăng giá bán lên {recommended:,}đ để tối ưu hóa lợi nhuận."

            # Case C: Market price is identical to our current price
            else:
                if current_price < floor_price:
                    recommended = floor_price
                    reason = f"Giá hiện tại dưới giá sàn an toàn. Đề xuất tăng lên mức sàn {floor_price:,}đ."
                else:
                    recommended = current_price
                    reason = "Giá hiện tại đang tối ưu và ngang bằng thị trường."

        # ABSOLUTE GUARANTEE: Recommended price must NEVER be lower than floor price
        if recommended < floor_price:
            recommended = floor_price

        profit_amount, profit_margin_pct = cls.calculate_profit(recommended, profile)

        return {
            "sku_id": profile.sku_id,
            "platform": profile.platform,
            "current_price": current_price,
            "market_price": market_price,
            "breakeven_price": breakeven_price,
            "floor_price": floor_price,
            "recommended_price": recommended,
            "profit_amount": profit_amount,
            "profit_margin_pct": profit_margin_pct,
            "reason": reason,
        }
