"""
Durable task queue (Frontier) backed by storage.
"""

from typing import List, Optional
from datetime import datetime, timezone, timedelta

from .models import Task, TaskState
from .storage import CrawlerStorage
from .retry import RetryDecision


class Frontier:
    """Durable task queue: pending/in-flight/done/dead-letter, per-source partitioned."""

    def __init__(self, storage: CrawlerStorage):
        self._storage = storage

    async def enqueue(self, task: Task) -> bool:
        """Add task if dedup_key not already pending/in-flight. Returns False if duplicate."""
        existing = await self._storage.get_task_by_dedup_key(task.source_id, task.dedup_key)
        if existing:
            state = existing.get("state")
            if state in (TaskState.PENDING.value, TaskState.IN_FLIGHT.value, TaskState.DONE.value):
                return False
        await self._storage.save_task(task)
        return True

    async def dequeue(self, source_id: str, count: int = 1) -> List[Task]:
        """Get highest-priority pending tasks and mark them in-flight."""
        tasks_data = await self._storage.get_pending_tasks(source_id=source_id, limit=count)
        result: List[Task] = []
        for t in tasks_data:
            task = Task(
                task_id=t["task_id"],
                source_id=t["source_id"],
                kind=t["kind"],
                url=t.get("url"),
                depth=t.get("depth", 0),
                priority=t.get("priority", 0),
                attempt=t.get("attempt", 0),
                dedup_key=t["dedup_key"],
                state=TaskState.IN_FLIGHT,
                last_error_kind=t.get("last_error_kind"),
                run_id=t.get("run_id")
            )
            await self._storage.update_task_state(task.task_id, TaskState.IN_FLIGHT)
            result.append(task)
        return result

    async def complete(self, task_id: str):
        """Mark task as done."""
        await self._storage.update_task_state(task_id, TaskState.DONE)

    async def fail(self, task_id: str, error_kind: str, retry_decision: RetryDecision):
        """Handle failure: requeue with backoff or dead-letter."""
        task_data = await self._storage.get_task(task_id)
        if not task_data:
            return

        current_attempt = task_data.get("attempt", 0) + 1

        if retry_decision.should_retry and current_attempt <= retry_decision.max_attempts:
            next_run = datetime.now(timezone.utc) + timedelta(seconds=retry_decision.backoff_seconds)
            await self._storage.update_task_retry(
                task_id=task_id,
                attempt=current_attempt,
                state=TaskState.PENDING,
                next_run_at=next_run,
                last_error_kind=error_kind
            )
        else:
            await self._storage.move_to_dead_letter(task_id, error_kind)

    async def mark_gone(self, task_id: str):
        """Mark as GONE (404/410), stop re-queuing."""
        await self._storage.update_task_state(task_id, TaskState.GONE)

    async def dead_letter_count(self, source_id: Optional[str] = None) -> int:
        return await self._storage.get_task_count_by_state(TaskState.DEAD_LETTER, source_id)

    async def pending_count(self, source_id: Optional[str] = None) -> int:
        return await self._storage.get_task_count_by_state(TaskState.PENDING, source_id)

    async def requeue_dead_letters(self, source_id: str) -> int:
        """Move dead-letter tasks back to pending for manual re-drive."""
        return await self._storage.requeue_dead_letters(source_id)
