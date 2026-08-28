"""
Crawler Observability — metrics and data-quality alarms (§9).

Provides counters and rate tracking for:
- Per-source per-field extraction success rate (alert on > 10pp drop)
- Records/hour vs same hour last week (alert on > 50% drop)
- HTTP status histogram (alert on 403, 429 rate > 1%)
- Fetch rung distribution (alert on browser share rising)
- Freshness lag: now - max(published_at) per source
- p95 fetch/extract duration, frontier depth, DLQ depth, quarantine rate

In production, forward these to Prometheus/DataDog/OpenTelemetry.
Here we keep it in-process with a simple thread-safe dict.
"""

import time
import logging
import threading
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RollingWindow:
    """Count events in a rolling time window."""

    def __init__(self, window_seconds: int = 3600):
        self._window = window_seconds
        self._events: deque = deque()  # [(timestamp, value)]
        self._lock = threading.Lock()

    def record(self, value: float = 1.0):
        now = time.time()
        with self._lock:
            self._events.append((now, value))
            # Prune old events
            cutoff = now - self._window
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def sum(self) -> float:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            return sum(v for ts, v in self._events if ts >= cutoff)

    def count(self) -> int:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            return sum(1 for ts, v in self._events if ts >= cutoff)

    def rate(self) -> float:
        """Events per second in the window."""
        c = self.count()
        return c / self._window if c > 0 else 0.0


class DurationTracker:
    """Track p50/p95/p99 durations with a sliding sample."""

    def __init__(self, max_samples: int = 1000):
        self._samples: deque = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record(self, duration_ms: float):
        with self._lock:
            self._samples.append(duration_ms)

    def percentile(self, p: float) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            sorted_samples = sorted(self._samples)
            idx = int(len(sorted_samples) * p / 100)
            return sorted_samples[min(idx, len(sorted_samples) - 1)]


class SourceMetrics:
    """Per-source metric collection."""

    def __init__(self, source_id: str):
        self.source_id = source_id

        # Fetch counters
        self.fetch_total = RollingWindow(3600)
        self.fetch_errors = RollingWindow(3600)
        self.status_histogram: Dict[int, int] = defaultdict(int)
        self.rung_histogram: Dict[int, int] = defaultdict(int)

        # Extraction per field
        self.field_success: Dict[str, RollingWindow] = {}
        self.field_attempts: Dict[str, RollingWindow] = {}

        # Record throughput
        self.records_written = RollingWindow(3600)
        self.records_quarantined = RollingWindow(3600)

        # Durations
        self.fetch_duration = DurationTracker()
        self.extract_duration = DurationTracker()

        # Freshness
        self.last_published_at: Optional[float] = None

        # DLQ
        self.dead_letter_count: int = 0

    def record_fetch(self, status: int, rung: int, duration_ms: float):
        self.fetch_total.record()
        self.status_histogram[status] += 1
        self.rung_histogram[rung] += 1
        self.fetch_duration.record(duration_ms)
        if status >= 400:
            self.fetch_errors.record()

    def record_field(self, field: str, success: bool):
        if field not in self.field_success:
            self.field_success[field] = RollingWindow(3600)
            self.field_attempts[field] = RollingWindow(3600)
        self.field_attempts[field].record()
        if success:
            self.field_success[field].record()

    def record_record(self, quarantined: bool = False):
        if quarantined:
            self.records_quarantined.record()
        else:
            self.records_written.record()

    def record_extract_duration(self, duration_ms: float):
        self.extract_duration.record(duration_ms)

    def update_freshness(self, published_at_ts: float):
        """Track the most recent published_at seen."""
        if self.last_published_at is None or published_at_ts > self.last_published_at:
            self.last_published_at = published_at_ts

    def freshness_lag_seconds(self) -> Optional[float]:
        if self.last_published_at is None:
            return None
        return time.time() - self.last_published_at

    def field_success_rate(self, field: str) -> Optional[float]:
        attempts = self.field_attempts.get(field)
        success = self.field_success.get(field)
        if not attempts or attempts.count() == 0:
            return None
        return success.count() / attempts.count()

    def http_403_count(self) -> int:
        return self.status_histogram.get(403, 0)

    def http_429_rate(self) -> float:
        total = self.fetch_total.count()
        if total == 0:
            return 0.0
        return self.status_histogram.get(429, 0) / total

    def browser_rung_share(self) -> float:
        total = sum(self.rung_histogram.values())
        if total == 0:
            return 0.0
        return self.rung_histogram.get(4, 0) / total


