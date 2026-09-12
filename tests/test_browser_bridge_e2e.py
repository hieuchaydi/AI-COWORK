"""End-to-End integration tests for Browser Gateway WebSocket server and protocol.

Exercises real TCP sockets, RFC 6455 handshakes, typed actions, security rejections,
and the human verification pause/resume flow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import browser_bridge.server as gateway_module
from browser_bridge.protocol import MessageEnvelope, MessageType
from browser_bridge.server import BrowserGatewayServer
from browser_bridge.transport import ExtensionState


class SimpleWebSocketTestClient:
    """Minimal dependency-free RFC 6455 client for testing BrowserGatewayServer."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self._unread = bytearray()

    def connect(
        self,
        path: str = "/browser/v1/ws",
        token: Optional[str] = None,
        origin: Optional[str] = "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
        client_id: Optional[str] = None,
        takeover: bool = False,
    ) -> int:
        self.sock.connect((self.host, self.port))
        query_params = {}
        if token:
            query_params["token"] = token
        if client_id:
            query_params["client_id"] = client_id
        if takeover:
            query_params["takeover"] = "1"
        query = f"?{urllib.parse.urlencode(query_params)}" if query_params else ""
        sec_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")

        req = (
            f"GET {path}{query} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        )
        if origin is not None:
            req += f"Origin: {origin}\r\n"
        req += "\r\n"
        self.sock.sendall(req.encode("ascii"))

        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            resp.extend(chunk)

        parts = resp.split(b"\r\n\r\n", 1)
        header_bytes = parts[0]
        if len(parts) > 1:
            self._unread.extend(parts[1])

        first_line = header_bytes.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        tokens = first_line.split()
        if len(tokens) >= 2 and tokens[1].isdigit():
            return int(tokens[1])
        return 500

    def _recv_exact(self, n: int) -> bytes:
        data = bytearray()
        if self._unread:
            take = min(len(self._unread), n)
            data.extend(self._unread[:take])
            del self._unread[:take]
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Socket closed")
            data.extend(chunk)
        return bytes(data)

    def send_json(self, payload: Dict[str, Any], fragment_at: Optional[int] = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if fragment_at is not None and 0 < fragment_at < len(data):
            # Chrome fragments large client messages; exercise that path on purpose.
            self._send_frame(data[:fragment_at], opcode=0x1, fin=False)
            self._send_frame(data[fragment_at:], opcode=0x0, fin=True)
            return
        self._send_frame(data, opcode=0x1, fin=True)

    def _send_frame(self, data: bytes, *, opcode: int, fin: bool) -> None:
        mask = secrets.token_bytes(4)
        length = len(data)

        header = bytearray([(0x80 if fin else 0x00) | opcode])
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))

        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_json(self, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        self.sock.settimeout(timeout)
        try:
            b0_b1 = self._recv_exact(2)
            if len(b0_b1) < 2:
                return None
            b0, b1 = b0_b1[0], b0_b1[1]
            opcode = b0 & 0x0F
            has_mask = bool(b1 & 0x80)
            length = b1 & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = self._recv_exact(8)
                length = int.from_bytes(ext, "big")

            mask = self._recv_exact(4) if has_mask else None
            data = self._recv_exact(length)

            if has_mask and mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # Close
                return None
            return json.loads(data.decode("utf-8"))
        except (ConnectionError, OSError):
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


@pytest.fixture
def gateway_server():
    token = secrets.token_urlsafe(32)
    server = BrowserGatewayServer(token=token)
    port = server.start("127.0.0.1", 0)
    try:
        yield server, port, token
    finally:
        server.stop()


def test_e2e_successful_handshake_and_greeting(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token)
        assert status == 101

        # The server must send a Hello envelope upon connection
        greeting = client.recv_json(timeout=2.0)
        assert greeting is not None
        assert greeting.get("type") == "hello"
        assert greeting.get("params", {}).get("protocolVersion") == "1.0"
        assert server.is_connected is True
    finally:
        client.close()


def test_e2e_rejection_on_invalid_token(gateway_server):
    _, port, _ = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token="wrong-token")
        assert status == 401
    finally:
        client.close()


