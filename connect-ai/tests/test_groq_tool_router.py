"""Comprehensive Tests for Groq Tool Router Architecture.

Audits and verifies:
1. Tool consolidation: grouping related operations into cohesive bundles.
2. Active tool capping per turn: strict limit for Groq models.
3. Shopee policy: strictly suppresses browser_* tools, prioritizes WebSocket/job queue & export.
4. Endpoint idempotency: prevents redundant duplicate invocations.
5. Job state management: maintains state transitions keyed by jobId.
6. Exponential backoff retry with jitter.
7. Payload ACK tracking: prunes large payloads that received ACK to prevent context explosion.
8. Sequential workflow slicing: steps through multi-phase tasks without state loss.
"""

import time
import pytest
from coworker.tools import (
    ToolCategory,
    ToolRegistry,
    categorize_tool,
    route_tools_for_context,
    is_groq_model,
    consolidate_tools_for_turn,
    EndpointIdempotencyTracker,
    JobState,
    JobStateManager,
    calculate_backoff,
    BackoffRetryPolicy,
    PayloadAckManager,
    prune_acknowledged_payloads,
    SequentialWorkflowCoordinator,
    WorkflowPhase,
)


def _make_registry(tool_names: list[str]) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        def fn(): pass
        fn.__name__ = name
        reg.register(fn, schema={"type": "function", "function": {"name": name}})
    return reg


# ─── 1. Groq Model Detection ──────────────────────────────────────────────────

def test_is_groq_model_detection():
    assert is_groq_model("groq:openai/gpt-oss-120b")
    assert is_groq_model("groq:openai/gpt-oss-20b")
    assert is_groq_model("groq:llama-3.3-70b-versatile")
    assert is_groq_model("https://api.groq.com/openai/v1")
    assert is_groq_model("llama-3.3-70b-versatile")
    assert not is_groq_model("gemini-2.0-flash")
    assert not is_groq_model("claude-3-5-sonnet")
    assert not is_groq_model("gpt-4o")
    assert not is_groq_model(None)


# ─── 2. Tool Consolidation ───────────────────────────────────────────────────

def test_consolidate_tools_for_turn_granular_subsumption():
    all_crawl_tools = {
        "crawl_urls",
        "parse_sitemap",
        "extract_html",
        "extract_table",
        "save_page_snapshot",
        "save_csv",
        "download_file",
        "zip_folder",
        "download_media_and_zip",
        "crawl_and_export_bundle",
        "read_file",
    }
    # When is_groq is True and crawl_and_export_bundle exists, drop atomic subordinates
    consolidated = consolidate_tools_for_turn(all_crawl_tools, is_groq=True, shopee_intent=False)
    assert "crawl_and_export_bundle" in consolidated
    assert "save_csv" in consolidated
    assert "download_media_and_zip" in consolidated
    assert "parse_sitemap" not in consolidated
    assert "extract_html" not in consolidated
    assert "extract_table" not in consolidated
    assert "save_page_snapshot" not in consolidated
    assert "zip_folder" not in consolidated
    assert "download_file" not in consolidated


# ─── 3. Strict Active Tool Capping ────────────────────────────────────────────

def test_groq_strict_active_tool_capping():
    # Register 35 tools across multiple categories
    names = [
        "read_file", "write_file", "list_files", "grep_files", "current_time",
        "ask_user", "web_search", "web_fetch", "todo_write", "todo_read",
        "crawl_urls", "save_csv", "crawl_and_export_bundle", "download_media_and_zip",
        "run_shell", "git_status", "git_diff", "git_commit", "git_log",
        "send_message", "send_file", "gmail_send", "slack_post",
        "schedule_task", "list_scheduled_tasks", "selfwake_sleep",
    ] + [f"extra_tool_{i}" for i in range(10)]
    reg = _make_registry(names)

    # User intent: crawl
    msgs = [{"role": "user", "content": "Crawl các bài viết tin tức và lưu file csv"}]
    routed = route_tools_for_context(
        reg,
        msgs,
        model="groq:openai/gpt-oss-120b",
        max_tools=12,
    )

    # Must be capped strictly at 12 tools
    assert len(routed) <= 12
    # CORE tools survive
    assert "read_file" in routed
    assert "write_file" in routed
    assert "web_search" in routed
    # Crawl tools prioritized
    assert "crawl_and_export_bundle" in routed or "save_csv" in routed
    # Irrelevant tools pruned out
    assert "slack_post" not in routed


