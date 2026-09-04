"""Unit tests for Commerce DiffEngine and AlertRules."""

from datetime import datetime, timedelta, timezone
import pytest

from crawler.commerce.interfaces import ProductSnapshot
from crawler.commerce.monitoring.alert_rules import (
    LowestPrice30DayRule,
    OutOfStockRule,
    PriceDropPercentRule,
    RatingDropRule,
    RestockRule,
    TargetPriceRule,
)
from crawler.commerce.monitoring.diff_engine import DiffEngine


def make_snapshot(price: float, in_stock: bool = True, rating: float = 4.8, days_ago: int = 0) -> ProductSnapshot:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return ProductSnapshot(
        product_id="test_prod_1",
        platform="tiki",
        title="Laptop Pro 16",
        current_price=price,
        in_stock=in_stock,
        rating_score=rating,
        url="https://tiki.vn/p1.html",
        timestamp=dt,
    )


def test_price_drop_rule():
    rule = PriceDropPercentRule(threshold_pct=0.20)
    prev = make_snapshot(price=10000000.0)

    # Drop 25% -> Trigger
    curr_drop = make_snapshot(price=7500000.0)
    trigger = rule.evaluate(curr_drop, prev, [prev])
    assert trigger is not None
    assert trigger.rule_name == "price_drop_pct"
    assert trigger.severity == "urgent"
    assert "-25.0%" in trigger.message

    # Drop 10% -> No trigger
    curr_small_drop = make_snapshot(price=9000000.0)
    assert rule.evaluate(curr_small_drop, prev, [prev]) is None


def test_lowest_price_30d_rule():
    rule = LowestPrice30DayRule(days=30)
    history = [
        make_snapshot(price=12000000.0, days_ago=10),
        make_snapshot(price=11000000.0, days_ago=5),
        make_snapshot(price=10500000.0, days_ago=1),
    ]
    prev = history[-1]

    # Current price 9,990,000 is lower than all 30d history (min was 10.5m)
    current = make_snapshot(price=9990000.0)
    trigger = rule.evaluate(current, prev, history)
    assert trigger is not None
    assert trigger.rule_name == "lowest_price_30d"
    assert "9,990,000đ" in trigger.message

    # Current price 11,500,000 is not a 30d low
    curr_high = make_snapshot(price=11500000.0)
    assert rule.evaluate(curr_high, prev, history) is None

    # A lower price outside the configured window must not suppress a new low.
    stale_low = make_snapshot(price=5000000.0, days_ago=31)
    assert rule.evaluate(current, prev, history + [stale_low]) is not None


def test_stock_transition_rules():
    restock_rule = RestockRule()
    out_rule = OutOfStockRule()

    out_snap = make_snapshot(price=500000.0, in_stock=False)
    in_snap = make_snapshot(price=500000.0, in_stock=True)

    # Restock event (out -> in)
    tr_restock = restock_rule.evaluate(in_snap, out_snap, [out_snap])
    assert tr_restock is not None
    assert tr_restock.rule_name == "back_in_stock"
    assert "In Stock" in tr_restock.message

    # Out of stock event (in -> out)
    tr_out = out_rule.evaluate(out_snap, in_snap, [in_snap])
    assert tr_out is not None
    assert tr_out.rule_name == "out_of_stock"


def test_rating_drop_rule():
    rule = RatingDropRule(threshold=0.3)
    prev = make_snapshot(price=500000.0, rating=4.8)
    curr = make_snapshot(price=500000.0, rating=4.4)  # dropped by 0.4

    trigger = rule.evaluate(curr, prev, [prev])
    assert trigger is not None
    assert trigger.rule_name == "rating_drop"
    assert "4.8" in trigger.message
    assert "4.4" in trigger.message


def test_target_price_rule():
    rule = TargetPriceRule()
    prev = make_snapshot(price=22000000.0)
    curr = make_snapshot(price=19500000.0)

    # Target 20,000,000 reached
    trigger = rule.evaluate(curr, prev, [prev], target_price=20000000.0)
    assert trigger is not None
    assert trigger.rule_name == "target_price_reached"

    # Already below target previously -> no duplicate trigger
    prev_already_low = make_snapshot(price=19800000.0)
    curr_lower = make_snapshot(price=19500000.0)
    assert rule.evaluate(curr_lower, prev_already_low, [prev_already_low], target_price=20000000.0) is None


def test_diff_engine_summary():
    engine = DiffEngine()
    snap1 = make_snapshot(price=1000000.0, days_ago=2)
    snap2 = make_snapshot(price=900000.0, days_ago=1)
    snap3 = make_snapshot(price=700000.0, days_ago=0)

    summary = engine.calculate_summary(snap3, snap2, [snap1, snap2])
    assert summary["price_delta"] == -200000.0
    assert pytest.approx(summary["price_delta_pct"], 0.01) == -0.222
    assert summary["min_history_price"] == 900000.0
    assert summary["is_30d_low"] is True
