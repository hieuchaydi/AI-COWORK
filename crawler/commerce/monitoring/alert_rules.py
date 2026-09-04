"""
Alert rules definitions for commerce monitoring.
Evaluates price drops, 30-day lows, stock status flips, and rating shifts.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from ..interfaces import ProductSnapshot


@dataclass
class AlertTrigger:
    """Represents a triggered alert event."""

    rule_name: str
    product_id: str
    platform: str
    title: str
    url: str
    previous_value: Optional[float]
    current_value: float
    message: str
    severity: str = "info"  # "info", "warning", "urgent"


class AlertRule(ABC):
    """Abstract rule interface for evaluating product changes."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        pass


class PriceDropPercentRule(AlertRule):
    """Alerts if current price drops by at least threshold_pct compared to previous price."""

    def __init__(self, threshold_pct: float = 0.20):
        self.threshold_pct = threshold_pct

    @property
    def name(self) -> str:
        return "price_drop_pct"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if not previous or previous.current_price <= 0:
            return None

        if current.current_price < previous.current_price:
            drop_ratio = (previous.current_price - current.current_price) / previous.current_price
            if drop_ratio >= self.threshold_pct:
                pct_str = f"{drop_ratio * 100:.1f}%"
                prev_formatted = f"{int(previous.current_price):,}đ"
                curr_formatted = f"{int(current.current_price):,}đ"
                msg = (
                    f"🔥 Giảm giá sốc -{pct_str}!\n"
                    f"Sản phẩm: {current.title}\n"
                    f"Giá cũ: {prev_formatted} ➔ Giá mới: {curr_formatted}"
                )
                return AlertTrigger(
                    rule_name=self.name,
                    product_id=current.product_id,
                    platform=current.platform,
                    title=current.title,
                    url=current.url,
                    previous_value=previous.current_price,
                    current_value=current.current_price,
                    message=msg,
                    severity="urgent",
                )
        return None


class LowestPrice30DayRule(AlertRule):
    """Alerts if current price is strictly lower than any snapshot in the past 30 days."""

    def __init__(self, days: int = 30):
        self.days = days

    @property
    def name(self) -> str:
        return "lowest_price_30d"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if not history:
            return None

        cutoff = current.timestamp - timedelta(days=self.days)
        past_prices = [
            snapshot.current_price
            for snapshot in history
            if cutoff <= snapshot.timestamp < current.timestamp
        ]
        if not past_prices:
            return None

        min_past_price = min(past_prices)
        if current.current_price < min_past_price:
            curr_formatted = f"{int(current.current_price):,}đ"
            min_formatted = f"{int(min_past_price):,}đ"
            msg = (
                f"🏷️ Đáy giá mới trong {self.days} ngày qua!\n"
                f"Sản phẩm: {current.title}\n"
                f"Giá hiện tại: {curr_formatted} (đáy cũ: {min_formatted})"
            )
            return AlertTrigger(
                rule_name=self.name,
                product_id=current.product_id,
                platform=current.platform,
                title=current.title,
                url=current.url,
                previous_value=min_past_price,
                current_value=current.current_price,
                message=msg,
                severity="urgent",
            )
        return None


class RestockRule(AlertRule):
    """Alerts when a previously out-of-stock product is back in stock."""

    @property
    def name(self) -> str:
        return "back_in_stock"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if previous and not previous.in_stock and current.in_stock:
            curr_formatted = f"{int(current.current_price):,}đ"
            msg = (
                f"📦 Hàng đã về lại (In Stock)!\n"
                f"Sản phẩm: {current.title}\n"
                f"Giá: {curr_formatted}"
            )
            return AlertTrigger(
                rule_name=self.name,
                product_id=current.product_id,
                platform=current.platform,
                title=current.title,
                url=current.url,
                previous_value=0.0,
                current_value=1.0,
                message=msg,
                severity="warning",
            )
        return None


class OutOfStockRule(AlertRule):
    """Alerts when a product goes out of stock."""

    @property
    def name(self) -> str:
        return "out_of_stock"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if previous and previous.in_stock and not current.in_stock:
            msg = f"⚠️ Hết hàng (Out of Stock)!\nSản phẩm: {current.title}"
            return AlertTrigger(
                rule_name=self.name,
                product_id=current.product_id,
                platform=current.platform,
                title=current.title,
                url=current.url,
                previous_value=1.0,
                current_value=0.0,
                message=msg,
                severity="info",
            )
        return None


class RatingDropRule(AlertRule):
    """Alerts when product rating drops significantly (e.g. >= 0.3)."""

    def __init__(self, threshold: float = 0.3):
        self.threshold = threshold

    @property
    def name(self) -> str:
        return "rating_drop"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if previous and previous.rating_score is not None and current.rating_score is not None:
            if previous.rating_score - current.rating_score >= self.threshold:
                msg = (
                    f"⭐ Điểm đánh giá giảm mạnh!\n"
                    f"Sản phẩm: {current.title}\n"
                    f"Rating: {previous.rating_score:.1f} ➔ {current.rating_score:.1f} (-{previous.rating_score - current.rating_score:.1f})"
                )
                return AlertTrigger(
                    rule_name=self.name,
                    product_id=current.product_id,
                    platform=current.platform,
                    title=current.title,
                    url=current.url,
                    previous_value=previous.rating_score,
                    current_value=current.rating_score,
                    message=msg,
                    severity="warning",
                )
        return None


class TargetPriceRule(AlertRule):
    """Alerts when price reaches or drops below user-specified target price."""

    @property
    def name(self) -> str:
        return "target_price_reached"

    def evaluate(
        self,
        current: ProductSnapshot,
        previous: Optional[ProductSnapshot],
        history: List[ProductSnapshot],
        target_price: Optional[float] = None,
    ) -> Optional[AlertTrigger]:
        if target_price is not None and target_price > 0:
            if current.current_price <= target_price:
                # Check if previous was already below to avoid spam
                if previous and previous.current_price <= target_price:
                    return None
                curr_formatted = f"{int(current.current_price):,}đ"
                target_formatted = f"{int(target_price):,}đ"
                msg = (
                    f"🎯 Đã đạt giá mục tiêu của bạn!\n"
                    f"Sản phẩm: {current.title}\n"
                    f"Giá hiện tại: {curr_formatted} <= Mục tiêu: {target_formatted}"
                )
                return AlertTrigger(
                    rule_name=self.name,
                    product_id=current.product_id,
                    platform=current.platform,
                    title=current.title,
                    url=current.url,
                    previous_value=previous.current_price if previous else None,
                    current_value=current.current_price,
                    message=msg,
                    severity="urgent",
                )
        return None