# ─── 4. Shopee Policy: Suppress Browser, Prioritize WebSocket/Queue ────────────

def test_shopee_intent_suppresses_browser_tools():
    names = [
        "read_file", "write_file", "web_search", "web_fetch", "current_time",
        "browser_open", "browser_click", "browser_fill", "browser_evaluate", "browser_screenshot",
        "crawl_and_export_bundle", "download_media_from_csv", "save_csv",
        "shopee_crawl_job", "queue_ingest_job",
    ]
    reg = _make_registry(names)

    msgs = [{"role": "user", "content": "Cào 50 đánh giá từ https://shopee.vn/product/123/456 kèm ảnh video"}]
    routed = route_tools_for_context(
        reg,
        msgs,
        model="groq:openai/gpt-oss-120b",
    )

    # Strictly NO browser_* tools allowed under Shopee intent
    browser_tools = [t for t in routed if t.startswith("browser_")]
    assert len(browser_tools) == 0

    # Ingest / Queue / Bundle export tools MUST survive
    assert "crawl_and_export_bundle" in routed
    assert "download_media_from_csv" in routed
    assert "save_csv" in routed
    assert "read_file" in routed


# ─── 5. Endpoint Idempotency Tracker ──────────────────────────────────────────

def test_endpoint_idempotency_tracker():
    tracker = EndpointIdempotencyTracker()

    endpoint = "http://127.0.0.1:8766/ingest/job"
    params = {"url": "https://shopee.vn/product/111/222", "kind": "shopee-reviews"}

    # First call: not duplicate
    assert not tracker.is_duplicate(endpoint, params)
    tracker.record_call(endpoint, params, result={"ok": True, "jobId": "job-101"})

    # Immediate second call with identical params: detected as duplicate
    assert tracker.is_duplicate(endpoint, params, window_seconds=10.0)
    assert tracker.get_last_result(endpoint, params) == {"ok": True, "jobId": "job-101"}

    # Polling endpoint rate limiter
    poll_ep = "http://127.0.0.1:8766/ingest/result?id=job-101"
    can_poll, wait = tracker.can_poll(poll_ep, min_interval_seconds=1.5)
    assert can_poll is True

    tracker.record_call(poll_ep)
    # Immediately check again: must wait
    can_poll, wait = tracker.can_poll(poll_ep, min_interval_seconds=1.5)
    assert can_poll is False
    assert wait > 0.0


# ─── 6. Job State Manager by jobId ────────────────────────────────────────────

def test_job_state_manager_by_job_id():
    manager = JobStateManager()

    job = manager.register_job("job-abc", url="https://shopee.vn/p/1", metadata={"shopid": "111"})
    assert job.job_id == "job-abc"
    assert job.status == "queued"
    assert job.phase == "queued"
    assert job.ack_received is False

    # Update job attributes
    manager.update_job("job-abc", status="running", step=2, attempts=1)
    retrieved = manager.get_job("job-abc")
    assert retrieved.status == "running"
    assert retrieved.step == 2
    assert retrieved.attempts == 1

    # Mark ACK and completion
    manager.mark_ack("job-abc")
    manager.update_job("job-abc", status="completed", csv_path="outputs/csv/shopee_1_reviews.csv")
    assert retrieved.ack_received is True
    assert retrieved.csv_path == "outputs/csv/shopee_1_reviews.csv"


# ─── 7. Exponential Backoff with Jitter ───────────────────────────────────────

