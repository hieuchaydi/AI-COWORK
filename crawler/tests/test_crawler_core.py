"""
Unit tests for Crawler models, identity, compliance, and retry logic.
"""
import pytest
from datetime import datetime, timezone
from crawler.models import (
    Source, Task, FetchResult, Record, Policy, AccessClass, TaskState, FetchRung, TaskKind,
    DiscoveryConfig, FetchConfig, IdentityConfig, SinkConfig
)
from crawler.identity import (
    canonicalize_url, extract_id, compute_dedup_key, compute_content_hash
)
from crawler.retry import decide_retry, compute_backoff
from crawler.compliance import (
    RobotsCache, RateLimiter, CircuitBreaker, ComplianceEngine, ComplianceResult
)

def test_models_instantiation():
    source = Source(
        source_id="test-source",
        access_class=AccessClass.PUBLIC,
        user_agent="TestBot/1.0 (+https://example.com)",
        politeness=Policy(max_concurrency_per_host=2, min_interval_ms=500),
        discovery=[DiscoveryConfig(kind="rss", urls=["https://example.com/rss"])],
        fetch=FetchConfig(default_rung=FetchRung.HTTP),
        identity=IdentityConfig(id_regex=r"-(\d+)\.html$", dedup_key="match:1"),
        sink=SinkConfig(table="articles")
    )
    assert source.source_id == "test-source"
    assert source.access_class == AccessClass.PUBLIC
    assert source.politeness.max_concurrency_per_host == 2

    task = Task(
        source_id="test-source",
        url="https://example.com/item-123.html",
        dedup_key="id:123"
    )
    assert task.state == TaskState.PENDING
    assert task.attempt == 0
    assert task.dedup_key == "id:123"

def test_identity_canonicalize():
    url = "https://EXAMPLE.com/path/to/article.html?utm_source=fb&gclid=12345#comments"
    canonical = canonicalize_url(url)
    assert canonical == "https://example.com/path/to/article.html"

def test_identity_extract_id_and_dedup_key():
    url = "https://vnexpress.net/bai-viet-moi-nhat-1234567.html"
    extracted = extract_id(url, r"-(\d+)\.html$")
    assert extracted == "1234567"

    cfg = IdentityConfig(id_regex=r"-(\d+)\.html$", dedup_key="match:1")
    dedup = compute_dedup_key(url, cfg)
    assert dedup == "id:1234567"

    content_hash = compute_content_hash("Title", "Body text content")
    assert len(content_hash) == 64

def test_retry_matrix():
    task = Task(source_id="test", dedup_key="k1", attempt=1)
    
    # 500 error -> retryable
    d1 = decide_retry(task, status=500, error_kind=None)
    assert d1.should_retry is True
    
    # 404 error -> not retryable
    d2 = decide_retry(task, status=404, error_kind=None)
    assert d2.should_retry is False

    # 403 error -> not retryable
    d3 = decide_retry(task, status=403, error_kind=None)
    assert d3.should_retry is False

    # NETWORK_ERROR -> retryable
    d4 = decide_retry(task, status=None, error_kind="NETWORK_ERROR")
    assert d4.should_retry is True

    # Backoff calculation
    b = compute_backoff(2, base=1.0)
    assert 4.0 <= b <= 6.0

def test_compliance_engine():
    import asyncio
    async def _test():
        engine = ComplianceEngine()
        source = Source(
            source_id="test",
            access_class=AccessClass.PUBLIC,
            user_agent="TestBot/1.0",
            politeness=Policy(min_interval_ms=100)
        )
        task = Task(source_id="test", url="https://example.com/page", dedup_key="k1")
        
        # Authorize task
        decision = await engine.authorize(task, source)
        assert decision.result in (ComplianceResult.ALLOW, ComplianceResult.DELAY)
    asyncio.run(_test())
