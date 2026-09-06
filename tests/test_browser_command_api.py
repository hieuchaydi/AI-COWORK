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
