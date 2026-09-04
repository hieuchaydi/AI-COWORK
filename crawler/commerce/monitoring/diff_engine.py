"""
Diff Engine for Commerce Snapshots.
Compares consecutive snapshots and historical trends against defined AlertRules.
"""

from typing import Any, Dict, List, Optional

from ..interfaces import ProductSnapshot
from .alert_rules import (
    AlertRule,
    AlertTrigger,
    LowestPrice30DayRule,
    OutOfStockRule,
    PriceDropPercentRule,
    RatingDropRule,
    RestockRule,
    TargetPriceRule,
)


class DiffEngine:
    """Calculates product deltas and evaluates alert triggers."""

    def __init__(self, rules: Optional[List[AlertRule]] = None):
        if rules is not None:
            self.rules = rules
        else:
            self.rules = [
                PriceDropPercentRule(threshold_pct=0.20),
                LowestPrice30DayRule(days=30),
                RestockRule(),
                OutOfStockRule(),
                RatingDropRule(threshold=0.3),
                TargetPriceRule(),
            ]

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> List[AlertTrigger]:
        """Evaluates all rules against current snapshot and returns triggered alerts."""
        triggers: List[AlertTrigger] = []
        for rule in self.rules:
            res = rule.evaluate(current, previous, history, target_price=target_price)
            if res is not None:
                triggers.append(res)
        return triggers

    def calculate_summary(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
    ) -> Dict[str, Any]:
        """Generates a statistical diff summary."""
        price_change = 0.0
        price_change_pct = 0.0
        if previous and previous.current_price > 0:
            price_change = current.current_price - previous.current_price
            price_change_pct = price_change / previous.current_price

        all_prices = [s.current_price for s in history if s.current_price > 0]
        min_price = min(all_prices) if all_prices else current.current_price
        max_price = max(all_prices) if all_prices else current.current_price

        return {
            "product_id": current.product_id,
            "platform": current.platform,
            "title": current.title,
            "current_price": current.current_price,
            "previous_price": previous.current_price if previous else None,
            "price_delta": price_change,
            "price_delta_pct": price_change_pct,
            "in_stock": current.in_stock,
            "rating": current.rating_score,
            "history_count": len(history),
            "min_history_price": min_price,
            "max_history_price": max_price,
            "is_30d_low": current.current_price <= min_price if all_prices else True,
        }
