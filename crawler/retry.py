"""
Retry logic and backoff matrix.
"""

import random
from dataclasses import dataclass
from typing import Optional

from .models import Task


@dataclass
class RetryDecision:
    should_retry: bool
    backoff_seconds: float
    max_attempts: int
    reason: str


def compute_backoff(attempt: int, base: float = 1.0, max_delay: float = 60.0) -> float:
    """Exponential backoff with jitter."""
    delay = min(base * (2 ** attempt), max_delay)
    # Add random jitter up to 10%
    jitter = delay * 0.1 * random.uniform(0, 1)
    return delay + jitter


def decide_retry(task: Task, status: Optional[int], error_kind: Optional[str]) -> RetryDecision:
    """
    Apply retry matrix logic based on status or error kind.
    """
    attempt = task.attempt if task else 0

    if error_kind:
        if "TIMEOUT" in error_kind or "TARGET_CRASHED" in error_kind or "TRANSPORT_CLOSED" in error_kind:
            return RetryDecision(
                should_retry=True,
                backoff_seconds=compute_backoff(attempt, base=2.0, max_delay=60.0),
                max_attempts=3,
                reason="Transient timeout/crash"
            )
        if "NETWORK_ERROR" in error_kind or "Connection reset" in error_kind or "DNS" in error_kind:
            return RetryDecision(
                should_retry=True,
                backoff_seconds=compute_backoff(attempt, base=1.0, max_delay=60.0),
                max_attempts=5,
                reason="Transient network error"
            )
        if "ELEMENT_NOT_FOUND" in error_kind:
            return RetryDecision(
                should_retry=False,
                backoff_seconds=0,
                max_attempts=0,
                reason="Element not found (Quarantine)"
            )
        if "VALIDATION" in error_kind.upper():
            return RetryDecision(
                should_retry=False,
                backoff_seconds=0,
                max_attempts=0,
                reason="Validation failure (Quarantine)"
            )

    if status:
        if status in (404, 410):
            return RetryDecision(
                should_retry=False,
                backoff_seconds=0,
                max_attempts=0,
                reason="Resource gone"
            )
        if status in (401, 403):
            return RetryDecision(
                should_retry=False,
                backoff_seconds=0,
                max_attempts=0,
                reason="Unauthorized/Forbidden"
            )
        if status in (429, 503):
            # Header retry-after parsing would happen elsewhere and could override this backoff
            return RetryDecision(
                should_retry=True,
                backoff_seconds=compute_backoff(attempt, base=5.0, max_delay=120.0),
                max_attempts=3,
                reason="Rate limited / Unavailable"
            )
        if status >= 500:
            return RetryDecision(
                should_retry=True,
                backoff_seconds=compute_backoff(attempt, base=2.0, max_delay=120.0),
                max_attempts=4,
                reason="Server error"
            )

    return RetryDecision(
        should_retry=False,
        backoff_seconds=0,
        max_attempts=0,
        reason="No retry needed / Unhandled case"
    )
