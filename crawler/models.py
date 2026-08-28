"""
Core data models for the Crawler Layer.
"""
from enum import Enum
from typing import Any, Optional
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field, ConfigDict

class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class AccessClass(str, Enum):
    PUBLIC = "public"
    FEED = "feed"
    API = "api"
    RESTRICTED = "restricted"

class FetchRung(int, Enum):
    FEED = 0          # RSS/sitemap/JSON API
    HTTP = 1          # Plain HTTP GET + HTML parse
    HTTP_EMBEDDED = 2 # HTTP + extract embedded JSON (__NEXT_DATA__, JSON-LD)
    HTTP_XHR = 3      # HTTP call to site's own XHR/JSON endpoint
    BROWSER = 4       # BCP Page

class TaskState(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    DEAD_LETTER = "dead_letter"
    GONE = "gone"  # 404/410

class TaskKind(str, Enum):
    DISCOVER = "discover"  # seed/discovery task
    FETCH = "fetch"        # fetch content
    BACKFILL = "backfill"  # low-priority backfill

class Policy(StrictBaseModel):
    max_concurrency_per_host: int = 1
    min_interval_ms: int = 1000
    respect_crawl_delay: bool = True
    max_retries: int = 5
    timeout_seconds: float = 30.0
    user_agent: str = "CrawlerBot/1.0"

class Task(StrictBaseModel):
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str
    kind: TaskKind = TaskKind.FETCH
    url: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    depth: int = 0
    priority: int = 0  # higher = more urgent
    attempt: int = 0
    dedup_key: str  # stable identity
    state: TaskState = TaskState.PENDING
    next_run_at: Optional[datetime] = None
    last_error_kind: Optional[str] = None
    run_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class FetchResult(StrictBaseModel):
    task_id: str
    status: int  # HTTP status or synthetic
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    final_url: str = ""
    from_cache: bool = False
    content_type: str = ""
    timings: dict[str, float] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Record(StrictBaseModel):
    source_id: str
    dedup_key: str
    record_type: str  # e.g. "Article", "ShopProduct"
    data: dict[str, Any]
    content_hash: str = ""
    extractor_version: int = 1
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: Optional[str] = None

class DiscoveryConfig(StrictBaseModel):
    kind: str  # "rss", "sitemap_index", "api_cursor", "listing_page"
    urls: list[str] = []
    url: Optional[str] = None
    interval: str = "15m"  # human-readable interval
    incremental_by: Optional[str] = None  # "lastmod", etc.

class FetchOverride(StrictBaseModel):
    url_pattern: str
    rung: FetchRung = FetchRung.BROWSER
    justification_doc: str = ""

class FetchConfig(StrictBaseModel):
    default_rung: FetchRung = FetchRung.HTTP
    overrides: list[FetchOverride] = []

class IdentityConfig(StrictBaseModel):
    id_regex: str  # regex to extract ID from URL
    dedup_key: str = "match:1"  # which group to use

class SinkConfig(StrictBaseModel):
    table: str = "articles"
    on_conflict: str = "upsert_by_dedup_key"
    version_on_content_change: bool = True

class Source(StrictBaseModel):
    source_id: str
    access_class: AccessClass = AccessClass.PUBLIC
    authorization_ref: Optional[str] = None
    enabled: bool = True
    user_agent: str = "CrawlerBot/1.0 (+https://example.com/bot)"
    politeness: Policy = Field(default_factory=Policy)
    discovery: list[DiscoveryConfig] = []
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    identity: Optional[IdentityConfig] = None
    extract: Optional[dict[str, str]] = None  # {"record": "Article", "rules_file": "..."}
    sink: SinkConfig = Field(default_factory=SinkConfig)
