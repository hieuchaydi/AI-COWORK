"""
Crawler Runner Layer.
Orchestrates the crawling loop: dequeueing from frontier, checking compliance,
fetching, extracting, sinking, and updating frontier state.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict

from .extractor import load_rules, extract_record
from .records import validate_record
from .models import Source, Task, Record, TaskState
from .storage import CrawlerStorage
from .frontier import Frontier
from .compliance import ComplianceEngine, ComplianceResult
from .fetcher import Fetcher
from .sink import Sink

logger = logging.getLogger(__name__)


@dataclass
class CrawlStepResult:
    task_id: Optional[str]
    status: str
    error: Optional[str] = None
    records_written: int = 0


class CrawlRunner:
    """
    Orchestrates the full crawl cycle for a single source:
      dequeue → authorize → fetch → extract → sink → complete/fail
    """

    def __init__(
        self,
        storage: CrawlerStorage,
        frontier: Frontier,
        compliance: ComplianceEngine,
        fetcher: Fetcher,
        sink: Sink,
    ):
        self.storage = storage
        self.frontier = frontier
        self.compliance = compliance
        self.fetcher = fetcher
        self.sink = sink

    async def step(self, source: Source) -> CrawlStepResult:
        tasks = await self.frontier.dequeue(source.source_id, count=1)
        if not tasks:
            return CrawlStepResult(task_id=None, status="EMPTY_FRONTIER")

        task = tasks[0]
        try:
            # ── 1. Compliance gate ──────────────────────────────────────────
            decision = await self.compliance.authorize(task, source)
            if decision.result == ComplianceResult.DENY:
                logger.warning("Task %s denied: %s", task.task_id, decision.reason)
                await self.frontier.fail(task.task_id, "COMPLIANCE_DENY", "DROP")
                return CrawlStepResult(task_id=task.task_id, status="DENIED")

            if decision.result == ComplianceResult.DELAY and decision.delay_ms > 0:
                await asyncio.sleep(decision.delay_ms / 1000.0)

            # ── 2. Fetch ────────────────────────────────────────────────────
            fetch_result = await self.fetcher.fetch(task, source)

            # Record response in compliance engine (circuit breaker / rate limiter)
            from urllib.parse import urlparse
            host = urlparse(task.url or "").netloc
            self.compliance.record_response(host, fetch_result.status)

            if fetch_result.status == 304:
                # Unchanged — update last_seen_at only (handled by sink via same hash)
                await self.frontier.complete(task.task_id)
                return CrawlStepResult(task_id=task.task_id, status="NOT_MODIFIED")

            if fetch_result.status not in range(200, 300):
                await self.frontier.fail(task.task_id, "HTTP_ERROR", "RETRY")
                return CrawlStepResult(
                    task_id=task.task_id,
                    status="HTTP_ERROR",
                    error=f"HTTP {fetch_result.status}",
                )

            # ── 3. Store raw response ───────────────────────────────────────
            from .identity import compute_content_hash
            body_bytes = fetch_result.body if isinstance(fetch_result.body, bytes) else (fetch_result.body or b"")
            content_hash = compute_content_hash("", body_bytes.decode("utf-8", errors="replace"))
            run_id = getattr(task, "run_id", "") or ""
            fetched_at = datetime.now(timezone.utc).isoformat()

            await self.storage.save_raw_response(
                task_id=task.task_id,
                fetched_at=fetched_at,
                status=fetch_result.status,
                headers=dict(fetch_result.headers) if fetch_result.headers else {},
                body=body_bytes,
                content_hash=content_hash,
            )

            # ── 4. Extract ──────────────────────────────────────────────────
            records: List[Record] = []
            if source.extract and source.extract.get("rules_file"):
                try:
                    rules = load_rules(source.extract["rules_file"])
                    html = body_bytes.decode("utf-8", errors="replace")
                    raw_dict = extract_record(html, rules, fetched_at=fetched_at)
                    # Wrap in a Record envelope
                    from .identity import compute_dedup_key, extract_id
                    dedup_key = str(extract_id(task.url or "", source.identity)) if source.identity else compute_dedup_key(task.url or "")
                    record = Record(
                        record_type=rules.record,
                        dedup_key=dedup_key,
                        content_hash=compute_content_hash(
                            (raw_dict.get("body_text") or "") + (raw_dict.get("title") or "")
                        ),
                        extractor_version=rules.version,
                        run_id=run_id,
                        data=raw_dict,
                    )
                    records.append(record)
                except Exception as exc:
                    logger.error("Extraction failed for task %s: %s", task.task_id, exc)
                    # Quarantine and continue (extraction failure ≠ retry)
                    await self.storage.quarantine_record(
                        task_id=task.task_id,
                        reason=f"Extraction error: {exc}",
                        validation_errors=[str(exc)],
                    )
                    await self.frontier.complete(task.task_id)
                    return CrawlStepResult(task_id=task.task_id, status="EXTRACTION_ERROR", error=str(exc))
            else:
                # No extractor config — store raw only, complete task
                await self.frontier.complete(task.task_id)
                return CrawlStepResult(task_id=task.task_id, status="SUCCESS_NO_EXTRACTOR")

            # ── 5. Sink ─────────────────────────────────────────────────────
            if records:
                sink_result = await self.sink.write(records, source, task.task_id)
                await self.frontier.complete(task.task_id)
                return CrawlStepResult(
                    task_id=task.task_id,
                    status="SUCCESS",
                    records_written=sink_result.inserted + sink_result.updated,
                )

            await self.frontier.complete(task.task_id)
            return CrawlStepResult(task_id=task.task_id, status="SUCCESS")

        except Exception as exc:
            logger.error("Error processing task %s: %s", task.task_id, exc, exc_info=True)
            await self.frontier.fail(task.task_id, "INTERNAL_ERROR", "RETRY")
            return CrawlStepResult(task_id=task.task_id, status="ERROR", error=str(exc))

    async def run(self, source: Source, max_tasks: int = 100) -> int:
        """Run up to max_tasks steps. Returns number of tasks processed."""
        tasks_processed = 0
        while tasks_processed < max_tasks:
            result = await self.step(source)
            if result.status == "EMPTY_FRONTIER":
                break
            tasks_processed += 1
        return tasks_processed
