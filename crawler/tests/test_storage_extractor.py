"""
Unit tests for Crawler Storage, Frontier, Extractor, and Sink.
"""
import pytest
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from crawler.models import (
    Source, Task, FetchResult, Record, Policy, AccessClass, TaskState,
    DiscoveryConfig, FetchConfig, IdentityConfig, SinkConfig
)
from crawler.storage import CrawlerStorage
from crawler.frontier import Frontier
from crawler.retry import decide_retry
from crawler.records import ArticleRecord, validate_record
from crawler.sink import Sink
from crawler.extractor import (
    ExtractionRules, FieldRule, extract_record, parse_vn_datetime, extract_meta
)
from bs4 import BeautifulSoup

def test_storage_and_frontier(tmp_path: Path):
    async def _test():
        db_file = tmp_path / "crawler.db"
        storage = CrawlerStorage(db_file)
        await storage.init_db()

        frontier = Frontier(storage)

        t1 = Task(
            source_id="vnexpress",
            url="https://vnexpress.net/test-123.html",
            dedup_key="id:123",
            priority=10
        )
        t2 = Task(
            source_id="vnexpress",
            url="https://vnexpress.net/test-456.html",
            dedup_key="id:456",
            priority=5
        )

        # Enqueue tasks
        added1 = await frontier.enqueue(t1)
        added2 = await frontier.enqueue(t2)
        assert added1 is True
        assert added2 is True

        # Duplicate enqueue should be ignored or return False
        dup = await frontier.enqueue(t1)
        assert dup is False

        # Pending count
        p_count = await frontier.pending_count("vnexpress")
        assert p_count == 2

        # Dequeue (priority order)
        dequeued = await frontier.dequeue("vnexpress", count=1)
        assert len(dequeued) == 1
        assert dequeued[0].dedup_key == "id:123"

        # Complete task
        await frontier.complete(dequeued[0].task_id)
        p_count = await frontier.pending_count("vnexpress")
        assert p_count == 1

        # Fail task with retry
        d = decide_retry(t2, status=500, error_kind=None)
        await frontier.fail(t2.task_id, "HTTP_500", d)

    asyncio.run(_test())

def test_extractor_and_sink(tmp_path: Path):
    async def _test():
        db_file = tmp_path / "crawler.db"
        storage = CrawlerStorage(db_file)
        await storage.init_db()
        sink = Sink(storage)

        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Tin tức mới nhất trong ngày</title>
            <meta property="og:title" content="Tiêu đề bài báo từ OG">
            <meta property="article:published_time" content="2026-08-28T14:00:00+07:00">
        </head>
        <body>
            <h1 class="title-detail">Tiêu đề bài báo từ H1</h1>
            <p class="description">Đoạn mô tả tóm tắt nội dung bài viết.</p>
            <article class="fck_detail">
                <p>Nội dung chi tiết của bài báo với độ dài hợp lý để vượt qua validation của crawler engine. Đoạn văn này có nhiều hơn 50 ký tự để đảm bảo hợp lệ.</p>
            </article>
        </body>
        </html>
        """

        rules = ExtractionRules(
            record_type="Article",
            version=1,
            fields={
                "title": [
                    FieldRule(source="meta", selector="og:title"),
                    FieldRule(source="css", selector="h1.title-detail")
                ],
                "lead": [
                    FieldRule(source="css", selector="p.description")
                ],
                "body_html": [
                    FieldRule(source="css", selector="article.fck_detail", mode="html")
                ],
                "body_text": [
                    FieldRule(source="css", selector="article.fck_detail", mode="text")
                ]
            },
            required_fields={"title", "body_text"}
        )

        now = datetime.now(timezone.utc)
        data, warnings = extract_record(sample_html, rules, fetched_at=now)
        assert data["title"] == "Tiêu đề bài báo từ OG"
        assert "Nội dung chi tiết" in data["body_text"]

        data["article_id"] = 123456
        data["canonical_url"] = "https://vnexpress.net/test-123456.html"
        data["source_id"] = "vnexpress"
        data["fetched_at"] = now

        record_obj, errors = validate_record(data, "Article")
        assert errors == []
        assert isinstance(record_obj, ArticleRecord)

        source = Source(
            source_id="vnexpress",
            access_class=AccessClass.PUBLIC,
            user_agent="TestBot/1.0",
            sink=SinkConfig(table="articles")
        )

        r = Record(
            source_id="vnexpress",
            dedup_key="id:123456",
            record_type="Article",
            data=record_obj.model_dump()
        )

        sink_res = await sink.write([r], source)
        assert sink_res.inserted == 1
        assert sink_res.quarantined == 0

        # Write again with same content -> unchanged
        sink_res2 = await sink.write([r], source)
        assert sink_res2.unchanged == 1

    asyncio.run(_test())
