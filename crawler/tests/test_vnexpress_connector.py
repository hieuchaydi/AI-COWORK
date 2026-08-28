"""
Tests for VnExpress connector and fixture-based golden tests.
"""
import pytest
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import httpx
from crawler.connectors.vnexpress.source import VnExpressSource
from crawler.extractor import load_rules, extract_record

FIXTURES_DIR = Path(__file__).parent.parent / "connectors" / "vnexpress" / "fixtures"
RULES_PATH = Path(__file__).parent.parent / "connectors" / "vnexpress" / "article.rules.yaml"

def test_vnexpress_extraction_from_html():
    html_file = FIXTURES_DIR / "article_standard.html"
    if not html_file.exists():
        pytest.skip("Fixture file not found")

    html_content = html_file.read_text(encoding="utf-8")
    client = httpx.AsyncClient()
    source = VnExpressSource(client)

    fetched_at = datetime(2026, 8, 28, 14, 0, 0, tzinfo=timezone.utc)
    record = source.extract(html_content, url="https://vnexpress.net/standard-1234567.html", fetched_at=fetched_at)
    assert record is not None
    assert record.get("title") != ""
    assert record.get("body_text") != ""

def test_vnexpress_photo_article():
    html_file = FIXTURES_DIR / "article_photo.html"
    if not html_file.exists():
        pytest.skip("Fixture file not found")

    html_content = html_file.read_text(encoding="utf-8")
    client = httpx.AsyncClient()
    source = VnExpressSource(client)

    fetched_at = datetime(2026, 8, 28, 14, 0, 0, tzinfo=timezone.utc)
    record = source.extract(html_content, url="https://vnexpress.net/photo-1234568.html", fetched_at=fetched_at)
    assert record is not None
    assert record.get("title") != ""