def test_e2e_rejection_on_web_origin(gateway_server):
    _, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token, origin="https://malicious-website.com")
        assert status == 403
    finally:
        client.close()


def test_e2e_allows_originless_extension_websocket_with_valid_token(gateway_server):
    _, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token, origin=None)
        assert status == 101
        assert client.recv_json()["type"] == "hello"
    finally:
        client.close()


def test_e2e_command_dispatch_and_result(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token)
        assert status == 101
        _ = client.recv_json()  # Read hello

        # Execute command from Gateway in a thread
        result_container = []

        def execute_worker():
            ok, res, err = server.transport.execute_command(
                action="tab.list",
                deadline_ms=4000,
            )
            result_container.append((ok, res, err))

        t = threading.Thread(target=execute_worker)
        t.start()

        # Client receives command envelope
        cmd = client.recv_json(timeout=3.0)
        assert cmd is not None
        assert cmd.get("type") == "command"
        assert cmd.get("action") == "tab.list"
        cmd_id = cmd.get("id")

        # Client responds with simulated open tabs
        mock_tabs = [{"id": 1, "url": "https://shopee.vn", "title": "Shopee"}]
        client.send_json({
            "v": 1,
            "type": "result",
            "id": cmd_id,
            "result": mock_tabs,
        })

        t.join(timeout=3.0)
        assert len(result_container) == 1
        ok, res, err = result_container[0]
        assert ok is True
        assert res == mock_tabs
        assert err is None
    finally:
        client.close()


def test_e2e_human_verification_and_resume_flow(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token)
        assert status == 101
        _ = client.recv_json()

        # Extension encounters Shopee verification checkpoint
        client.send_json({
            "v": 1,
            "type": "verification.required",
            "params": {
                "reason": "Shopee slide captcha triggered",
                "url": "https://shopee.vn/verify/traffic",
            },
        })

        # Wait for state update
        deadline = time.time() + 2.0
        while server.transport.get_state() != ExtensionState.AWAITING_USER_VERIFICATION and time.time() < deadline:
            time.sleep(0.05)

        assert server.transport.get_state() == ExtensionState.AWAITING_USER_VERIFICATION
        info = server.transport.get_verification_info()
        assert info is not None
        assert "Shopee slide captcha" in info.get("reason", "")

        # Resume verification (simulating user clicking Resume in popup or gateway endpoint)
        server.transport.resume_verification()
        assert server.transport.get_state() == ExtensionState.CONNECTED
    finally:
        client.close()


def test_e2e_duplicate_client_is_rejected_and_takeover_is_explicit(gateway_server):
    server, port, token = gateway_server
    client1 = SimpleWebSocketTestClient("127.0.0.1", port)
    client2 = SimpleWebSocketTestClient("127.0.0.1", port)
    client3 = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        # Client 1 connects
        s1 = client1.connect(token=token, client_id="owner-1")
        assert s1 == 101
        _ = client1.recv_json()
        assert server.is_connected is True

        # Client 2 cannot silently replace the single active owner.
        s2 = client2.connect(token=token, client_id="owner-2")
        assert s2 == 101
        rejection = client2.recv_json()
        assert rejection["type"] == "error"
        assert rejection["error"]["code"] == "CLIENT_ALREADY_CONNECTED"
        assert server.is_connected is True
        assert server.active_connection_info()["clientId"] == "owner-1"
        client2.close()

        # A user-initiated takeover is the only way to transfer ownership.
        s3 = client3.connect(token=token, client_id="owner-3", takeover=True)
        assert s3 == 101
        _ = client3.recv_json()
        assert server.is_connected is True
        assert server.active_connection_info()["clientId"] == "owner-3"

        # Client 1 closes its socket
        client1.close()
        time.sleep(0.2)

        # Server transport state MUST remain CONNECTED because client 2 is active!
        assert server.is_connected is True
        assert server.transport.get_state() == ExtensionState.CONNECTED

        # Can still execute command via the explicit takeover owner.
        result_container = []

        def execute_worker():
            ok, res, err = server.transport.execute_command(
                action="tab.list",
                deadline_ms=4000,
            )
            result_container.append((ok, res, err))

        t = threading.Thread(target=execute_worker)
        t.start()

        cmd = client3.recv_json(timeout=3.0)
        assert cmd is not None
        assert cmd.get("action") == "tab.list"

        client3.send_json({
            "v": 1,
            "type": "result",
            "id": cmd.get("id"),
            "result": [{"id": 42}],
        })

        t.join(timeout=3.0)
        assert len(result_container) == 1
        ok, res, err = result_container[0]
        assert ok is True
        assert res == [{"id": 42}]
    finally:
        client1.close()
        client2.close()
        client3.close()


