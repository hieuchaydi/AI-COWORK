"""Dynamic Tool Router — categorizes tools, consolidates operations, and selects optimal subsets per turn.

Specially engineered for tool-constrained and quota-sensitive models (Groq, Llama, Free-tier APIs):
1. Keeps CORE tools (file ops, todos, clock, ask_user, web search/fetch) always available.
2. Consolidates granular operations into high-level cohesive bundle tools (crawl_and_export_bundle).
3. Strictly limits active tools per turn for Groq models (default cap = 16) to prevent 400s & 429 TPM exhaustion.
4. Enforces Shopee policy: strictly suppresses browser_* tools and prioritizes WebSocket bridge / Job Queue.
5. Tracks endpoint idempotency to prevent duplicate calls and rate-limit loops.
6. Manages workflow and job state by jobId across turns.
7. Computes exponential backoff with jitter for retries and polling.
8. Prunes acknowledged large payloads to prevent context window explosion.
9. Coordinates sequential workflow slicing without state loss for models limited to single tool execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from enum import Enum
from typing import Any, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry


class ToolCategory(str, Enum):
    CORE = "core"
    BROWSER = "browser"
    CRAWL = "crawl"
    DEV_SHELL = "dev_shell"
    CONNECTORS = "connectors"
    AUTOMATION = "automation"
    CUSTOM = "custom"


# Core built-in tools that should always survive trimming and category pruning
_CORE_BUILTINS = frozenset({
    "read_file",
    "write_file",
    "list_files",
    "grep_files",
    "todo_write",
    "todo_read",
    "current_time",
    "ask_user",
    "web_search",
    "web_fetch",
    "load_skill",
    "remember",
    "propose_plan",
    "request_directory",
})

# Patterns for categorizing tools by their function names
_BROWSER_TOOL_PREFIXES = ("browser_",)
_CRAWL_TOOL_NAMES = {
    "crawl_urls",
    "parse_sitemap",
    "extract_html",
    "extract_table",
    "save_page_snapshot",
    "save_csv",
    "save_artifact",
    "download_file",
    "zip_folder",
    "download_media_and_zip",
    "download_media_from_csv",
    "crawl_and_export_bundle",
    "commerce_monitor_track",
    "commerce_monitor_check",
    "commerce_monitor_list",
    "commerce_monitor_history",
    "commerce_monitor_check_all",
    "configure_profit_guard",
    "get_pricing_recommendation",
    "list_pending_price_approvals",
    "approve_price_change",
    "reject_price_change",
    "get_pricing_audit_history",
    "shopee_crawl_job",
    "queue_ingest_job",
    "shopee_job_status",
}
_DEV_SHELL_TOOL_NAMES = {
    "run_shell",
    "explorer_subagent",
    "git_status",
    "git_diff",
    "git_log",
    "git_commit",
    "git_checkout",
    "git_branch",
    "git_add",
    "code_search",
}
_CONNECTORS_PREFIXES = (
    "mcp__",
    "gmail_",
    "drive_",
    "gcal_",
    "slack_",
    "discord_",
    "telegram_",
    "notion_",
    "hubspot_",
    "jira_",
    "github_",
    "linear_",
    "asana_",
    "trello_",
    "monday_",
    "clickup_",
    "salesforce_",
)
_CONNECTORS_TOOL_NAMES = {
    "send_message",
    "send_file",
    "subscribe",
    "unsubscribe",
    "list_subscriptions",
}
_AUTOMATION_TOOL_NAMES = {
    "schedule_task",
    "list_scheduled_tasks",
    "cancel_scheduled_task",
    "selfwake_sleep",
    "selfwake_check",
}
_AUTOMATION_PREFIXES = ("schedule_", "selfwake_")

# Granular/atomic tools that should be subsumed by compound bundles
_ATOMIC_CRAWL_SUBORDINATES = frozenset({
    "parse_sitemap",
    "extract_html",
    "extract_table",
    "save_page_snapshot",
    "save_artifact",
    "zip_folder",
    "download_file",
})

# Groq model identifiers for automatic detection
_GROQ_MODEL_IDENTIFIERS = (
    "groq:",
    "groq/",
    "api.groq.com",
    "llama-3.3-70b-versatile",
    "llama-3.1-70b",
    "llama-3.1-8b",
    "gpt-oss-120b",
    "gpt-oss-20b",
    "mixtral-8x7b",
)

_DEFAULT_GROQ_TOOL_CAP = 16


def is_groq_model(model: Optional[str]) -> bool:
    """Check if the target model is hosted on Groq or constrained by Groq tool limits."""
    if os.environ.get("COWORKER_FORCE_GROQ_ROUTER", "0").strip().lower() in ("1", "true", "yes"):
        return True
    if not model or not isinstance(model, str):
        return False
    m = model.lower()
    return any(ident in m for ident in _GROQ_MODEL_IDENTIFIERS)


def categorize_tool(tool_name: str, metadata: Any = None) -> ToolCategory:
    """Classify a tool into a ToolCategory based on its name and metadata."""
    if metadata is not None:
        meta_cat = getattr(metadata, "category", None)
        if meta_cat:
            meta_cat_str = str(meta_cat).lower()
            if "custom" in meta_cat_str:
                return ToolCategory.CUSTOM
            if "browser" in meta_cat_str:
                return ToolCategory.BROWSER
            if "crawl" in meta_cat_str or "scrape" in meta_cat_str:
                return ToolCategory.CRAWL
            if "shell" in meta_cat_str or "dev" in meta_cat_str or "code" in meta_cat_str:
                return ToolCategory.DEV_SHELL
            if (
                "connector" in meta_cat_str
                or "messaging" in meta_cat_str
                or "integration" in meta_cat_str
            ):
                return ToolCategory.CONNECTORS
            if "automation" in meta_cat_str or "schedule" in meta_cat_str or "wake" in meta_cat_str:
                return ToolCategory.AUTOMATION

    name = tool_name.lower()
    if name.startswith(_BROWSER_TOOL_PREFIXES):
        return ToolCategory.BROWSER
    if name in _CRAWL_TOOL_NAMES:
        return ToolCategory.CRAWL
    if name in _DEV_SHELL_TOOL_NAMES or name.startswith("git_") or name.startswith("code_"):
        return ToolCategory.DEV_SHELL
    if name in _CONNECTORS_TOOL_NAMES or name.startswith(_CONNECTORS_PREFIXES):
        return ToolCategory.CONNECTORS
    if name in _AUTOMATION_TOOL_NAMES or name.startswith(_AUTOMATION_PREFIXES):
        return ToolCategory.AUTOMATION

    return ToolCategory.CORE


_BROWSER_KEYWORDS = {
    "browser",
    "playwright",
    "chromium",
    "trình duyệt",
    "mở web",
    "mở trang",
    "vào trang",
    "vào web",
    "đăng nhập",
    "chụp màn hình",
    "chụp web",
    "screenshot",
    "click",
    "bấm nút",
    "điền form",
    "nhập form",
    "chọn nút",
    "gõ phím",
    "tương tác web",
    "giao diện web",
    "login",
    "sign in",
    "signin",
    "signup",
    "sign up",
    "css selector",
    "xpath",
    "cookie",
}

_CRAWL_KEYWORDS = {
    "crawl",
    "cào web",
    "cào",
    "scrape",
    "scraper",
    "scraping",
    "sitemap",
    "sitemap.xml",
    "bóc tách",
    "tải nhiều trang",
    "bò web",
    "lấy danh sách link",
    "lấy bảng",
    "lưu csv",
    "trích xuất",
    "tải hàng loạt",
    "download_file",
    "save_csv",
    "extract_html",
    "extract_table",
    "thu thập dữ liệu",
    "zip",
    "nén zip",
    "nén file",
    "nén ảnh",
    "nén video",
    "tải ảnh",
    "tải video",
    "theo dõi giá",
    "giá sản phẩm",
    "price monitor",
    "commerce monitor",
    "cào ảnh",
    "cào video",
    "zip_folder",
    "download_media_and_zip",
    "profit_guard",
    "định giá",
    "giá bán",
    "lợi nhuận",
    "biên lợi nhuận",
    "giá sàn",
    "giá hòa vốn",
    "pricing",
    "margin",
    "phê duyệt giá",
    "bảo vệ lợi nhuận",
}

_DEV_SHELL_KEYWORDS = {
    "run_shell",
    "shell",
    "terminal",
    "bash",
    "powershell",
    "cmd",
    "git",
    "commit",
    "branch",
    "push",
    "pull",
    "chạy lệnh",
    "dòng lệnh",
    "cài đặt",
    "chạy test",
    "chạy file",
    "build",
    "compile",
    "pytest",
    "npm",
    "npx",
    "pip",
    "pnpm",
    "yarn",
    "python",
    "node",
    "cargo",
    "docker",
    "make",
    "code_search",
    "thực thi",
}

_CONNECTORS_KEYWORDS = {
    "telegram",
    "gmail",
    "email",
    "mail",
    "hòm thư",
    "hộp thư",
    "thư đến",
    "drive",
    "gdrive",
    "google drive",
    "slack",
    "discord",
    "notion",
    "jira",
    "github",
    "hubspot",
    "linear",
    "asana",
    "trello",
    "monday",
    "clickup",
    "salesforce",
    "gửi tin",
    "nhắn tin",
    "send_message",
    "send_file",
    "gửi file",
    "kênh",
    "channel",
    "bot",
    "subscribe",
    "unsubscribe",
    "inbox",
}

_AUTOMATION_KEYWORDS = {
    "schedule",
    "scheduled",
    "selfwake",
    "timer",
    "cron",
    "fire_at",
    "hẹn giờ",
    "lên lịch",
    "đặt lịch",
    "tự động chạy",
    "chạy định kỳ",
    "nhắc nhở",
    "phút nữa",
    "tiếng nữa",
    "giờ nữa",
    "ngày mai",
    "tối nay",
    "sáng mai",
    "hàng ngày",
    "mỗi ngày",
}

_SHOPEE_KEYWORDS = {
    "shopee",
    "shopee.vn",
    "đánh giá shopee",
    "review shopee",
    "cào shopee",
    "crawl shopee",
}


def _matches_any_keyword(text: str, keywords: set[str]) -> bool:
    for kw in keywords:
        if kw in text:
            return True
    return False


# ─── 1. Compound / Consolidated Tools ─────────────────────────────────────────

def consolidate_tools_for_turn(
    tools: set[str],
    is_groq: bool = False,
    shopee_intent: bool = False,
) -> set[str]:
    """Consolidate related granular operations into cohesive bundle tools.

    - Exclude all browser_* tools when interacting with Shopee (CDP bot-wall prevention).
    - Drop redundant atomic crawl operations if high-level compound bundles are available.
    """
    result = set(tools)

    # Shopee Policy: Strict exclusion of browser_* tools
    if shopee_intent:
        result = {t for t in result if not t.startswith("browser_")}

    # Granular consolidation for Groq or tool-constrained environments
    if is_groq:
        has_compound_crawl = bool({"crawl_and_export_bundle", "download_media_from_csv"} & result)
        if has_compound_crawl:
            result = {t for t in result if t not in _ATOMIC_CRAWL_SUBORDINATES}

    return result


# ─── 2. Endpoint Idempotency Tracker ──────────────────────────────────────────

class EndpointIdempotencyTracker:
    """Tracks endpoint calls to prevent redundant duplicate invocations and rate-limit loops."""

    def __init__(self) -> None:
        self._history: dict[str, dict[str, Any]] = {}
        self._last_poll: dict[str, float] = {}

    def _make_key(self, endpoint: str, params: Any = None) -> str:
        clean_ep = str(endpoint).strip().lower()
        param_str = json.dumps(params, sort_keys=True) if params else ""
        h = hashlib.sha256(param_str.encode("utf-8")).hexdigest()[:16]
        return f"{clean_ep}::{h}"

    def record_call(self, endpoint: str, params: Any = None, result: Any = None) -> None:
        key = self._make_key(endpoint, params)
        now = time.time()
        if key in self._history:
            self._history[key]["count"] += 1
            self._history[key]["timestamp"] = now
            if result is not None:
                self._history[key]["last_result"] = result
        else:
            self._history[key] = {
                "endpoint": endpoint,
                "timestamp": now,
                "count": 1,
                "last_result": result,
            }
        self._last_poll[endpoint.split("?")[0].lower()] = now

    def is_duplicate(self, endpoint: str, params: Any = None, window_seconds: float = 30.0) -> bool:
        """Check if this exact endpoint+params was called recently."""
        key = self._make_key(endpoint, params)
        record = self._history.get(key)
        if not record:
            return False
        return (time.time() - record["timestamp"]) < window_seconds

    def can_poll(self, endpoint: str, min_interval_seconds: float = 2.0) -> tuple[bool, float]:
        """Check if polling endpoint can be called, or return remaining wait seconds."""
        ep_clean = endpoint.split("?")[0].lower()
        last = self._last_poll.get(ep_clean, 0.0)
        now = time.time()
        elapsed = now - last
        if elapsed < min_interval_seconds:
            return False, round(min_interval_seconds - elapsed, 2)
        return True, 0.0

    def get_last_result(self, endpoint: str, params: Any = None) -> Any:
        key = self._make_key(endpoint, params)
        rec = self._history.get(key)
        return rec.get("last_result") if rec else None

    def clear(self) -> None:
        self._history.clear()
        self._last_poll.clear()


# ─── 3. Job State Manager by jobId ────────────────────────────────────────────

class JobState(dict):
    """Convenient dict subclass with attribute access for job state fields."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            return None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class JobStateManager:
    """Manages multi-turn workflow state keyed by jobId."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}

    def register_job(
        self,
        job_id: str,
        url: str = "",
        kind: str = "shopee-reviews",
        phase: str = "queued",
        metadata: Optional[dict[str, Any]] = None,
    ) -> JobState:
        now = time.time()
        state = JobState({
            "job_id": job_id,
            "url": url,
            "kind": kind,
            "phase": phase,
            "step": 1,
            "status": "queued",
            "ack_received": False,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "last_poll_time": 0.0,
            "result": None,
            "csv_path": None,
            "zip_path": None,
            "report_path": None,
            "error": None,
            "metadata": metadata or {},
        })
        self._jobs[job_id] = state
        return state

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[JobState]:
        job = self._jobs.get(job_id)
        if not job:
            return None
        for k, v in kwargs.items():
            job[k] = v
        job["updated_at"] = time.time()
        return job

    def get_job(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)

    def mark_ack(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            job["ack_received"] = True
            job["updated_at"] = time.time()

    def list_jobs(self) -> list[JobState]:
        return list(self._jobs.values())

    def clear(self) -> None:
        self._jobs.clear()


# ─── 4. Exponential Backoff & Retries ─────────────────────────────────────────

def calculate_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff delay with optional random jitter."""
    exp = max(0, attempt)
    delay = min(max_delay, base_delay * (backoff_factor ** exp))
    if jitter:
        jitter_amount = random.uniform(0.0, min(1.0, delay * 0.15))
        delay += jitter_amount
    return round(delay, 2)


