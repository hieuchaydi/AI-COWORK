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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

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
        origin: str = "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    ) -> int:
        self.sock.connect((self.host, self.port))
        query = f"?token={token}" if token else ""
        sec_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")

        req = (
            f"GET {path}{query} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n\r\n"
        )
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

    def send_json(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        mask = secrets.token_bytes(4)
        length = len(data)

        header = bytearray([0x81])
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


def test_e2e_reconnect_does_not_corrupt_active_state(gateway_server):
    server, port, token = gateway_server
    client1 = SimpleWebSocketTestClient("127.0.0.1", port)
    client2 = SimpleWebSocketTestClient("127.0.0.1", port)
    try:
        # Client 1 connects
        s1 = client1.connect(token=token)
        assert s1 == 101
        _ = client1.recv_json()
        assert server.is_connected is True

        # Client 2 connects (replaces client 1 as active connection)
        s2 = client2.connect(token=token)
        assert s2 == 101
        _ = client2.recv_json()
        assert server.is_connected is True

        # Client 1 closes its socket
        client1.close()
        time.sleep(0.2)

        # Server transport state MUST remain CONNECTED because client 2 is active!
        assert server.is_connected is True
        assert server.transport.get_state() == ExtensionState.CONNECTED

        # Can still execute command via client 2
        result_container = []

        def execute_worker():
            ok, res, err = server.transport.execute_command(
                action="tab.list",
                deadline_ms=4000,
            )
            result_container.append((ok, res, err))

        t = threading.Thread(target=execute_worker)
        t.start()

        cmd = client2.recv_json(timeout=3.0)
        assert cmd is not None
        assert cmd.get("action") == "tab.list"

        client2.send_json({
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