class CrawlerMetrics:
    """Global metrics registry. One per process."""

    _instance: Optional["CrawlerMetrics"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._sources: Dict[str, SourceMetrics] = {}
        self._source_lock = threading.Lock()
        # Alarm thresholds (§9)
        self.field_drop_alert_pp = 10.0  # 10 percentage points
        self.records_drop_alert_pct = 50.0
        self.browser_rung_alert_share = 0.20  # 20%

    @classmethod
    def get(cls) -> "CrawlerMetrics":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def source(self, source_id: str) -> SourceMetrics:
        with self._source_lock:
            if source_id not in self._sources:
                self._sources[source_id] = SourceMetrics(source_id)
            return self._sources[source_id]

    def check_alarms(self, source_id: str):
        """
        Run alarm checks for a source. Log warnings that should become pages/alerts.
        In production, emit to alerting system (PagerDuty, Slack, etc.).
        """
        m = self.source(source_id)

        # 403 alarm
        if m.http_403_count() > 0:
            logger.warning(
                "ALARM[%s]: received HTTP 403. Check credentials and robots.txt. "
                "Do NOT retry with a different identity.",
                source_id,
            )

        # 429 rate alarm
        rate_429 = m.http_429_rate()
        if rate_429 > 0.01:
            logger.warning(
                "ALARM[%s]: 429 rate %.1f%% exceeds 1%% threshold. "
                "Rate limiter may be misconfigured.",
                source_id, rate_429 * 100,
            )

        # Browser rung share alarm
        browser_share = m.browser_rung_share()
        if browser_share > self.browser_rung_alert_share:
            logger.warning(
                "ALARM[%s]: browser-rung share %.1f%% above threshold. "
                "Silent cost explosion risk.",
                source_id, browser_share * 100,
            )

        # Freshness alarm
        lag = m.freshness_lag_seconds()
        if lag is not None and lag > 3600:
            logger.warning(
                "ALARM[%s]: freshness lag %.0fs (>1h). Site may have changed or crawler stalled.",
                source_id, lag,
            )

        # DLQ growth alarm
        if m.dead_letter_count > 100:
            logger.warning(
                "ALARM[%s]: DLQ depth %d is high. Investigate dead-letter tasks.",
                source_id, m.dead_letter_count,
            )

    def snapshot(self) -> Dict[str, Any]:
        """Return a dict of all current metrics for external reporting."""
        result = {}
        for source_id, m in self._sources.items():
            result[source_id] = {
                "fetch_total_1h": m.fetch_total.count(),
                "fetch_error_rate": (
                    m.fetch_errors.count() / m.fetch_total.count()
                    if m.fetch_total.count() > 0 else 0.0
                ),
                "records_written_1h": m.records_written.count(),
                "records_quarantined_1h": m.records_quarantined.count(),
                "fetch_p95_ms": m.fetch_duration.percentile(95),
                "extract_p95_ms": m.extract_duration.percentile(95),
                "freshness_lag_s": m.freshness_lag_seconds(),
                "dead_letter_count": m.dead_letter_count,
                "http_429_rate": m.http_429_rate(),
                "http_403_count": m.http_403_count(),
                "browser_rung_share": m.browser_rung_share(),
                "status_histogram": dict(m.status_histogram),
                "rung_histogram": dict(m.rung_histogram),
            }
        return result