class BackoffRetryPolicy:
    """Manages retry progression with exponential backoff."""

    def __init__(
        self,
        max_retries: int = 5,
        base_delay: float = 1.5,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    def next_delay(self, attempt: int) -> float:
        return calculate_backoff(
            attempt=attempt,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            backoff_factor=self.backoff_factor,
            jitter=True,
        )

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_retries


# ─── 5. Payload ACK Manager & Pruning ──────────────────────────────────────────

class PayloadAckManager:
    """Tracks large payloads that have already received an ACK to prevent resending."""

    def __init__(self) -> None:
        self._acks: set[str] = set()
        self._summaries: dict[str, str] = {}

    def acknowledge(self, job_id: str, summary: str = "") -> None:
        if job_id:
            self._acks.add(job_id)
            if summary:
                self._summaries[job_id] = summary

    def is_acknowledged(self, job_id: str) -> bool:
        return job_id in self._acks

    def get_summary(self, job_id: str) -> str:
        return self._summaries.get(job_id, "Payload acknowledged")

    def clear(self) -> None:
        self._acks.clear()
        self._summaries.clear()


def prune_acknowledged_payloads(
    messages: list[dict[str, Any]],
    ack_manager: Optional[PayloadAckManager] = None,
    max_chars: int = 1200,
) -> list[dict[str, Any]]:
    """Prune large payload bodies that have already been acknowledged to prevent context explosion."""
    if not messages:
        return messages

    pruned: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")

        if isinstance(content, str) and len(content) > max_chars:
            matched_job = None
            job_match = re.search(r"\b(job-[A-Za-z0-9_-]+)\b", content)
            if job_match:
                matched_job = job_match.group(1)

            is_acked = False
            if ack_manager and matched_job and ack_manager.is_acknowledged(matched_job):
                is_acked = True
            elif ('"ok": true' in content or '"ok":true' in content or '"status": "ok"' in content) and matched_job:
                is_acked = True

            if is_acked:
                summary = ack_manager.get_summary(matched_job) if (ack_manager and matched_job) else "Payload processed & acknowledged"
                replacement = (
                    f"[ACKNOWLEDGED_PAYLOAD: jobId={matched_job or 'N/A'}, "
                    f"summary='{summary}', raw_size={len(content)} chars. Content safely stored on disk]"
                )
                msg_copy = dict(msg)
                msg_copy["content"] = replacement
                pruned.append(msg_copy)
                continue

        pruned.append(msg)

    return pruned


# ─── 6. Sequential Workflow Slicing ───────────────────────────────────────────

class WorkflowPhase(str, Enum):
    DISCOVERY = "discovery"
    QUEUE_JOB = "queue_job"
    POLL_RESULT = "poll_result"
    EXPORT_BUNDLE = "export_bundle"
    COMPLETED = "completed"


_PHASE_ALLOWED_TOOLS: dict[WorkflowPhase, set[str]] = {
    WorkflowPhase.DISCOVERY: {
        "read_file", "write_file", "web_search", "current_time", "ask_user"
    },
    WorkflowPhase.QUEUE_JOB: {
        "web_fetch", "read_file", "current_time"
    },
    WorkflowPhase.POLL_RESULT: {
        "web_fetch", "current_time"
    },
    WorkflowPhase.EXPORT_BUNDLE: {
        "crawl_and_export_bundle", "download_media_from_csv", "save_csv",
        "download_media_and_zip", "read_file", "write_file", "current_time"
    },
    WorkflowPhase.COMPLETED: {
        "read_file", "write_file", "send_message", "current_time"
    },
}


class SequentialWorkflowCoordinator:
    """Coordinates multi-step workflows sequentially without state loss."""

    def __init__(self, job_state_manager: Optional[JobStateManager] = None) -> None:
        self.job_manager = job_state_manager or JobStateManager()
        self._session_phase: dict[str, WorkflowPhase] = {}
        self._session_job: dict[str, str] = {}

    def get_phase(self, session_id: str) -> WorkflowPhase:
        return self._session_phase.get(session_id, WorkflowPhase.DISCOVERY)

    def set_phase(self, session_id: str, phase: WorkflowPhase, job_id: str = "") -> None:
        self._session_phase[session_id] = phase
        if job_id:
            self._session_job[session_id] = job_id
            job = self.job_manager.get_job(job_id)
            if job:
                job["phase"] = phase.value

    def get_tools_for_phase(self, phase: WorkflowPhase, all_tools: set[str]) -> set[str]:
        allowed = _PHASE_ALLOWED_TOOLS.get(phase, set())
        return {t for t in all_tools if t in allowed}

    def advance_phase(self, session_id: str, current_result: dict[str, Any]) -> WorkflowPhase:
        """Advance phase based on execution result without losing state."""
        current = self.get_phase(session_id)
        job_id = self._session_job.get(session_id, "")

        if current == WorkflowPhase.DISCOVERY:
            if current_result.get("url") or current_result.get("itemid"):
                self.set_phase(session_id, WorkflowPhase.QUEUE_JOB, job_id)
                return WorkflowPhase.QUEUE_JOB

        elif current == WorkflowPhase.QUEUE_JOB:
            new_job_id = (
                current_result.get("job", {}).get("id")
                or current_result.get("jobId")
                or current_result.get("id")
            )
            if new_job_id:
                self.job_manager.register_job(new_job_id, url=current_result.get("url", ""))
                self.set_phase(session_id, WorkflowPhase.POLL_RESULT, new_job_id)
                return WorkflowPhase.POLL_RESULT

        elif current == WorkflowPhase.POLL_RESULT:
            if current_result.get("ok") and current_result.get("result"):
                self.job_manager.mark_ack(job_id)
                self.job_manager.update_job(job_id, status="completed", result=current_result.get("result"))
                self.set_phase(session_id, WorkflowPhase.EXPORT_BUNDLE, job_id)
                return WorkflowPhase.EXPORT_BUNDLE

        elif current == WorkflowPhase.EXPORT_BUNDLE:
            if current_result.get("csv") or current_result.get("zip"):
                self.set_phase(session_id, WorkflowPhase.COMPLETED, job_id)
                return WorkflowPhase.COMPLETED

        return current


# ─── 7. Context & Dynamic Tool Routing ────────────────────────────────────────

def get_active_categories(
    messages: list[dict[str, Any]],
    total_registered_tools: int = 0,
    agent_family: Optional[str] = None,
) -> set[ToolCategory]:
    """Determine the active tool categories for the current context/turn."""
    if os.environ.get("COWORKER_DYNAMIC_TOOL_ROUTING", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return set(ToolCategory)

    if 0 < total_registered_tools <= 10:
        return set(ToolCategory)

    active: set[ToolCategory] = {ToolCategory.CORE, ToolCategory.CUSTOM}

    if agent_family == "code":
        active.add(ToolCategory.DEV_SHELL)

    # Multi-turn continuity
    for msg in reversed(messages[-8:] if len(messages) > 8 else messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = None
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name") or tc.get("name")
                elif hasattr(tc, "name"):
                    name = getattr(tc, "name")
                if name:
                    cat = categorize_tool(name)
                    if cat != ToolCategory.CORE:
                        active.add(cat)

    recent_texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") in ("user", "steering"):
            content = msg.get("content", "")
            if isinstance(content, str):
                recent_texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        recent_texts.append(part.get("text", ""))
        if len(recent_texts) >= 3:
            break

    combined_text = " ".join(recent_texts).lower()

    if _matches_any_keyword(combined_text, _BROWSER_KEYWORDS):
        active.add(ToolCategory.BROWSER)

    if _matches_any_keyword(combined_text, _CRAWL_KEYWORDS):
        active.add(ToolCategory.CRAWL)

    if _matches_any_keyword(combined_text, _DEV_SHELL_KEYWORDS):
        active.add(ToolCategory.DEV_SHELL)

    if _matches_any_keyword(combined_text, _CONNECTORS_KEYWORDS) or "platform:" in combined_text:
        active.add(ToolCategory.CONNECTORS)

    if _matches_any_keyword(combined_text, _AUTOMATION_KEYWORDS) or re.search(
        r"\b\d+\s*(phút|giờ|tiếng|ngày|min|hour|day)", combined_text
    ):
        active.add(ToolCategory.AUTOMATION)

    if "http://" in combined_text or "https://" in combined_text or "www." in combined_text:
        active.add(ToolCategory.BROWSER)
        active.add(ToolCategory.CRAWL)

    if active == {ToolCategory.CORE}:
        active.add(ToolCategory.DEV_SHELL)

    return active


def route_tools_for_context(
    registry: "ToolRegistry",
    messages: list[dict[str, Any]],
    agent_family: Optional[str] = None,
    model: Optional[str] = None,
    max_tools: Optional[int] = None,
) -> set[str]:
    """Select the set of tool names to expose to the LLM for this turn.

    Enhanced for Groq / tool-constrained models:
    - Detects Groq model and enforces strict tool budget.
    - Detects Shopee intent: suppresses browser_* tools, prioritizes WebSocket/job queue & export.
    - Consolidates atomic crawl tools into high-level bundles.
    - Preserves core tools and multi-turn continuity.
    """
    all_names = registry.names()
    is_groq = is_groq_model(model)

    # Detect Shopee intent from recent messages
    recent_texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") in ("user", "steering"):
            content = msg.get("content", "")
            if isinstance(content, str):
                recent_texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        recent_texts.append(part.get("text", ""))
        if len(recent_texts) >= 4:
            break
    combined_text = " ".join(recent_texts).lower()
    shopee_intent = _matches_any_keyword(combined_text, _SHOPEE_KEYWORDS)

    active_categories = get_active_categories(
        messages=messages,
        total_registered_tools=len(all_names),
        agent_family=agent_family,
    )

    if active_categories == set(ToolCategory) and not is_groq and not shopee_intent and max_tools is None:
        return set(all_names)

    selected: set[str] = set()
    for name in all_names:
        spec = registry.get(name)
        metadata = spec.metadata if spec else None
        cat = categorize_tool(name, metadata=metadata)
        if cat in active_categories or (shopee_intent and cat == ToolCategory.CRAWL):
            selected.add(name)

    # Consolidate related operations & apply Shopee policy
    selected = consolidate_tools_for_turn(selected, is_groq=is_groq, shopee_intent=shopee_intent)

    # Determine effective cap
    effective_cap = max_tools
    if effective_cap is None and is_groq:
        env_cap = os.environ.get("COWORKER_GROQ_TOOL_CAP") or os.environ.get("COWORKER_MAX_TOOLS_PER_TURN")
        if env_cap:
            try:
                effective_cap = int(env_cap)
            except ValueError:
                effective_cap = _DEFAULT_GROQ_TOOL_CAP
        else:
            effective_cap = _DEFAULT_GROQ_TOOL_CAP

    if effective_cap is not None and len(selected) > effective_cap:
        def _score_tool(t_name: str) -> float:
            score = 10.0
            if t_name in _CORE_BUILTINS:
                score += 100.0
            if shopee_intent:
                if t_name in ("crawl_and_export_bundle", "download_media_from_csv", "save_csv"):
                    score += 80.0
                if "shopee" in t_name or "ingest" in t_name or "job" in t_name:
                    score += 90.0
            if "crawl_and_export" in t_name or "download_media" in t_name:
                score += 70.0
            if "web_search" in t_name or "web_fetch" in t_name:
                score += 50.0
            if t_name in ("read_file", "write_file"):
                score += 40.0
            return score

        sorted_tools = sorted(selected, key=_score_tool, reverse=True)
        selected = set(sorted_tools[:effective_cap])

    return selected
