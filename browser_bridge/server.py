"""Standalone and embeddable RFC 6455 loopback WebSocket Gateway for Chrome Extension."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import socketserver
import struct
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional, Set

from .protocol import (
    BridgeError,
    ErrorCode,
    MAX_MESSAGE_BYTES,
    MessageEnvelope,
    MessageType,
    redact_sensitive_data,
)
from .security import (
    AuditLogger,
    generate_pairing_token,
    validate_extension_origin,
    verify_pairing_token,
)
from .transport import ExtensionState, WebSocketTransport

logger = logging.getLogger("browser_bridge.server")
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketProtocolError(Exception):
    pass


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Peer closed socket")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    head = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        return head + bytes([size]) + payload
    elif size <= 0xFFFF:
        return head + bytes([126]) + struct.pack("!H", size) + payload
    return head + bytes([127]) + struct.pack("!Q", size) + payload


def _read_frame(sock: socket.socket, max_bytes: int = MAX_MESSAGE_BYTES) -> tuple[int, bytes]:
    first, second = _read_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F

    if not fin:
        raise WebSocketProtocolError("Fragmented frames not supported")
    if not masked:
        raise WebSocketProtocolError("Client frames must be masked")

    if size == 126:
        size = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", _read_exact(sock, 8))[0]

    if size > max_bytes:
        raise WebSocketProtocolError(f"Payload size {size} exceeds limit {max_bytes}")

    mask = _read_exact(sock, 4)
    payload = bytearray(_read_exact(sock, size))
    for i in range(size):
        payload[i] ^= mask[i % 4]

    return opcode, bytes(payload)


class GatewayClientConnection:
    """Manages an individual connected extension client socket."""

    def __init__(self, sock: socket.socket, extension_id: str):
        self.sock = sock
        self.extension_id = extension_id
        self.send_lock = threading.Lock()
        self.closed = False

    def send(self, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError(f"Outbound payload {len(raw)} exceeds {MAX_MESSAGE_BYTES}")
        with self.send_lock:
            if self.closed:
                raise ConnectionError("Connection is closed")
            self.sock.sendall(_encode_frame(raw))

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        with self.send_lock:
            if not self.closed:
                self.sock.sendall(_encode_frame(payload[:125], opcode=opcode))

    def close(self) -> None:
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.sock.sendall(_encode_frame(b"", opcode=0x8))
            except OSError:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


class BrowserGatewayServer:
    """Secure loopback-only WebSocket Gateway Server for Chrome Extension communication."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        allowlisted_extension_ids: Optional[Set[str]] = None,
        max_concurrency: int = 10,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
    ):
        self.token = token or generate_pairing_token()
        self.allowlisted_extension_ids = allowlisted_extension_ids
        self.max_concurrency = max_concurrency
        self.on_message = on_message
        self.on_connect = on_connect
        self.audit = AuditLogger()

        self._active_conn: Optional[GatewayClientConnection] = None
        self._lock = threading.RLock()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Bind transport
        self.transport = WebSocketTransport(
            send_fn=self.broadcast_or_send,
            is_connected_fn=lambda: self.is_connected,
        )

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._active_conn is not None and not self._active_conn.closed

    @property
    def port(self) -> Optional[int]:
        server = self._server
        return int(server.server_address[1]) if server else None

    def start(self, host: str = "127.0.0.1", port: int = 8767) -> int:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Browser Gateway MUST only bind to loopback (127.0.0.1, localhost, ::1)")

        if self._server is not None:
            assert self.port is not None
            return self.port

        gw = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                gw.handle_socket(self.request)

        self._server = Server((host, port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="browser-gateway-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("Browser Gateway Server started on %s:%d", host, self.port)
        return self.port  # type: ignore

    def stop(self) -> None:
        server = self._server
        self._server = None
        with self._lock:
            conn = self._active_conn
            self._active_conn = None
        if conn:
            conn.close()
        if server:
            server.shutdown()
            server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self.transport.set_state(ExtensionState.DISCONNECTED)
        logger.info("Browser Gateway Server stopped")

    def broadcast_or_send(self, payload: Dict[str, Any]) -> bool:
        with self._lock:
            conn = self._active_conn
        if not conn or conn.closed:
            return False
        try:
            conn.send(payload)
            return True
        except Exception as exc:
            logger.warning("Failed to send payload to extension: %s", exc)
            self._unregister(conn)
            return False

    def _register(self, conn: GatewayClientConnection) -> None:
        with self._lock:
            prev = self._active_conn
            self._active_conn = conn
        if prev and prev is not conn:
            prev.close()

        self.transport.set_state(ExtensionState.CONNECTED)
        self.audit.record("client.connected", details={"extension_id": conn.extension_id})

        # Send greeting envelope
        hello_env = MessageEnvelope(
            type=MessageType.HELLO,
            params={
                "v": 1,
                "protocolVersion": "1.0",
                "heartbeatMs": 20000,
                "maxPayloadBytes": MAX_MESSAGE_BYTES,
            },
        )
        conn.send(hello_env.to_dict())
        if self.on_connect:
            try:
                self.on_connect()
            except Exception as e:
                logger.warning("on_connect callback raised: %s", e)

    def _unregister(self, conn: GatewayClientConnection) -> None:
        with self._lock:
            if self._active_conn is conn:
                self._active_conn = None
        conn.close()
        self.transport.set_state(ExtensionState.DISCONNECTED)
        self.audit.record("client.disconnected", details={"extension_id": conn.extension_id})

    def upgrade_http_connection(
        self,
        sock: socket.socket,
        path: str,
        query: str,
        headers: Dict[str, str],
    ) -> None:
        """Upgrades an already-parsed HTTP connection on an existing server to WebSocket."""
        supplied_token = urllib.parse.parse_qs(query).get("token", [""])[0]
        origin = headers.get("origin", "")

        valid_origin, ext_id = validate_extension_origin(origin, self.allowlisted_extension_ids)
        if not valid_origin:
            logger.warning("Rejected WebSocket connection with unauthorized origin: %s", origin)
            self.audit.record("auth.rejected", details={"reason": "origin_forbidden", "origin": origin})
            self._reject(sock, 403, "Forbidden: Invalid Extension Origin")
            return

        if not verify_pairing_token(supplied_token, self.token):
            logger.warning("Rejected WebSocket connection with invalid token")
            self.audit.record("auth.rejected", details={"reason": "token_mismatch", "ext_id": ext_id})
            self._reject(sock, 401, "Unauthorized: Invalid Pairing Token")
            return

        sec_key = headers.get("sec-websocket-key", "")
        if not sec_key:
            self._reject(sock, 400, "Bad Request: Missing sec-websocket-key")
            return

        accept = base64.b64encode(hashlib.sha1((sec_key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        sock.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

        conn = GatewayClientConnection(sock, extension_id=ext_id or "unknown")
        self._register(conn)
        self._run_frame_loop(sock, conn)

    def _run_frame_loop(self, sock: socket.socket, conn: GatewayClientConnection) -> None:
        try:
            while True:
                opcode, raw = _read_frame(sock)
                if opcode == 0x8:  # Close
                    break
                elif opcode == 0x9:  # Ping
                    conn.send_control(0xA, raw)
                    continue
                elif opcode == 0xA:  # Pong
                    continue
                elif opcode != 0x1:
                    raise WebSocketProtocolError(f"Unsupported opcode {opcode}")

                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    conn.send(
                        MessageEnvelope(
                            type=MessageType.ERROR,
                            error=BridgeError.create(ErrorCode.INVALID_MESSAGE, "Malformed JSON payload"),
                        ).to_dict()
                    )
                    continue

                if not isinstance(payload, dict):
                    continue

                msg_type = payload.get("type")
                if msg_type == MessageType.PING or msg_type == "bridge.ping":
                    conn.send(MessageEnvelope(type=MessageType.PONG, params={"at": time.time()}).to_dict())
                    continue

                if self.on_message:
                    try:
                        self.on_message(payload)
                    except Exception as e:
                        logger.warning("on_message callback raised: %s", e)

                self.transport.handle_inbound_envelope(payload)
        except (ConnectionError, OSError, WebSocketProtocolError, ValueError) as exc:
            logger.debug("Client socket terminated: %s", exc)
        finally:
            self._unregister(conn)

    def handle_socket(self, sock: socket.socket) -> None:
        conn: Optional[GatewayClientConnection] = None
        try:
            request_line, headers = self._read_http_handshake(sock)
            method, target, _ = request_line.split(" ", 2)
            parsed = urllib.parse.urlsplit(target)
            supplied_token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            origin = headers.get("origin", "")

            # Path check
            if method != "GET" or parsed.path not in ("/browser/v1/ws", "/browser-extension"):
                self._reject(sock, 404, "Not Found")
                return

            # Origin check
            valid_origin, ext_id = validate_extension_origin(origin, self.allowlisted_extension_ids)
            if not valid_origin:
                logger.warning("Rejected WebSocket connection with unauthorized origin: %s", origin)
                self.audit.record("auth.rejected", details={"reason": "origin_forbidden", "origin": origin})
                self._reject(sock, 403, "Forbidden: Invalid Extension Origin")
                return

            # Token check
            if not verify_pairing_token(supplied_token, self.token):
                logger.warning("Rejected WebSocket connection with invalid token")
                self.audit.record("auth.rejected", details={"reason": "token_mismatch", "ext_id": ext_id})
                self._reject(sock, 401, "Unauthorized: Invalid Pairing Token")
                return

            # WebSocket upgrade check
            sec_key = headers.get("sec-websocket-key", "")
            if headers.get("upgrade", "").lower() != "websocket" or not sec_key:
                self._reject(sock, 400, "Bad Request: Missing WebSocket Upgrade Headers")
                return

            # Accept handshake
            accept = base64.b64encode(hashlib.sha1((sec_key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
            sock.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )

            conn = GatewayClientConnection(sock, extension_id=ext_id or "unknown")
            self._register(conn)
            self._run_frame_loop(sock, conn)
        except (ConnectionError, OSError, WebSocketProtocolError, ValueError) as exc:
            logger.debug("Client socket terminated: %s", exc)
        finally:
            if not conn:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _read_http_handshake(sock: socket.socket) -> tuple[str, dict[str, str]]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(2048)
            if not chunk:
                raise ConnectionError("Peer closed during handshake")
            data.extend(chunk)
            if len(data) > 16 * 1024:
                raise WebSocketProtocolError("Handshake headers exceed 16KB")
        head = bytes(data).split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, sep, val = line.partition(":")
            if sep:
                headers[name.strip().lower()] = val.strip()
        return lines[0], headers

    @staticmethod
    def _reject(sock: socket.socket, status: int, reason: str) -> None:
        body = reason.encode("ascii", errors="replace")
        try:
            sock.sendall(
                (
                    f"HTTP/1.1 {status} {reason}\r\n"
                    "Connection: close\r\n"
                    "Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode("ascii")
                + body
            )
        except OSError:
            pass
