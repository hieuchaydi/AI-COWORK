"""Unit tests for Commerce Monitoring data models and interfaces."""

from datetime import datetime, timezone
import pytest

from crawler.commerce.interfaces import (
    CaptchaChallengeException,
    ProductIdentity,
    ProductSnapshot,
    RateLimitException,
    ReviewItem,
    ReviewPage,
    SessionExpiredException,
)


def test_product_identity_creation():
    identity = ProductIdentity(
        platform="tiki",
        product_id="274819231",
        sku_id="998877",
        canonical_url="https://tiki.vn/product-p274819231.html?spid=998877",
        title="Samsung Galaxy S24 Ultra",
    )
    assert identity.platform == "tiki"
    assert identity.product_id == "274819231"
    assert identity.sku_id == "998877"
    assert "tiki.vn" in identity.canonical_url


def test_product_snapshot_computed_discount():
    # Test discount from original price
    snap1 = ProductSnapshot(
        product_id="123",
        platform="tiki",
        title="Phone",
        current_price=800000.0,
        original_price=1000000.0,
        url="https://tiki.vn/p123.html",
    )
    assert pytest.approx(snap1.computed_discount_pct, 0.01) == 0.20

    # Test explicit discount_rate
    snap2 = ProductSnapshot(
        product_id="124",
        platform="tiki",
        title="Book",
        current_price=150000.0,
        discount_rate=25.0,
        url="https://tiki.vn/p124.html",
    )
    assert pytest.approx(snap2.computed_discount_pct, 0.01) == 0.25


def test_custom_exceptions():
    cap_exc = CaptchaChallengeException(
        platform="shopee",
        url="https://shopee.vn/product/123",
        message="Turnstile challenge active",
    )
    assert cap_exc.platform == "shopee"
    assert "https://shopee.vn/product/123" in str(cap_exc)

    rl_exc = RateLimitException(platform="tiki", retry_after=45.0)
    assert rl_exc.retry_after == 45.0
    assert "Back off for 45.0s" in str(rl_exc)

    sess_exc = SessionExpiredException(platform="lazada", message="Please login")
    assert sess_exc.platform == "lazada"