def test_exponential_backoff_and_retry_policy():
    policy = BackoffRetryPolicy(max_retries=4, base_delay=1.0, max_delay=16.0, backoff_factor=2.0)

    # Delays must grow exponentially
    d0 = policy.next_delay(0)  # ~1.0 + jitter
    d1 = policy.next_delay(1)  # ~2.0 + jitter
    d2 = policy.next_delay(2)  # ~4.0 + jitter
    d3 = policy.next_delay(3)  # ~8.0 + jitter

    assert 1.0 <= d0 <= 1.3
    assert 2.0 <= d1 <= 2.5
    assert 4.0 <= d2 <= 5.0
    assert 8.0 <= d3 <= 10.0

    assert policy.should_retry(0) is True
    assert policy.should_retry(3) is True
    assert policy.should_retry(4) is False  # Reached max_retries


# ─── 8. Payload ACK Manager & Pruning ─────────────────────────────────────────

def test_payload_ack_manager_and_pruning():
    ack_mgr = PayloadAckManager()
    ack_mgr.acknowledge("job-test-99", summary="50 reviews saved to outputs/csv/shopee_99.csv")

    huge_raw_rows = '{"rows": [' + ','.join([f'{{"sao": 5, "noi_dung": "review số {i} rat tuyet voi"}}' for i in range(100)]) + '], "ok": true}'
    assert len(huge_raw_rows) > 3000

    messages = [
        {"role": "user", "content": "Crawl kết quả cho job-test-99"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "function": {"name": "web_fetch"}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": f"job-test-99 result: {huge_raw_rows}"},
    ]

    pruned = prune_acknowledged_payloads(messages, ack_manager=ack_mgr, max_chars=500)

    # Tool message payload must be pruned
    tool_msg = pruned[2]
    assert "[ACKNOWLEDGED_PAYLOAD: jobId=job-test-99" in tool_msg["content"]
    assert "50 reviews saved" in tool_msg["content"]
    assert len(tool_msg["content"]) < 300  # Dramatically smaller than 3000+ chars


# ─── 9. Sequential Workflow Slicing without State Loss ────────────────────────

def test_sequential_workflow_coordinator_preserves_state():
    job_mgr = JobStateManager()
    coordinator = SequentialWorkflowCoordinator(job_state_manager=job_mgr)
    session_id = "sess-01"

    all_tools = {
        "web_search", "web_fetch", "read_file", "write_file", "current_time",
        "crawl_and_export_bundle", "download_media_from_csv", "save_csv",
        "send_message",
    }

    # Step 1: DISCOVERY
    assert coordinator.get_phase(session_id) == WorkflowPhase.DISCOVERY
    tools_step1 = coordinator.get_tools_for_phase(WorkflowPhase.DISCOVERY, all_tools)
    assert "web_search" in tools_step1
    assert "crawl_and_export_bundle" not in tools_step1

    # Advance to QUEUE_JOB after discovering URL
    p2 = coordinator.advance_phase(session_id, {"url": "https://shopee.vn/product/123/456", "itemid": "456"})
    assert p2 == WorkflowPhase.QUEUE_JOB
    tools_step2 = coordinator.get_tools_for_phase(p2, all_tools)
    assert "web_fetch" in tools_step2
    assert "crawl_and_export_bundle" not in tools_step2

    # Advance to POLL_RESULT after job is queued with jobId
    p3 = coordinator.advance_phase(session_id, {"job": {"id": "job-777"}, "url": "https://shopee.vn/product/123/456"})
    assert p3 == WorkflowPhase.POLL_RESULT
    assert coordinator._session_job[session_id] == "job-777"
    assert job_mgr.get_job("job-777") is not None

    # Advance to EXPORT_BUNDLE after result arrives
    p4 = coordinator.advance_phase(session_id, {"ok": True, "result": {"count": 25, "csv": "outputs/csv/test.csv"}})
    assert p4 == WorkflowPhase.EXPORT_BUNDLE
    tools_step4 = coordinator.get_tools_for_phase(p4, all_tools)
    assert "crawl_and_export_bundle" in tools_step4
    assert "save_csv" in tools_step4

    # Advance to COMPLETED after export
    p5 = coordinator.advance_phase(session_id, {"csv": "outputs/csv/test.csv", "zip": "outputs/zips/test.zip"})
    assert p5 == WorkflowPhase.COMPLETED
    # Verify state was fully preserved in job_mgr
    final_job = job_mgr.get_job("job-777")
    assert final_job.status == "completed"
    assert final_job.ack_received is True
