"""Unit tests for Tiki Commerce Connector using mocked HTTP transport."""

import json
import pytest
import httpx

from crawler.commerce.connectors.tiki import TikiCommerceConnector
from crawler.commerce.interfaces import (
    CaptchaChallengeException,
    ProductNotFoundException,
    RateLimitException,
)


@pytest.mark.asyncio
async def test_tiki_resolve_product():
    connector = TikiCommerceConnector()

    # 1. Full product url
    id1 = await connector.resolve_product("https://tiki.vn/dien-thoai-samsung-galaxy-s24-p274819231.html?spid=12345")
    assert id1.platform == "tiki"
    assert id1.product_id == "274819231"
    assert id1.sku_id == "12345"
    assert id1.canonical_url == "https://tiki.vn/product-p274819231.html?spid=12345"

    # 2. Short url
    id2 = await connector.resolve_product("https://tiki.vn/p998877.html")
    assert id2.product_id == "998877"
    assert id2.sku_id is None


@pytest.mark.asyncio
async def test_tiki_fetch_product_success():
    mock_data = {
        "id": 274819231,
        "name": "Samsung Galaxy S24 Ultra",
        "price": 28990000,
        "original_price": 33990000,
        "discount_rate": 15,
        "rating_average": 4.9,
        "review_count": 350,
        "inventory_status": "available",
        "stock_item": {"qty": 25},
        "current_seller": {"name": "Samsung Official Store"},
        "images": [{"base_url": "https://tiki.vn/image1.jpg"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_data)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TikiCommerceConnector(client=client)

    identity = await connector.resolve_product("https://tiki.vn/product-p274819231.html")
    snapshot = await connector.fetch_product(identity)

    assert snapshot.product_id == "274819231"
    assert snapshot.title == "Samsung Galaxy S24 Ultra"
    assert snapshot.current_price == 28990000.0
    assert snapshot.original_price == 33990000.0
    assert snapshot.in_stock is True
    assert snapshot.stock_quantity == 25
    assert snapshot.rating_score == 4.9
    assert snapshot.review_count == 350
    assert snapshot.seller_name == "Samsung Official Store"


@pytest.mark.asyncio
async def test_tiki_fetch_product_errors():
    # 404 Not Found
    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client_404 = httpx.AsyncClient(transport=httpx.MockTransport(handler_404))
    connector_404 = TikiCommerceConnector(client=client_404)
    identity = await connector_404.resolve_product("https://tiki.vn/product-p1.html")

    with pytest.raises(ProductNotFoundException):
        await connector_404.fetch_product(identity)

    # 429 Rate Limit
    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, text="Too Many Requests")

    client_429 = httpx.AsyncClient(transport=httpx.MockTransport(handler_429))
    connector_429 = TikiCommerceConnector(client=client_429)

    with pytest.raises(RateLimitException) as exc_info:
        await connector_429.fetch_product(identity)
    assert exc_info.value.retry_after == 30.0

    # Challenge detection
    def handler_challenge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<html><body>Please complete security captcha challenge</body></html>")

    client_challenge = httpx.AsyncClient(transport=httpx.MockTransport(handler_challenge))
    connector_challenge = TikiCommerceConnector(client=client_challenge)

    with pytest.raises(CaptchaChallengeException):
        await connector_challenge.fetch_product(identity)
