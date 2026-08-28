"""
Unit and integration tests for CrawlRunner.
Updated to match the refactored CrawlRunner.__init__ and compliance API.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional

from crawler.runner import CrawlRunner, CrawlStepResult
from crawler.compliance import ComplianceDecision, ComplianceResult


@dataclass
class MockSource:
    source_id: str
    user_agent: str = "TestBot/1.0"
    # CrawlRunner reads source.extract to decide whether to run extractor
    extract: Optional[dict] = None
    politeness: Any = None

    def __post_init__(self):
        if self.politeness is None:
            from crawler.models import Policy
            self.politeness = Policy()


@dataclass
class MockTask:
    task_id: str
    url: str
    dedup_key: str = "key"
    source_id: str = "src1"
    attempt: int = 0
    run_id: str = ""
    params: Optional[dict] = None
    kind: Any = None

    def __post_init__(self):
        if self.kind is None:
            from crawler.models import TaskKind
            self.kind = TaskKind.FETCH


class MockFrontier:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.completed: List[str] = []
        self.failed: List[tuple] = []

    async def dequeue(self, source_id: str, count: int):
        res = self.tasks[:count]
        self.tasks = self.tasks[count:]
        return res

    async def complete(self, task_id: str):
        self.completed.append(task_id)

    async def fail(self, task_id: str, error_kind: str, decision: str):
        self.failed.append((task_id, error_kind, decision))


class MockComplianceAllow:
    async def authorize(self, task, source):
        return ComplianceDecision(result=ComplianceResult.ALLOW)

    def record_response(self, host: str, status: int, retry_after=None):
        pass


class MockComplianceDeny:
    async def authorize(self, task, source):
        return ComplianceDecision(result=ComplianceResult.DENY, reason="robots.txt")

    def record_response(self, host: str, status: int, retry_after=None):
        pass


class MockFetchResult:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.status = status_code
        self.headers = {}
        self.body = b""
        self.final_url = ""
        self.from_cache = False
        self.content_type = "text/html"
        self.timings = {}
        self.task_id = ""


class MockFetcherOK:
    async def fetch(self, task, source):
        r = MockFetchResult(200)
        r.task_id = task.task_id
        return r


class MockFetcherError:
    async def fetch(self, task, source):
        r = MockFetchResult(500)
        r.task_id = task.task_id
        return r


class MockStorage:
    async def save_raw_response(self, **kwargs):
        return 1

    async def quarantine_record(self, **kwargs):
        pass


class MockSink:
    def __init__(self):
        self.written = []

    async def write(self, records, source, task_id):
        self.written.append((records, task_id))
        from crawler.sink import SinkResult
        return SinkResult(inserted=len(records))


def test_runner_success():
    async def _test():
        source = MockSource("src1")  # no extract → SUCCESS_NO_EXTRACTOR
        tasks = [MockTask("t1", "http://ok.com")]
        frontier = MockFrontier(tasks)
        sink = MockSink()

        runner = CrawlRunner(
            storage=MockStorage(),
            frontier=frontier,
            compliance=MockComplianceAllow(),
            fetcher=MockFetcherOK(),
            sink=sink,
        )

        await runner.run(source, max_tasks=10)

        assert len(frontier.completed) == 1
        assert frontier.completed[0] == "t1"

    asyncio.run(_test())


def test_runner_deny():
    async def _test():
        source = MockSource("src1")
        tasks = [MockTask("t2", "http://deny.com")]
        frontier = MockFrontier(tasks)

        runner = CrawlRunner(
            storage=MockStorage(),
            frontier=frontier,
            compliance=MockComplianceDeny(),
            fetcher=MockFetcherOK(),
            sink=MockSink(),
        )

        await runner.run(source, max_tasks=10)

        assert len(frontier.failed) == 1
        assert frontier.failed[0][1] == "COMPLIANCE_DENY"
        assert len(frontier.completed) == 0

    asyncio.run(_test())


def test_runner_http_error():
    async def _test():
        source = MockSource("src1")
        tasks = [MockTask("t3", "http://error.com")]
        frontier = MockFrontier(tasks)

        runner = CrawlRunner(
            storage=MockStorage(),
            frontier=frontier,
            compliance=MockComplianceAllow(),
            fetcher=MockFetcherError(),
            sink=MockSink(),
        )

        await runner.run(source, max_tasks=10)

        assert len(frontier.failed) == 1
        assert frontier.failed[0][1] == "HTTP_ERROR"

    asyncio.run(_test())
