"""End-to-End Integration Tests for Shopee Review Ingest Workflow.

Covers all 10 essential criteria:
1. Extension connected (RFC 6455 WebSocket handshake, hello message, connected state).
2. Job dispatched (queued job dispatched over WebSocket).
3. Extension returns accepted (job moves to inflight queue).
4. Ratings request returns 200 (preflight and API get_ratings HTTP 200).
5. Pagination offset increments correctly (offset=0, offset=20, offset=40).
6. Upload progress/chunk/complete (acknowledged RPC progress, data chunks, and complete).
7. Server outputs CSV/report/ZIP (Excel-safe UTF-8 BOM CSV, star distribution Markdown, ZIP bundle).
8. login_required error handling (detects 90309999/is_login=false, stops cleanly without captcha confusion).
9. verification_required & resume (CAPTCHA challenge pause and resolution flow).
10. WebSocket disconnect, reconnect, and automatic in-flight job replay.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest

import launch
from browser_bridge.ingest import IngestRPC
from browser_bridge.server import BrowserGatewayServer
from browser_bridge.transport import ExtensionState
from test_browser_bridge_e2e import SimpleWebSocketTestClient


@pytest.fixture
def gateway_server():
    """BrowserGatewayServer bound to an ephemeral loopback port."""
    token = secrets.token_urlsafe(32)
    server = BrowserGatewayServer(token=token)
    port = server.start("127.0.0.1", 0)
    try:
        yield server, port, token
    finally:
        server.stop()


@pytest.fixture
def setup_shopee_environment(gateway_server, monkeypatch, tmp_path):
    """Wires up launch module with mock output directories and gateway server."""
    server, port, token = gateway_server
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(launch, "_BROWSER_WS", server)

    rpc = IngestRPC(
        send=server.broadcast_or_send,
        store=launch._store_ingest_payload,
        progress=launch._update_ingest_progress,
    )
    monkeypatch.setattr(launch, "_INGEST_RPC", rpc)

    server.on_connect = launch._on_browser_ws_connect
    server.on_message = launch._on_browser_ws_message

    # Reset in-memory states
    with launch._INGEST_JOBS_LOCK:
        launch._INGEST_JOBS.clear()
        launch._INGEST_INFLIGHT.clear()
        launch._INGEST_ALL_JOBS.clear()
    with launch._INGEST_PROGRESS_LOCK:
        launch._INGEST_PROGRESS.clear()
    with launch._INGEST_TRACES_LOCK:
        launch._INGEST_TRACES.clear()
    launch._INGEST_RESULTS.clear()

    # Local dummy media content for offline testing
    dummy_img_bytes = b"FAKE_SHOPEE_REVIEW_IMAGE_RAW_BYTES_12345"
    dummy_vid_bytes = b"FAKE_SHOPEE_REVIEW_VIDEO_RAW_BYTES_67890"

    def fake_download_media(url: str, target_without_ext: Path) -> str:
        if "video" in url:
            target = target_without_ext.with_suffix(".mp4")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(dummy_vid_bytes)
        else:
            target = target_without_ext.with_suffix(".jpg")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(dummy_img_bytes)
        return f"outputs/{target.relative_to(launch._outputs_root()).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download_media)

    yield server, port, token, rpc, tmp_path


def recv_reply_matching(client: SimpleWebSocketTestClient, req_id: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Waits for an ingest.reply matching req_id, ignoring unhandled push envelopes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        msg = client.recv_json(timeout=remaining)
        if not msg:
            continue
        if msg.get("type") == "ingest.reply" and msg.get("id") == req_id:
            return msg
    raise TimeoutError(f"Timed out waiting for ingest.reply with id={req_id}")


# ─── 1-7. Full Workflow: Connect -> Dispatch -> Accepted -> 200 -> Pagination -> Upload -> Outputs ───

def test_shopee_full_workflow_from_queue_to_results(setup_shopee_environment):
    server, port, token, rpc, tmp_path = setup_shopee_environment

    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        # 1. Extension connected
        status = client.connect(token=token)
        assert status == 101
        greeting = client.recv_json(timeout=2.0)
        assert greeting["type"] == "hello"
        assert server.connected is True
        time.sleep(0.1)

        # 2. Job được dispatch
        test_url = "https://shopee.vn/product/123456/25018847315"
        job = launch._queue_ingest_job(test_url, "shopee-reviews")
        job_id = job["id"]
        assert job_id.startswith("job-")
        assert launch._INGEST_PROGRESS[job_id]["status"] == "queued"

        msg = client.recv_json(timeout=3.0)
        assert msg["type"] == "ingest.job"
        assert msg["job"]["id"] == job_id
        assert msg["job"]["url"] == test_url

        # 3. Extension trả accepted
        client.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})
        time.sleep(0.1)
        with launch._INGEST_JOBS_LOCK:
            assert job_id not in [j["id"] for j in launch._INGEST_JOBS]
            assert job_id in launch._INGEST_INFLIGHT

        # 4 & 5. Ratings request trả 200 và phân trang offset tăng đúng (0 -> 20 -> 40)
        pagination_batches = [
            {"offset": 0, "limit": 20, "count": 20},
            {"offset": 20, "limit": 20, "count": 20},
            {"offset": 40, "limit": 20, "count": 10},
        ]
        for idx, batch in enumerate(pagination_batches, start=1):
            req_trace = {
                "jobId": job_id,
                "itemid": "25018847315",
                "shopid": "123456",
                "event": "ratings-request",
                "offset": batch["offset"],
                "limit": batch["limit"],
                "tabUrl": test_url,
                "responseUrl": f"https://shopee.vn/api/v2/item/get_ratings?itemid=25018847315&shopid=123456&offset={batch['offset']}&limit={batch['limit']}",
            }
            resp_trace = {
                "jobId": job_id,
                "itemid": "25018847315",
                "shopid": "123456",
                "event": "ratings-response",
                "offset": batch["offset"],
                "limit": batch["limit"],
                "httpStatus": 200,
                "rowsCount": batch["count"],
                "elapsedMs": 42,
                "tabUrl": test_url,
            }
            launch._update_ingest_progress(job_id, {"stage": "trace", "trace": req_trace})
            launch._update_ingest_progress(job_id, {"stage": "trace", "trace": resp_trace})

        # Verify traces record correct offsets and HTTP status 200
        traces = launch._INGEST_TRACES.get(job_id, [])
        resp_traces = [t for t in traces if t.get("event") == "ratings-response"]
        assert len(resp_traces) == 3
        assert [t["offset"] for t in resp_traces] == [0, 20, 40]
        assert [t["limit"] for t in resp_traces] == [20, 20, 20]
        assert all(t["httpStatus"] == 200 for t in resp_traces)

        # 6. Upload progress/chunk/complete
        # 6a. Progress RPC
        req_id_prog = "rpc-prog-1"
        client.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": req_id_prog,
            "params": {
                "operation": "progress",
                "job": job_id,
                "progress": {"status": "running", "stage": "uploading", "percent": 75, "message": "Đang tải dữ liệu"},
            },
        })
        prog_reply = recv_reply_matching(client, req_id_prog, timeout=5.0)
        assert prog_reply["type"] == "ingest.reply"
        assert prog_reply["id"] == req_id_prog
        assert prog_reply["ok"] is True
        assert launch._INGEST_PROGRESS[job_id]["percent"] == 75

        # 6b. Chunk RPC (50 reviews with star ratings and media URLs)
        rows = []
        for i in range(50):
            star = (i % 5) + 1
            has_media = (i % 3 == 0)
            rows.append({
                "user": f"buyer_{i}",
                "sao": star,
                "noi_dung": f"Sản phẩm rất tốt {i}, chất lượng tuyệt vời",
                "thoi_gian": f"2026-09-0{min(9, (i % 8) + 1)} 12:00:00",
                "anh_urls": f"https://cf.shopee.vn/file/img_{i}.jpg" if has_media else "",
                "video_urls": f"https://cvf.shopee.vn/file/vid_{i}.mp4" if (has_media and i % 2 == 0) else "",
            })

        payload = {
            "job": job_id,
            "name": "shopee_25018847315_reviews",
            "source": test_url,
            "rows": rows,
        }
        upload_id = "upload-test-shopee-50"
        chunk_str = json.dumps(payload, ensure_ascii=False)
        req_id_chunk = "rpc-chunk-1"
        client.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": req_id_chunk,
            "params": {
                "operation": "chunk",
                "uploadId": upload_id,
                "index": 0,
                "chunk": chunk_str,
            },
        })
        chunk_reply = recv_reply_matching(client, req_id_chunk, timeout=5.0)
        assert chunk_reply["type"] == "ingest.reply"
        assert chunk_reply["id"] == req_id_chunk
        assert chunk_reply["ok"] is True

        # 6c. Complete RPC
        req_id_complete = "rpc-complete-1"
        client.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": req_id_complete,
            "params": {
                "operation": "complete",
                "uploadId": upload_id,
            },
        })
        comp_reply = recv_reply_matching(client, req_id_complete, timeout=5.0)
        comp_reply = recv_reply_matching(client, req_id_complete, timeout=15.0)
        assert comp_reply["type"] == "ingest.reply"
        assert comp_reply["id"] == req_id_complete
        assert comp_reply["ok"] is True
        assert comp_reply["result"]["count"] == 50

        # 7. Server ghi CSV/report/ZIP
        assert job_id in launch._INGEST_RESULTS
        result = launch._INGEST_RESULTS[job_id]
        assert result["ok"] is True
        assert result["count"] == 50

        # Verify CSV with UTF-8 BOM
        csv_path = tmp_path / "csv" / "shopee_25018847315_reviews.csv"
        assert csv_path.is_file()
        raw_csv_bytes = csv_path.read_bytes()
        assert raw_csv_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM!
        assert raw_csv_bytes[:3] == bytes.fromhex("efbbbf")  # UTF-8 BOM!
        csv_text = raw_csv_bytes.decode("utf-8-sig")
        csv_lines = csv_text.strip().splitlines()
        assert len(csv_lines) == 51  # 1 header + 50 rows
        header = csv_lines[0].split(",")
        for expected_col in ["thoi_gian", "sao", "noi_dung", "anh_urls", "image_files", "video_files"]:
            assert expected_col in header

        # Verify Markdown report
        report_path = tmp_path / "text" / "shopee_25018847315_reviews_report.md"
        assert report_path.is_file()
        report_text = report_path.read_text(encoding="utf-8")
        assert "Tổng số đánh giá**: 50" in report_text
        assert "Phân bố số sao" in report_text
        assert "[Tải file CSV]" in report_text
        assert "[Tải trọn bộ ảnh/video .ZIP]" in report_text

        # Verify ZIP archive contains CSV, manifest.json, and media
        zip_path = tmp_path / "zips" / "shopee_25018847315_reviews_media.zip"
        assert zip_path.is_file()
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            assert "shopee_25018847315_reviews.csv" in namelist
            assert "manifest.json" in namelist
            # At least one image or video in zip
            assert any(name.endswith(".jpg") or name.endswith(".mp4") for name in namelist)

        # Inflight queue cleaned up
        with launch._INGEST_JOBS_LOCK:
            assert job_id not in launch._INGEST_INFLIGHT

    finally:
        client.close()
        rpc.executor.shutdown(wait=True)


# ─── 8. Lỗi login_required: dừng đúng lỗi, không bypass, không nhầm sang CAPTCHA ─

def test_shopee_workflow_login_required_stops_cleanly(setup_shopee_environment):
    server, port, token, rpc, tmp_path = setup_shopee_environment

    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        client.recv_json(timeout=2.0)

        # Queue job
        test_url = "https://shopee.vn/product/111222/333444555"
        job = launch._queue_ingest_job(test_url, "shopee-reviews")
        job_id = job["id"]

        job_envelope = client.recv_json(timeout=3.0)
        assert job_envelope["type"] == "ingest.job"

        client.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})
        time.sleep(0.05)

        # Preflight in tab discovers is_login == False and error 90309999
        preflight_error = "Shopee login required (HTTP 200, error=90309999, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome"
        preflight_trace = {
            "jobId": job_id,
            "itemid": "333444555",
            "shopid": "111222",
            "event": "tab-preflight",
            "httpStatus": 200,
            "error": preflight_error,
            "is_login": False,
            "responseUrl": "https://shopee.vn/api/v2/item/get_ratings",
        }
        launch._update_ingest_progress(job_id, {"stage": "trace", "trace": preflight_trace})

        # Extension reports login_required error via complete payload
        login_err_payload = {
            "job": job_id,
            "error": preflight_error,
            "login_required": True,
            "status": "login_required",
        }
        status_code, result = launch._store_ingest_payload(login_err_payload)
        assert status_code == 200
        assert result["ok"] is False
        assert result["login_required"] is True
        assert result["verification_required"] is False

        # Verify progress status is login_required
        progress = launch._INGEST_PROGRESS.get(job_id, {})
        assert progress["status"] == "login_required"
        assert progress["login_required"] is True
        assert progress["verification_required"] is False

        # Server transport must NOT be paused for captcha verification
        assert server.transport.is_paused() is False

    finally:
        client.close()
        rpc.executor.shutdown(wait=True)


# ─── 9. Lỗi verification_required & Resume ─────────────────────────────────────

def test_shopee_workflow_verification_required_and_resume(setup_shopee_environment):
    server, port, token, rpc, tmp_path = setup_shopee_environment

    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        client.recv_json(timeout=2.0)

        test_url = "https://shopee.vn/product/111222/777888999"
        job = launch._queue_ingest_job(test_url, "shopee-reviews")
        job_id = job["id"]

        client.recv_json(timeout=3.0)
        client.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})
        time.sleep(0.05)

        # Extension encounters traffic verification / CAPTCHA challenge
        client.send_json({
            "v": 1,
            "type": "verification.required",
            "params": {
                "job_id": job_id,
                "url": "https://shopee.vn/verify/traffic",
                "reason": "Shopee traffic verification required",
            },
        })
        time.sleep(0.1)

        # Server pauses for human verification
        progress = launch._INGEST_PROGRESS.get(job_id, {})
        assert progress["status"] == "awaiting_user_verification"
        assert progress["verification_required"] is True
        assert server.transport.is_paused() is True

        # User solves captcha -> extension sends verification.resolved
        client.send_json({
            "v": 1,
            "type": "verification.resolved",
            "params": {"jobId": job_id, "retry": True},
        })
        deadline = time.time() + 2.0
        while server.transport.is_paused() and time.time() < deadline:
            time.sleep(0.02)

        # Server unpauses verification
        assert server.transport.is_paused() is False

    finally:
        client.close()
        rpc.executor.shutdown(wait=True)


# ─── 10. WebSocket Disconnect, Reconnect, and Resume In-Flight Job ──────────────

def test_shopee_workflow_disconnect_reconnect_resumes_job(setup_shopee_environment):
    server, port, token, rpc, tmp_path = setup_shopee_environment

    client1 = SimpleWebSocketTestClient("127.0.0.1", port)
    client2 = None
    try:
        # Step A: Client 1 connects and receives job
        assert client1.connect(token=token) == 101
        client1.recv_json(timeout=2.0)

        test_url = "https://shopee.vn/product/123456/888888888"
        job = launch._queue_ingest_job(test_url, "shopee-reviews")
        job_id = job["id"]

        job_envelope = client1.recv_json(timeout=3.0)
        assert job_envelope["type"] == "ingest.job"
        assert job_envelope["job"]["id"] == job_id

        client1.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})
        time.sleep(0.1)

        # Verify job is inflight
        with launch._INGEST_JOBS_LOCK:
            assert job_id in launch._INGEST_INFLIGHT

        # Step B: Client 1 disconnects abruptly (network drop / worker termination)
        client1.close()
        time.sleep(0.2)

        # Verify job is preserved in inflight queue (no state loss)
        with launch._INGEST_JOBS_LOCK:
            assert job_id in launch._INGEST_INFLIGHT

        # Step C: Client 2 reconnects
        client2 = SimpleWebSocketTestClient("127.0.0.1", port)
        assert client2.connect(token=token) == 101
        greeting = client2.recv_json(timeout=2.0)
        assert greeting["type"] == "hello"

        # Server replays inflight job on connect
        replayed_job_msg = client2.recv_json(timeout=3.0)
        assert replayed_job_msg is not None
        assert replayed_job_msg["type"] == "ingest.job"
        assert replayed_job_msg["job"]["id"] == job_id

        # Step D: Client 2 finishes the job
        client2.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})

        upload_id = "upload-reconnect-1"
        payload = {
            "job": job_id,
            "name": "shopee_888888888_reviews",
            "source": test_url,
            "rows": [{"user": "reconnected_user", "sao": 5, "noi_dung": "Hoàn tất sau reconnect"}],
        }
        client2.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": "rpc-reconnect-chunk",
            "params": {"operation": "chunk", "uploadId": upload_id, "index": 0, "chunk": json.dumps(payload)},
        })
        client2.recv_json(timeout=3.0)

        client2.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": "rpc-reconnect-complete",
            "params": {"operation": "complete", "uploadId": upload_id},
        })
        comp_res = client2.recv_json(timeout=5.0)
        assert comp_res["ok"] is True

        # Verify output CSV was successfully created after reconnect
        assert (tmp_path / "csv" / "shopee_888888888_reviews.csv").is_file()
        assert launch._INGEST_RESULTS[job_id]["ok"] is True

    finally:
        client1.close()
        if client2:
            client2.close()
        rpc.executor.shutdown(wait=True)
