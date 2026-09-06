"""Unit tests for browser_bridge package.

Covers:
- Protocol envelopes, serialization, errors, size limits
- Security: pairing token generation, constant-time validation, Origin checks, strict same-origin, audit redaction
- Action Registry: schemas and parameter validation for all 18 actions
- Transport: correlation IDs, deadlines, idempotency, verification states, and fallback
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from browser_bridge.actions import (
    ALL_ACTIONS,
    ActionName,
    DomClickParams,
    DomQueryParams,
    FetchSameOriginParams,
    PageNavigateParams,
    TabOpenParams,
    validate_action_params,
)
from browser_bridge.protocol import (
    BridgeError,
    ErrorCode,
    MAX_MESSAGE_BYTES,
    MessageEnvelope,
    MessageType,
    redact_sensitive_data,
)
from browser_bridge.security import (
    AuditLogger,
    generate_pairing_token,
    is_same_origin,
    validate_extension_origin,
    verify_pairing_token,
)
from browser_bridge.transport import (
    ExtensionState,
    HttpPollingTransport,
    TransportManager,
    WebSocketTransport,
)


# ── Protocol & Envelope Tests ───────────────────────────────────────────────

def test_message_envelope_roundtrip():
    env = MessageEnvelope(
        type=MessageType.COMMAND,
        id="cmd-123",
        sessionId="sess-1",
        deadlineMs=5000,
        action="page.navigate",
        params={"url": "https://example.com"},
    )
    d = env.to_dict()
    assert d["v"] == 1
    assert d["type"] == "command"
    assert d["id"] == "cmd-123"
    assert d["action"] == "page.navigate"
    assert d["params"]["url"] == "https://example.com"

    recovered = MessageEnvelope.from_dict(d)
    assert recovered.id == env.id
    assert recovered.type == env.type
    assert recovered.action == env.action
    assert recovered.params == env.params


def test_message_envelope_size_limit():
    huge_data = "x" * (MAX_MESSAGE_BYTES + 10)
    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        MessageEnvelope(type=MessageType.COMMAND, params={"data": huge_data})


def test_bridge_error_structure():
    err = BridgeError.create(ErrorCode.TIMEOUT, "Action timed out after 5000ms", details={"details": "none"})
    assert err.code == ErrorCode.TIMEOUT.value
    assert "timed out" in err.message
    d = err.to_dict()
    assert d["code"] == "TIMEOUT"
    assert d["details"] == {"details": "none"}


# ── Security & Authentication Tests ──────────────────────────────────────────

def test_pairing_token_generation_and_verification():
    token = generate_pairing_token()
    assert len(token) >= 32
    assert verify_pairing_token(token, token) is True
    assert verify_pairing_token("wrong-token", token) is False
    assert verify_pairing_token("", token) is False
    assert verify_pairing_token(None, token) is False


def test_validate_extension_origin():
    valid, ext_id = validate_extension_origin("chrome-extension://abcdefghijklmnopabcdefghijklmnop")
    assert valid is True
    assert ext_id == "abcdefghijklmnopabcdefghijklmnop"

    # Reject standard web origins
    valid, _ = validate_extension_origin("https://evil.com")
    assert valid is False
    valid, _ = validate_extension_origin("http://127.0.0.1:8080")
    assert valid is False
    valid, _ = validate_extension_origin("file:///etc/passwd")
    assert valid is False
    valid, _ = validate_extension_origin("")
    assert valid is False

    # Allowlist filtering
    allowlist = {"trustedextensionid12345678901234"}
    valid, _ = validate_extension_origin("chrome-extension://trustedextensionid12345678901234", allowlist)
    assert valid is True
    valid, _ = validate_extension_origin("chrome-extension://untrustedextensionid1234567890", allowlist)
    assert valid is False


def test_strict_same_origin_validation():
    tab_url = "https://shopee.vn/product/123/456"
    assert is_same_origin(tab_url, "https://shopee.vn/api/v4/item/get") is True
    assert is_same_origin(tab_url, "https://shopee.vn:443/api/v4/item/get") is True
    # Different scheme
    assert is_same_origin(tab_url, "http://shopee.vn/api/v4/item/get") is False
    # Different host / subdomain
    assert is_same_origin(tab_url, "https://api.shopee.vn/item/get") is False
    assert is_same_origin(tab_url, "https://evil.com") is False
    # Port mismatch
    assert is_same_origin("http://localhost:8766", "http://localhost:8767") is False


def test_redact_sensitive_data_and_audit():
    sensitive = {
        "token": "secret-token-12345",
        "authorization": "Bearer ya29.secret",
        "cookie": "SPC_EC=abc123xyz; session=pass",
        "safe_data": "hello world",
        "nested": {
            "password": "super-password",
            "number": 42,
        },
    }
    redacted = redact_sensitive_data(sensitive)
    assert redacted["token"] == "[REDACTED]"
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["cookie"] == "[REDACTED]"
    assert redacted["safe_data"] == "hello world"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["number"] == 42

    logger = AuditLogger()
    record = logger.record("test.event", details=sensitive)
    assert record["details"]["token"] == "[REDACTED]"
    assert record["details"]["cookie"] == "[REDACTED]"


# ── Action Registry Tests ───────────────────────────────────────────────────

def test_action_registry_contains_18_actions():
    expected_actions = {
        "browser.health",
        "tab.list",
        "tab.getActive",
        "tab.open",
        "tab.focus",
        "page.navigate",
        "page.getUrl",
        "page.getTitle",
        "page.waitFor",
        "dom.query",
        "dom.queryAll",
        "dom.getText",
        "dom.getAttribute",
        "dom.click",
        "input.type",
        "input.select",
        "page.snapshot",
        "fetch.sameOrigin",
        "job.cancel",
    }
    registered = set(ALL_ACTIONS)
    assert expected_actions.issubset(registered)
    assert len(ALL_ACTIONS) >= 18


def test_action_param_validation():
    # Valid tab.open
    params = validate_action_params(ActionName.TAB_OPEN, {"url": "https://shopee.vn", "active": True})
    assert isinstance(params, TabOpenParams)
    assert params.url == "https://shopee.vn"

    # Invalid tab.open (missing url)
    with pytest.raises(Exception):
        validate_action_params(ActionName.TAB_OPEN, {})

    # Valid page.navigate
    nav = validate_action_params(ActionName.PAGE_NAVIGATE, {"url": "https://shopee.vn", "waitUntil": "networkidle"})
    assert isinstance(nav, PageNavigateParams)

    # Valid dom.query
    dq = validate_action_params(ActionName.DOM_QUERY, {"selector": "div.product-item", "tabId": 123})
    assert isinstance(dq, DomQueryParams)

    # Valid dom.click
    dc = validate_action_params(ActionName.DOM_CLICK, {"selector": "button.submit"})
    assert isinstance(dc, DomClickParams)

    # Valid fetch.sameOrigin
    fso = validate_action_params(ActionName.FETCH_SAME_ORIGIN, {"pathOrUrl": "/api/v4/items", "method": "GET"})
    assert isinstance(fso, FetchSameOriginParams)


# ── Transport & Correlation Tests ───────────────────────────────────────────

def test_websocket_transport_command_correlation_and_timeout():
    import threading
    sent_envelopes = []
    connected = [True]

    transport = WebSocketTransport(
        send_fn=lambda env: sent_envelopes.append(env) or True,
        is_connected_fn=lambda: connected[0],
    )
    transport.set_state(ExtensionState.CONNECTED)

    result_box = []

    def caller():
        ok, res, err = transport.execute_command(
            action=ActionName.PAGE_NAVIGATE,
            params={"url": "https://example.com"},
            deadline_ms=2000,
            command_id="cmd-corr-1",
        )
        result_box.append((ok, res, err))

    t = threading.Thread(target=caller)
    t.start()

    # Wait until command envelope is transmitted
    deadline = time.time() + 1.0
    while not sent_envelopes and time.time() < deadline:
        time.sleep(0.02)

    assert len(sent_envelopes) == 1
    sent_env = sent_envelopes[0]
    assert sent_env["type"] == "command"
    assert sent_env["id"] == "cmd-corr-1"

    # Inbound result envelope
    response_env = MessageEnvelope(
        type=MessageType.RESULT,
        id="cmd-corr-1",
        result={"url": "https://example.com", "loaded": True},
    )
    transport.handle_inbound_envelope(response_env.to_dict())

    t.join(timeout=1.0)
    assert len(result_box) == 1
    ok, res, err = result_box[0]
    assert ok is True
    assert res == {"url": "https://example.com", "loaded": True}
    assert err is None

    # Verify idempotency caching
    ok2, res2, err2 = transport.execute_command(
        action=ActionName.PAGE_NAVIGATE,
        params={"url": "https://example.com"},
        command_id="cmd-corr-1",
    )
    assert ok2 is True
    assert res2 == {"url": "https://example.com", "loaded": True}


def test_websocket_transport_verification_flow():
    connected = [True]
    transport = WebSocketTransport(
        send_fn=lambda env: True,
        is_connected_fn=lambda: connected[0],
    )
    transport.set_state(ExtensionState.CONNECTED)

    # Inbound verification.required event
    verify_env = MessageEnvelope(
        type=MessageType.VERIFICATION_REQUIRED,
        params={"reason": "Shopee bot detection: slide captcha", "url": "https://shopee.vn/verify/traffic"},
    )
    transport.handle_inbound_envelope(verify_env.to_dict())

    assert transport.get_state() == ExtensionState.AWAITING_USER_VERIFICATION
    info = transport.get_verification_info()
    assert info is not None
    assert "Shopee" in info.get("reason", "")

    # Resolve verification
    transport.resume_verification()
    assert transport.get_state() == ExtensionState.CONNECTED
    assert transport.get_verification_info() is None


def test_transport_manager_fallback():
    connected = [False]
    ws_transport = WebSocketTransport(
        send_fn=lambda env: True,
        is_connected_fn=lambda: connected[0],
    )
    http_transport = HttpPollingTransport(base_url="http://127.0.0.1:8766")
    manager = TransportManager(ws_transport=ws_transport, http_transport=http_transport)

    # When WS is disconnected, fallback to HTTP
    assert ws_transport.is_connected() is False
    assert manager.active_transport_name == "http-polling"

    # When WS is connected, preference is WS
    connected[0] = True
    assert manager.active_transport_name == "websocket"


def test_browser_ws_bridge_wrapper_and_lifecycle():
    from browser_ws_bridge import BrowserWebSocketBridge

    # Empty token must fail
    with pytest.raises(ValueError, match="must not be empty"):
        BrowserWebSocketBridge("")

    disconnect_called = [False]
    connect_called = [False]

    bridge = BrowserWebSocketBridge(
        "test-valid-token-12345678901234567890",
        on_connect=lambda: connect_called.__setitem__(0, True),
        on_disconnect=lambda: disconnect_called.__setitem__(0, True),
    )

    assert bridge.connected is False

    # Send returns False when not connected
    assert bridge.send({"type": "test"}) is False

    # Send accepts MessageEnvelope
    env = MessageEnvelope(type=MessageType.PING)
    assert bridge.send(env) is False

    # Context manager test
    with bridge as b:
        assert b is bridge

    # on_disconnect callback triggering
    assert bridge.on_disconnect is not None


def test_websocket_transport_verification_resolved_flow():
    connected = [True]
    transport = WebSocketTransport(
        send_fn=lambda env: True,
        is_connected_fn=lambda: connected[0],
    )
    transport.set_state(ExtensionState.CONNECTED)

    # Trigger verification
    verify_env = MessageEnvelope(
        type=MessageType.VERIFICATION_REQUIRED,
        params={"reason": "Captcha challenge"},
    )
    transport.handle_inbound_envelope(verify_env.to_dict())
    assert transport.get_state() == ExtensionState.AWAITING_USER_VERIFICATION
    assert transport.get_verification_info() == {"reason": "Captcha challenge"}

    # Inbound verification.resolved resets state
    resolved_env = MessageEnvelope(
        type=MessageType.VERIFICATION_RESOLVED,
        params={"status": "resumed"},
    )
    transport.handle_inbound_envelope(resolved_env.to_dict())
    assert transport.get_state() == ExtensionState.CONNECTED
    assert transport.get_verification_info() is None


def test_buffered_socket_reader_logic():
    from browser_bridge.server import _BufferedSocketReader

    class DummySocket:
        def __init__(self, wire_bytes: bytes):
            self.wire = bytearray(wire_bytes)

        def recv(self, n: int) -> bytes:
            take = min(len(self.wire), n)
            chunk = bytes(self.wire[:take])
            del self.wire[:take]
            return chunk

    # Reader with initial leftover data of 4 bytes, wire with 4 bytes
    sock = DummySocket(b"5678")
    reader = _BufferedSocketReader(sock, initial_data=b"1234")

    # Read first 2 bytes from buffer
    assert reader.read_exact(2) == b"12"
    # Read next 4 bytes (2 from buffer, 2 from wire)
    assert reader.read_exact(4) == b"3456"
    # Read remaining 2 bytes from wire
    assert reader.read_exact(2) == b"78"


def test_all_message_types_serialization():
    types_to_test = [
        MessageType.COMMAND,
        MessageType.ACCEPTED,
        MessageType.PROGRESS,
        MessageType.RESULT,
        MessageType.ERROR,
        MessageType.CANCEL,
        MessageType.VERIFICATION_REQUIRED,
        MessageType.VERIFICATION_RESOLVED,
        MessageType.PING,
        MessageType.PONG,
    ]
    for mtype in types_to_test:
        env = MessageEnvelope(
            type=mtype,
            id=f"test-{mtype}",
            params={"kind": str(mtype)},
            result={"ok": True} if mtype == MessageType.RESULT else None,
            error=BridgeError.create(ErrorCode.TIMEOUT, "test") if mtype == MessageType.ERROR else None,
        )
        d = env.to_dict()
        assert d["type"] == mtype.value
        recovered = MessageEnvelope.from_dict(d)
        assert recovered.type == mtype


def test_same_origin_bypass_vectors():
    tab_url = "https://shop.example.com/product/123"

    # Same host, same port, same scheme
    assert is_same_origin("https://shop.example.com/api/details", tab_url) is True
    assert is_same_origin("/api/details", tab_url) is True
    assert is_same_origin("details", tab_url) is True

    # Bypass vectors
    # 1. Subdomain suffix bypass (e.g. shop.example.com.attacker.com)
    assert is_same_origin("https://shop.example.com.attacker.com/api", tab_url) is False
    # 2. Userinfo bypass (e.g. https://shop.example.com@attacker.com)
    assert is_same_origin("https://shop.example.com@attacker.com/api", tab_url) is False
    assert is_same_origin("https://attacker.com@shop.example.com/api", tab_url) is False
    # 3. Port mismatch
    assert is_same_origin("https://shop.example.com:8443/api", tab_url) is False
    # 4. Scheme mismatch
    assert is_same_origin("http://shop.example.com/api", tab_url) is False
    # 5. Non-web schemes
    assert is_same_origin("data:text/html,payload", tab_url) is False
    assert is_same_origin("file:///etc/passwd", tab_url) is False
    assert is_same_origin("javascript:alert(1)", tab_url) is False
    assert is_same_origin("chrome://extensions", tab_url) is False
    # 6. Invalid / malformed URLs
    assert is_same_origin("", tab_url) is False
    assert is_same_origin("://malformed", tab_url) is False


def test_empty_params_and_extra_forbidden():
    from pydantic import ValidationError

    # Empty params valid for tab.list and browser.health
    p1 = validate_action_params(ActionName.BROWSER_HEALTH, {})
    assert p1 is not None
    p2 = validate_action_params(ActionName.TAB_LIST, {})
    assert p2 is not None

    # Extra fields forbidden
    with pytest.raises(ValidationError):
        validate_action_params(ActionName.BROWSER_HEALTH, {"unexpected_field": 123})


def test_enum_validation_and_disallowed_params():
    from pydantic import ValidationError
    from browser_bridge.actions import PageNavigateParams, PageSnapshotParams, PageWaitForParams

    # Invalid waitUntil enum
    with pytest.raises(ValidationError):
        PageNavigateParams(url="https://example.com", waitUntil="instant")

    # Invalid state enum
    with pytest.raises(ValidationError):
        PageWaitForParams(selector="button", state="clickable")

    # Invalid snapshot format
    with pytest.raises(ValidationError):
        PageSnapshotParams(format="accessibility")

    # Forbidden authorization header in fetch.sameOrigin
    with pytest.raises(ValidationError, match="(?i)authorization"):
        validate_action_params(
            ActionName.FETCH_SAME_ORIGIN,
            {"pathOrUrl": "/api", "headers": {"Authorization": "Bearer secret"}},
        )


def test_verification_pause_blocks_actions():
    connected = [True]
    transport = WebSocketTransport(
        send_fn=lambda env: True,
        is_connected_fn=lambda: connected[0],
    )
    transport.set_state(ExtensionState.AWAITING_USER_VERIFICATION, {"reason": "Captcha"})

    # Action that modifies page must be blocked
    ok, res, err = transport.execute_command(
        action=ActionName.PAGE_NAVIGATE,
        params={"url": "https://example.com"},
    )
    assert ok is False
    assert err is not None
    assert err.code == ErrorCode.VERIFICATION_REQUIRED.value

    # Whitelisted inspection action must be allowed
    ok2, res2, err2 = transport.execute_command(
        action=ActionName.TAB_LIST,
        deadline_ms=500,
    )
    # Even if it times out waiting for mock response, it was NOT blocked by verification check
    assert err2.code != ErrorCode.VERIFICATION_REQUIRED.value


def test_websocket_transport_concurrency_semaphore():
    import threading

    transport = WebSocketTransport(
        send_fn=lambda env: True,
        is_connected_fn=lambda: True,
        max_concurrency=1,
    )
    transport.set_state(ExtensionState.CONNECTED)

    results = []

    def task1():
        ok, res, err = transport.execute_command(
            action=ActionName.TAB_LIST,
            deadline_ms=1500,
            command_id="cmd-task-1",
        )
        results.append((1, ok, err))

    def task2():
        time.sleep(0.05)
        # Attempt to run second concurrent command while task1 is occupying the single permit
        ok, res, err = transport.execute_command(
            action=ActionName.TAB_LIST,
            deadline_ms=1500,
            command_id="cmd-task-2",
        )
        results.append((2, ok, err))

    t1 = threading.Thread(target=task1)
    t2 = threading.Thread(target=task2)
    t1.start()
    t2.start()

    time.sleep(0.2)
    # Satisfy task1
    transport.handle_inbound_envelope({
        "v": 1,
        "type": "result",
        "id": "cmd-task-1",
        "result": [],
    })

    time.sleep(0.2)
    # Satisfy task2
    transport.handle_inbound_envelope({
        "v": 1,
        "type": "result",
        "id": "cmd-task-2",
        "result": [],
    })

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert len(results) == 2
    for item in results:
        assert item[1] is True  # ok == True


def test_http_polling_network_error_handling():
    # Transport pointing to nonexistent port returns BridgeError safely without unhandled crash
    transport = HttpPollingTransport(base_url="http://127.0.0.1:59999")
    ok, res, err = transport.execute_command(
        action="shopee-reviews",
        params={"url": "https://shopee.vn/product/1/2"},
        deadline_ms=1000,
    )
    assert ok is False
    assert err is not None
    assert err.code == ErrorCode.INTERNAL_ERROR.value
    assert err.retryable is True



@pytest.mark.parametrize("raises", [False, True])
def test_failed_send_cleans_pending_and_busy_state(raises):
    def send(_):
        if raises:
            raise OSError("socket closed")
        return False

    transport = WebSocketTransport(send_fn=send, is_connected_fn=lambda: True)
    transport.set_state(ExtensionState.CONNECTED)
    ok, _, error = transport.execute_command("tab.list")
    assert not ok and error is not None
    assert transport.get_state() == ExtensionState.CONNECTED
    assert not transport._pending


def test_http_polling_accepts_helper_nested_job_response():
    transport = HttpPollingTransport(
        queue_fn=lambda url, kind: {"ok": True, "job": {"id": "nested-job"}},
        get_result_fn=lambda job_id: ({"ok": True, "id": job_id}, None),
    )
    ok, result, error = transport.execute_command("shopee-reviews", {"url": "https://shopee.vn/product/1/2"})
    assert ok and error is None
    assert result["id"] == "nested-job"
