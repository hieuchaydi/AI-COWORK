"""
Source conformance suite (C13).
Verifies every Source connector satisfies the Crawler Layer contract:
- Declares source_id, access_class, user_agent
- Config validates without errors
- Implements Source protocol (discover + extract methods exist)
- No direct network calls outside Fetcher (checked by method inspection)
"""
import json
import sys
import os
import asyncio
import datetime
from pathlib import Path

# Add workspace to path
WORKSPACE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(WORKSPACE))

from crawler.connectors.vnexpress.source import VnExpressSource
from crawler.connectors.shopee_api.source import ShopeeApiSource, TokenStore
from crawler.config import load_source_config

import httpx
import pytest


# ─── VnExpress conformance ────────────────────────────────────────────────────

def test_vnexpress_source_config_validates():
    """C13: config validates without errors."""
    config_path = WORKSPACE / "crawler" / "connectors" / "vnexpress" / "config.yaml"
    source = load_source_config(str(config_path))
    assert source.source_id == "vnexpress"
    assert source.access_class.value in ("public", "feed", "api", "restricted")
    assert source.user_agent  # must be non-empty
    assert source.politeness.min_interval_ms >= 1000  # §4.3 minimum 1000ms


def test_vnexpress_source_interface():
    """C13: VnExpressSource implements the connector interface."""
    client = httpx.AsyncClient()
    connector = VnExpressSource(client)
    # Must have discover and extract methods
    assert callable(getattr(connector, "discover_rss", None)), "Missing discover_rss"
    assert callable(getattr(connector, "discover_sitemap", None)), "Missing discover_sitemap"
    assert callable(getattr(connector, "extract", None)), "Missing extract"


def test_vnexpress_extract_is_pure():
    """C13: extract() is a pure function (no network, no randomness)."""
    client = httpx.AsyncClient()
    connector = VnExpressSource(client)
    html = "<html><head><meta property='og:title' content='Test Title'/></head><body></body></html>"
    result1 = connector.extract(html, url="https://vnexpress.net/test-1234567.html")
    result2 = connector.extract(html, url="https://vnexpress.net/test-1234567.html")
    # Pure function: same input → same output
    assert result1 == result2


def test_vnexpress_no_raw_http_in_extract():
    """C13: extract() must not make any HTTP calls (pure function requirement)."""
    import unittest.mock as mock
    client = mock.MagicMock()
    connector = VnExpressSource(client)
    html = "<html><head><meta property='og:title' content='Test'/></head><body></body></html>"
    # If extract() calls client.get(), it would raise; it must not
    connector.extract(html, url="https://vnexpress.net/test-9999999.html")
    client.get.assert_not_called()


# ─── Shopee API conformance ───────────────────────────────────────────────────

def test_shopee_source_declares_api_access_class():
    """C13: Shopee connector must declare access_class = 'api'."""
    config_path = WORKSPACE / "crawler" / "connectors" / "shopee_api" / "config.yaml"
    source = load_source_config(str(config_path))
    assert source.access_class.value == "api"
    assert source.source_id == "shopee_api"


def test_shopee_source_has_authorization_ref():
    """C13: restricted/api access class must have authorization_ref."""
    config_path = WORKSPACE / "crawler" / "connectors" / "shopee_api" / "config.yaml"
    source = load_source_config(str(config_path))
    # Per §4.2: api class requires authorization_ref
    assert source.authorization_ref, "Shopee source must have authorization_ref"


def test_shopee_source_interface():
    """C13: ShopeeApiSource implements the connector interface."""
    client = httpx.AsyncClient()
    connector = ShopeeApiSource(
        partner_id=123456, partner_key="test-key", client=client
    )
    assert callable(getattr(connector, "sign_request", None))
    assert callable(getattr(connector, "get_access_token", None))
    assert callable(getattr(connector, "sync_products", None))


def test_shopee_sign_request_is_deterministic():
    """C13: Signing must be deterministic for the same inputs."""
    client = httpx.AsyncClient()
    connector = ShopeeApiSource(partner_id=12345, partner_key="secret", client=client)
    sig1 = connector.sign_request("/api/v2/product/get_item_list", 1700000000, "token123", 99)
    sig2 = connector.sign_request("/api/v2/product/get_item_list", 1700000000, "token123", 99)
    assert sig1 == sig2


# ─── Golden fixture tests (C15) ──────────────────────────────────────────────

FIXTURE_DIR = WORKSPACE / "crawler" / "connectors" / "vnexpress" / "fixtures"
GOLDEN_FIELDS = ["title", "lead", "published_at"]


def _load_golden_fixture(name: str):
    html = (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8")
    golden = json.loads((FIXTURE_DIR / f"{name}.golden.json").read_text(encoding="utf-8"))
    return html, golden


@pytest.mark.parametrize("fixture_name", [
    "article_standard",
    "article_photo",
    "article_liveblog",
    "article_video",
    "article_paywalled",
])
def test_vnexpress_golden_fixture(fixture_name):
    """C15: Extractor output must match checked-in golden JSON."""
    html, golden = _load_golden_fixture(fixture_name)
    client = httpx.AsyncClient()
    connector = VnExpressSource(client)
    result = connector.extract(html, url=f"https://vnexpress.net/{fixture_name}-1234567.html")

    for field in GOLDEN_FIELDS:
        if golden.get(field) is None:
            # Golden says this field is null → result should also be None/absent
            assert not result.get(field), (
                f"Fixture {fixture_name}: field '{field}' should be null/absent, got {result.get(field)!r}"
            )
        else:
            assert result.get(field) == golden[field], (
                f"Fixture {fixture_name}: field '{field}' mismatch.\n"
                f"  Expected: {golden[field]!r}\n"
                f"  Got:      {result.get(field)!r}"
            )


# ─── Fixture endpoint tests (T9) ─────────────────────────────────────────────

def test_fixture_server_all_endpoints():
    """T9: Verify fixture server responds correctly on all defined endpoints."""
    sys.path.insert(0, str(WORKSPACE / "browser-control-plane" / "conformance" / "fixtures"))
    from server import FixtureServer
    server = FixtureServer().start()
    base = server.base_url

    import urllib.request
    try:
        endpoints = ["/", "/5k-node", "/json-api", "/spa", "/shadow-dom",
                     "/iframe", "/dialog", "/form", "/cookies", "/js-error"]
        for path in endpoints:
            req = urllib.request.urlopen(base + path, timeout=5)
            assert req.status == 200, f"Endpoint {path} returned {req.status}"
    finally:
        server.stop()


def test_fixture_server_redirect_chain():
    """T9: Redirect chain returns 302 → 302 → 302 → 200."""
    sys.path.insert(0, str(WORKSPACE / "browser-control-plane" / "conformance" / "fixtures"))
    from server import FixtureServer
    server = FixtureServer().start()
    base = server.base_url

    import urllib.request
    try:
        # Python urllib follows redirects by default, so we just check the final page
        req = urllib.request.urlopen(base + "/redirect-3x", timeout=5)
        body = req.read().decode()
        assert "Redirect chain complete" in body
    finally:
        server.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
