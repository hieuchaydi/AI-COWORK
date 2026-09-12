"""Exercise the real helper HTTP command boundary with a controlled transport."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest
import launch


@pytest.fixture
def command_api(monkeypatch):
    transport = Mock()
    transport.execute_command.return_value = (True, [{"id": 7}], None)
    bridge = Mock(token="test-bridge-token", allowlisted_extension_ids=None, transport=transport)
    monkeypatch.setattr(launch, "_BROWSER_WS", bridge)
    server = ThreadingHTTPServer(("127.0.0.1", 0), launch._HelperHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/browser/command", transport
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request_command(url, body, token="test-bridge-token", origin=None):
    headers = {"Content-Type": "application/json", "X-Bridge-Token": token}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_command_dispatches_with_correlation(command_api):
    url, transport = command_api
    status, body = request_command(url, {"type": "command", "action": "tab.list", "id": "cmd-7"})
    assert status == 200 and body["ok"] and body["id"] == "cmd-7"
    assert body["result"] == [{"id": 7}]
    transport.execute_command.assert_called_once_with(
        "tab.list", {}, deadline_ms=30000, command_id="cmd-7", session_id=None)


def test_solver_abstention_is_a_successful_http_verdict(command_api, monkeypatch):
    url, _ = command_api
    verdict = {"ok": False, "abstain": True, "reason": "no_puzzle_in_frame", "travel": None}
    monkeypatch.setattr("browser_bridge.captcha_detector.solve_puzzle_cv", lambda **kwargs: verdict)
    status, body = request_command(url.replace("/browser/command", "/browser/solve_puzzle_cv"), {"screenshot": "blank"})
    assert status == 200
    assert body["abstain"] is True
    assert body["travel"] is None


@pytest.mark.parametrize("token,origin,status", [
    ("wrong", None, 401),
    ("test-bridge-token", "https://example.com", 403),
])
def test_command_rejects_unauthorized_callers(command_api, token, origin, status):
    url, transport = command_api
    actual, _ = request_command(url, {"type": "command", "action": "tab.list"}, token, origin)
    assert actual == status
    transport.execute_command.assert_not_called()


@pytest.mark.parametrize("body", [[], {}, {"type": "command", "action": "unknown"},
    {"type": "command", "action": "tab.list", "deadlineMs": 0},
    {"type": "command", "action": "tab.list", "params": {"invalid": True}}])
def test_command_rejects_invalid_envelopes(command_api, body):
    url, transport = command_api
    status, _ = request_command(url, body)
    assert status == 400
    transport.execute_command.assert_not_called()


def test_acknowledged_ingest_job_replays_until_result_saved(monkeypatch, tmp_path):
    job = {"id": "replay-job", "url": "https://example.com", "kind": "example"}
    monkeypatch.setattr(launch, "_INGEST_JOBS", [job])
    monkeypatch.setattr(launch, "_INGEST_INFLIGHT", {})
    bridge = Mock()
    monkeypatch.setattr(launch, "_BROWSER_WS", bridge)
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    launch._ack_ingest_job(job["id"])
    assert launch._INGEST_JOBS == []
    launch._on_browser_ws_connect()
    bridge.send.assert_called_once_with({"type": "ingest.job", "job": job})
    status, result = launch._store_ingest_payload({"job": job["id"], "rows": [{"text": "done"}]})
    assert status == 200 and result["ok"]
    assert launch._INGEST_INFLIGHT == {}
    bridge.reset_mock()
    launch._on_browser_ws_connect()
    bridge.send.assert_not_called()


def test_chrome_launcher_guides_unpacked_setup_instead_of_unsupported_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch, "_chrome_proc", None)
    monkeypatch.setattr(launch, "_find_chrome", lambda: "chrome.exe")
    popen = Mock()
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    assert launch._ensure_chrome_with_extension()
    args = popen.call_args.args[0]
    assert "chrome://extensions/" in args
    assert not any(arg.startswith("--load-extension") for arg in args)


def test_registered_extension_does_not_repeat_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    profile = tmp_path / "chrome-profile"
    default = profile / "Default"
    default.mkdir(parents=True)
    (default / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {
        "example": {"path": str(tmp_path / "browser-extension")}
    }}}), encoding="utf-8")
    assert launch._profile_has_browser_extension(profile)


def test_transport_allows_tab_open_during_verification():
    from browser_bridge.transport import WebSocketTransport, ExtensionState

    transport = WebSocketTransport(send_fn=lambda data: True, is_connected_fn=lambda: True)
    transport.set_state(ExtensionState.AWAITING_USER_VERIFICATION, {"url": "https://shopee.vn/verify/traffic?id=123"})

    # Action outside allowlist should be rejected immediately with VERIFICATION_REQUIRED
    ok, res, err = transport.execute_command("page.evaluate", {"script": "1+1"})
    assert not ok
    assert err is not None and err.code.lower() == "verification_required"

    # Action in allowlist should pass the verification state check
    ok, res, err = transport.execute_command("tab.open", {"url": "https://shopee.vn/verify/traffic?id=123"}, deadline_ms=10)
    assert err is None or err.code.lower() != "verification_required"


def test_open_verification_endpoint(monkeypatch):
    transport = Mock()
    transport.get_verification_info.return_value = {
        "url": "https://shopee.vn/product/123",
        "verification_url": "https://shopee.vn/verify/traffic?anti_bot_tracking_id=test123",
        "tab_id": 99,
    }
    transport.execute_command.return_value = (True, {"id": 99}, None)
    bridge = Mock(token="test-bridge-token", allowlisted_extension_ids=None, transport=transport)
    monkeypatch.setattr(launch, "_BROWSER_WS", bridge)
    server = ThreadingHTTPServer(("127.0.0.1", 0), launch._HelperHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/browser/open_verification"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            assert resp.status == 200
            assert data["ok"] is True
            assert data["opened_url"] == "https://shopee.vn/verify/traffic?anti_bot_tracking_id=test123"
            transport.execute_command.assert_called_once_with("tab.open", {"url": "https://shopee.vn/verify/traffic?anti_bot_tracking_id=test123"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