def test_e2e_pipelined_handshake_frame_buffering(gateway_server):
    server, port, token = gateway_server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect(("127.0.0.1", port))
        sec_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        req = (
            f"GET /browser/v1/ws?token={token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: chrome-extension://abcdefghijklmnopabcdefghijklmnop\r\n\r\n"
        ).encode("ascii")

        # Create a masked ping frame to send simultaneously with the handshake request
        ping_payload = json.dumps({"v": 1, "type": "ping"}).encode("utf-8")
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(ping_payload))
        frame = bytes([0x81, 0x80 | len(ping_payload)]) + mask + masked

        # Send BOTH together in a single sendall call (pipelined)
        sock.sendall(req + frame)

        # Read HTTP response
        resp = bytearray()
        unread = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                break
            resp.extend(chunk)

        parts = resp.split(b"\r\n\r\n", 1)
        assert b"101 Switching Protocols" in parts[0]
        if len(parts) > 1:
            unread.extend(parts[1])

        # Receive frames via SimpleWebSocketTestClient
        client = SimpleWebSocketTestClient("127.0.0.1", port)
        client.sock = sock
        client._unread = unread

        hello = client.recv_json(timeout=2.0)
        assert hello is not None
        assert hello.get("type") == "hello"

        pong = client.recv_json(timeout=2.0)
        assert pong is not None
        assert pong.get("type") == "pong"
    finally:
        try:
            sock.close()
        except Exception:
            pass


def test_e2e_command_timeout_and_cancel_dispatch(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        status = client.connect(token=token)
        assert status == 101
        _ = client.recv_json()  # hello

        result_container = []

        def execute_worker():
            ok, res, err = server.transport.execute_command(
                action="tab.list",
                deadline_ms=500,
            )
            result_container.append((ok, res, err))

        t = threading.Thread(target=execute_worker)
        t.start()

        # Client receives command envelope
        cmd = client.recv_json(timeout=2.0)
        assert cmd is not None
        assert cmd.get("type") == "command"
        cmd_id = cmd.get("id")

        # Intentionally DO NOT respond, wait for gateway timeout
        t.join(timeout=2.0)
        assert len(result_container) == 1
        ok, res, err = result_container[0]
        assert ok is False
        assert err is not None
        assert err.code == "TIMEOUT"

        # Client must receive automatic CANCEL envelope from Gateway
        cancel_env = client.recv_json(timeout=2.0)
        assert cancel_env is not None
        assert cancel_env.get("type") == "cancel"
        assert cancel_env.get("params", {}).get("targetCommandId") == cmd_id
    finally:
        client.close()


def test_e2e_rfc6455_version_enforcement(gateway_server):
    _, port, token = gateway_server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect(("127.0.0.1", port))
        sec_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        req = (
            f"GET /browser/v1/ws?token={token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 12\r\n"  # Invalid version (must be 13)
            "Origin: chrome-extension://abcdefghijklmnopabcdefghijklmnop\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)

        resp = sock.recv(1024)
        assert b"400 Bad Request" in resp
    finally:
        sock.close()


def test_e2e_token_rotation(gateway_server):
    server, port, token1 = gateway_server
    client1 = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client1.connect(token=token1) == 101
        _ = client1.recv_json()
        assert server.is_connected is True

        # Rotate token
        token2 = server.rotate_token()
        assert token2 != token1

        # Previous connection should be terminated
        time.sleep(0.1)

        # Reconnecting with old token fails with 401
        client_old = SimpleWebSocketTestClient("127.0.0.1", port)
        try:
            assert client_old.connect(token=token1) == 401
        finally:
            client_old.close()

        # Connecting with new token succeeds with 101
        client_new = SimpleWebSocketTestClient("127.0.0.1", port)
        try:
            assert client_new.connect(token=token2) == 101
            _ = client_new.recv_json()
            assert server.is_connected is True
        finally:
            client_new.close()
    finally:
        client1.close()


def test_e2e_control_frame_payload_limit(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        _ = client.recv_json()

        # Send Ping control frame (opcode 0x9) with 130 bytes (> 125B limit)
        huge_payload = b"p" * 130
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(huge_payload))
        # 0x89: FIN=1, Opcode=9 (Ping), 0x80 | 126 (size as 2 bytes)
        frame = bytes([0x89, 0x80 | 126]) + len(huge_payload).to_bytes(2, "big") + mask + masked
        client.sock.sendall(frame)

        # Server must reject and close connection due to RFC 6455 violation
        time.sleep(0.1)
        next_data = client.recv_json(timeout=1.0)
        assert next_data is None
        assert server.is_connected is False
    finally:
        client.close()




def test_e2e_ingest_progress_chunks_and_result_over_websocket(gateway_server, monkeypatch, tmp_path):
    import launch
    from browser_bridge.ingest import IngestRPC

    server, port, token = gateway_server
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    stores = []

    def store(body):
        stores.append(body)
        return launch._store_ingest_payload(body)

    rpc = IngestRPC(server.broadcast_or_send, store, launch._update_ingest_progress)
    server.on_message = lambda message: rpc.submit(message) if message.get("type") == "ingest.rpc" else None
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        assert client.recv_json()["type"] == "hello"

        def call(request_id, **params):
            client.send_json({"v": 1, "type": "ingest.rpc", "id": request_id, "params": params})
            reply = client.recv_json(timeout=3)
            assert reply["type"] == "ingest.reply" and reply["id"] == request_id
            return reply

        assert call("progress-1", operation="progress", job="ws-job", progress={"percent": 50})["ok"]
        assert launch._INGEST_PROGRESS["ws-job"]["percent"] == 50
        payload = json.dumps({"job": "ws-job", "name": "ws-result", "rows": [{"text": "Tiếng Việt"}]}, ensure_ascii=False)
        for index, chunk in enumerate([payload[:20], payload[20:]]):
            assert call(f"chunk-{index}", operation="chunk", uploadId="upload-1", index=index, chunk=chunk)["ok"]
        reply = call("complete-1", operation="complete", uploadId="upload-1")
        assert reply["ok"] and reply["result"]["count"] == 1
        assert (tmp_path / "csv" / "ws-result.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        assert call("complete-1", operation="complete", uploadId="upload-1") == reply
        assert len(stores) == 1  # A lost acknowledgement must not save/download twice.
        assert not call("bad-chunk", operation="chunk", uploadId="bad", index=3, chunk="x")["ok"]
    finally:
        client.close()
        rpc.executor.shutdown(wait=True)


# ── Zombie-socket recovery ───────────────────────────────────────────────────
# Chrome keeps an extension WebSocket open after the MV3 worker dies, so the gateway can
# end up owning a connection that never answers again. A silent socket must not block the
# extension's next reconnect (exclusive policy) nor keep reporting "connected" forever.


def _live_gateway(monkeypatch, idle_seconds: float, sweep_seconds: float):
    monkeypatch.setattr(gateway_module, "LIVENESS_IDLE_SECONDS", idle_seconds)
    monkeypatch.setattr(gateway_module, "LIVENESS_SWEEP_SECONDS", sweep_seconds)
    token = secrets.token_urlsafe(32)
    server = BrowserGatewayServer(token=token)
    port = server.start("127.0.0.1", 0)
    return server, port, token


def test_e2e_silent_owner_is_replaced_without_manual_takeover(monkeypatch):
    server, port, token = _live_gateway(monkeypatch, idle_seconds=1.5, sweep_seconds=0.3)
    first = SimpleWebSocketTestClient("127.0.0.1", port)
    duplicate = SimpleWebSocketTestClient("127.0.0.1", port)
    newcomer = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert first.connect(token=token, client_id="client-a") == 101
        assert first.recv_json(timeout=2.0)["type"] == "hello"

        # While the owner is answering, a second client is still a duplicate.
        assert duplicate.connect(token=token, client_id="client-b") == 101
        refusal = duplicate.recv_json(timeout=2.0)
        assert refusal["type"] == "error"
        assert refusal["error"]["code"] == "CLIENT_ALREADY_CONNECTED"

        # ...but once it goes silent past the heartbeat window it is stale, not the owner.
        time.sleep(1.8)
        assert newcomer.connect(token=token, client_id="client-b") == 101
        assert newcomer.recv_json(timeout=2.0)["type"] == "hello"
        assert server.is_connected is True
        assert server.active_connection_info()["clientId"] == "client-b"
    finally:
        first.close()
        duplicate.close()
        newcomer.close()
        server.stop()


def test_e2e_watchdog_drops_silent_connection_and_frees_the_gateway(monkeypatch):
    server, port, token = _live_gateway(monkeypatch, idle_seconds=0.6, sweep_seconds=0.2)
    frozen = SimpleWebSocketTestClient("127.0.0.1", port)
    replacement = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert frozen.connect(token=token, client_id="client-a") == 101
        assert frozen.recv_json(timeout=2.0)["type"] == "hello"
        assert server.is_connected is True

        deadline = time.time() + 6
        while time.time() < deadline and server.is_connected:
            time.sleep(0.1)
        assert server.is_connected is False, "a silent socket must be dropped"

        assert replacement.connect(token=token, client_id="client-b") == 101
        assert replacement.recv_json(timeout=2.0)["type"] == "hello"
        assert server.is_connected is True
    finally:
        frozen.close()
        replacement.close()
        server.stop()


def test_e2e_fragmented_client_message_is_reassembled_not_dropped(gateway_server):
    """Chrome fragments large extension messages; the gateway must reassemble, not disconnect."""
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        assert client.recv_json(timeout=2.0)["type"] == "hello"

        client.send_json(
            {"v": 1, "type": "ping", "id": "frag-ping", "params": {"pad": "x" * 5000}},
            fragment_at=64,
        )
        reply = client.recv_json(timeout=3.0)
        assert reply is not None, "fragmented message must not kill the socket"
        assert reply["type"] == "pong"
        assert server.is_connected is True
    finally:
        client.close()


def test_ingest_finalize_sends_only_metadata_to_store():
    from browser_bridge.ingest import IngestRPC

    replies = []
    stored = []
    replied = threading.Event()

    def send(message):
        replies.append(message)
        replied.set()
        return True

    def store(body):
        stored.append(body)
        return 200, {"ok": True, "count": 3006}

    rpc = IngestRPC(send, store, lambda *_args: None)
    try:
        rpc.submit({
            "v": 1,
            "type": "ingest.rpc",
            "id": "finalize-1",
            "params": {
                "operation": "finalize",
                "job": "job-checkpointed",
                "name": "shopee_1546910319_reviews",
                "source": "https://shopee.vn/product/1/1546910319",
            },
        })
        assert replied.wait(2)
        assert stored == [{
            "job": "job-checkpointed",
            "name": "shopee_1546910319_reviews",
            "source": "https://shopee.vn/product/1/1546910319",
            "rows": [],
        }]
        assert replies[0]["ok"] is True
        assert replies[0]["result"]["count"] == 3006
    finally:
        rpc.executor.shutdown(wait=True)


def test_helper_http_websocket_upgrade_and_reconnect(monkeypatch):
    import launch
    import urllib.request
    from http.server import ThreadingHTTPServer

    gateway = BrowserGatewayServer(token="helper-test-token")
    monkeypatch.setattr(launch, "_BROWSER_WS", gateway)
    helper = ThreadingHTTPServer(("127.0.0.1", 0), launch._HelperHandler)
    thread = threading.Thread(target=helper.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{helper.server_port}/browser/pair")
        with urllib.request.urlopen(request, timeout=3) as response:
            pairing = json.load(response)
        hinted_request = urllib.request.Request(
            f"http://127.0.0.1:{helper.server_port}/browser/pair",
            headers={"X-Bridge-Client": launch.BRIDGE_CLIENT_HEADER},
        )
        with urllib.request.urlopen(hinted_request, timeout=3) as response:
            hinted_pairing = json.load(response)
        assert hinted_pairing["token"] == pairing["token"]
        for _ in range(3):
            client = SimpleWebSocketTestClient("127.0.0.1", helper.server_port)
            try:
                assert client.connect(token=pairing["token"]) == 101
                assert client.recv_json()["type"] == "hello"
                client.send_json({"v": 1, "type": "ping", "id": "heartbeat"})
                assert client.recv_json()["type"] == "pong"
            finally:
                client.close()
    finally:
        gateway.stop()
        helper.shutdown()
        helper.server_close()
        thread.join(timeout=2)


def test_socket_disconnect_releases_pending_command(gateway_server):
    server, port, token = gateway_server
    client = SimpleWebSocketTestClient("127.0.0.1", port)
    assert client.connect(token=token) == 101
    assert client.recv_json()["type"] == "hello"
    results = []
    thread = threading.Thread(target=lambda: results.append(server.transport.execute_command("tab.list", deadline_ms=30000)))
    thread.start()
    assert client.recv_json()["type"] == "command"
    client.close()
    thread.join(timeout=2)
    assert not thread.is_alive(), "Socket loss must release the caller immediately"
    assert not results[0][0]
    assert "disconnected" in results[0][2].message.lower()


def test_e2e_full_7_step_websocket_ingest_flow(gateway_server, monkeypatch, tmp_path):
    """End-to-end verification of all 7 WebSocket ingest steps between server and extension."""
    import launch
    from browser_bridge.ingest import IngestRPC

    server, port, token = gateway_server
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(launch, "_BROWSER_WS", server)

    # Initialize IngestRPC attached to server
    rpc = IngestRPC(
        send=server.broadcast_or_send,
        store=launch._store_ingest_payload,
        progress=launch._update_ingest_progress,
    )
    monkeypatch.setattr(launch, "_INGEST_RPC", rpc)

    # Wire up server callbacks to launch handlers
    server.on_connect = launch._on_browser_ws_connect
    server.on_message = launch._on_browser_ws_message

    # Reset in-memory state
    with launch._INGEST_JOBS_LOCK:
        launch._INGEST_JOBS.clear()
        launch._INGEST_INFLIGHT.clear()
        launch._INGEST_ALL_JOBS.clear()
    with launch._INGEST_PROGRESS_LOCK:
        launch._INGEST_PROGRESS.clear()
    launch._INGEST_RESULTS.clear()

    client = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        assert client.connect(token=token) == 101
        hello = client.recv_json()
        assert hello["type"] == "hello"
        time.sleep(0.1)

        # Step 1: Server tạo job
        test_url = "https://shopee.vn/product/123456/25018847315"
        job = launch._queue_ingest_job(test_url, "shopee-reviews")
        job_id = job["id"]
        assert job_id.startswith("job-")
        assert launch._INGEST_PROGRESS[job_id]["status"] == "queued"

        # Step 2: Server gửi ingest.job qua WebSocket
        msg = client.recv_json(timeout=3)
        assert msg["type"] == "ingest.job"
        assert msg["job"]["id"] == job_id
        assert msg["job"]["url"] == test_url

        # Step 3: Extension nhận job và trả accepted
        client.send_json({"v": 1, "type": "accepted", "id": job_id, "jobId": job_id})
        time.sleep(0.1)

        # Verify Step 3: Server nhận accepted và đưa job vào inflight
        with launch._INGEST_JOBS_LOCK:
            assert job_id not in [j["id"] for j in launch._INGEST_JOBS]
            assert job_id in launch._INGEST_INFLIGHT

        # Step 4: Extension gửi ingest.rpc (progress)
        req_id_progress = "rpc-progress-1"
        client.send_json({
            "v": 1,
            "type": "ingest.rpc",
            "id": req_id_progress,
            "params": {
                "operation": "progress",
                "job": job_id,
                "progress": {"status": "running", "stage": "crawling", "percent": 45, "message": "Đang cào dữ liệu"},
            },
        })

        # Step 5: Server trả ingest.reply đúng correlation id cho progress
        reply_progress = client.recv_json(timeout=3)
        assert reply_progress["type"] == "ingest.reply"
        assert reply_progress["id"] == req_id_progress
        assert reply_progress["ok"] is True
        assert launch._INGEST_PROGRESS[job_id]["percent"] == 45
        assert launch._INGEST_PROGRESS[job_id]["status"] == "running"

        # Step 6: Progress/chunk/complete
        payload_body = {
            "job": job_id,
            "name": "shopee_25018847315_reviews",
            "source": test_url,
            "rows": [
                {
                    "user": "test_buyer",
                    "sao": 5,
                    "noi_dung": "Dép đi rất êm và bền",
                    "thoi_gian": "2026-09-06 20:00:00",
                    "anh": 0,
                    "video": 0,
                }
            ],
        }
        chunk_str = json.dumps(payload_body, ensure_ascii=False)
        req_id_chunk = "rpc-chunk-0"
        upload_id = "upload-test-123"
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
        reply_chunk = client.recv_json(timeout=3)
        assert reply_chunk["type"] == "ingest.reply"
        assert reply_chunk["id"] == req_id_chunk
        assert reply_chunk["ok"] is True
        assert reply_chunk["result"]["index"] == 0

        # Complete operation
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
        reply_complete = client.recv_json(timeout=5)
        assert reply_complete["type"] == "ingest.reply"
        assert reply_complete["id"] == req_id_complete
        assert reply_complete["ok"] is True
        assert reply_complete["result"]["count"] == 1

        # Step 7: Server ghi kết quả cuối
        assert job_id in launch._INGEST_RESULTS
        res = launch._INGEST_RESULTS[job_id]
        assert res["ok"] is True
        assert res["count"] == 1
        assert "shopee_25018847315_reviews.csv" in res["csv"]
        assert (tmp_path / "csv" / "shopee_25018847315_reviews.csv").exists()
        assert launch._INGEST_PROGRESS[job_id]["status"] == "done"
        assert launch._INGEST_PROGRESS[job_id]["percent"] == 100

        # Inflight queue should be cleaned up
        with launch._INGEST_JOBS_LOCK:
            assert job_id not in launch._INGEST_INFLIGHT

        # State persistence file exists
        state_file = tmp_path / ".ingest_jobs_state.json"
        assert state_file.exists()
        state_data = json.loads(state_file.read_text(encoding="utf-8"))
        assert job_id in state_data["results"]
    finally:
        client.close()
        rpc.executor.shutdown(wait=True)
