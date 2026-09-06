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
    get_allowed_extension_ids,
    mask_token,
    validate_extension_origin,
    verify_pairing_token,
)
from .transport import ExtensionState, WebSocketTransport

logger = logging.getLogger("browser_bridge.server")
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketProtocolError(Exception):
    pass


class _BufferedSocketReader:
    """Socket reader that drains any leftover handshake buffer before reading the wire."""

    def __init__(self, sock: socket.socket, initial_data: bytes = b""):
        self.sock = sock
        self.buf = bytearray(initial_data)

    def read_exact(self, size: int) -> bytes:
        chunks = []
        if self.buf:
            take = min(len(self.buf), size)
            chunks.append(bytes(self.buf[:take]))
            del self.buf[:take]
            size -= take
        while size > 0:
            chunk = self.sock.recv(size)
            if not chunk:
                raise ConnectionError("Peer closed socket")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)


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


def _read_frame(source: socket.socket | _BufferedSocketReader, max_bytes: int = MAX_MESSAGE_BYTES) -> tuple[int, bytes]:
    read_fn = source.read_exact if isinstance(source, _BufferedSocketReader) else lambda sz: _read_exact(source, sz)

    first, second = read_fn(2)
    fin = bool(first & 0x80)
    rsv = (first & 0x70) >> 4
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F

    if rsv != 0:
        raise WebSocketProtocolError("RSV bits must be 0")
    if not fin:
        raise WebSocketProtocolError("Fragmented frames not supported")
    if not masked:
        raise WebSocketProtocolError("Client frames must be masked")
    if opcode in (0x8, 0x9, 0xA) and size > 125:
        raise WebSocketProtocolError("Control frames payload must be 125 bytes or less")

    if size == 126:
        size = struct.unpack("!H", read_fn(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", read_fn(8))[0]

    if size > max_bytes:
        raise WebSocketProtocolError(f"Payload size {size} exceeds limit {max_bytes}")

    mask = read_fn(4)
    payload = bytearray(read_fn(size))
    for i in range(size):
        payload[i] ^= mask[i % 4]

    return opcode, bytes(payload)


class GatewayClientConnection:
    """Manages an individual connected extension client socket."""

    def __init__(self, sock: socket.socket, extension_id: str, client_id: str = "unknown"):
        self.sock = sock
        self.extension_id = extension_id
        self.client_id = client_id or "unknown"
        self.send_lock = threading.Lock()
        self.closed = False
        self.close_sent = False

    def send(self, payload: Dict[str, Any] | MessageEnvelope) -> None:
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError(f"Outbound payload {len(raw)} exceeds {MAX_MESSAGE_BYTES}")
        with self.send_lock:
            if self.closed or self.close_sent:
                raise ConnectionError("Connection is closed")
            self.sock.sendall(_encode_frame(raw))

    def send_control(self, opcode: int, payload: bytes = b"") -> None:
        with self.send_lock:
            if self.closed:
                return
            if opcode == 0x8:
                if self.close_sent:
                    return
                self.close_sent = True
            try:
                self.sock.sendall(_encode_frame(payload[:125], opcode=opcode))
            except OSError:
                pass

    def close(self) -> None:
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
            if not self.close_sent:
                self.close_sent = True
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
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        self.token = token or generate_pairing_token()
        self.allowlisted_extension_ids = (
            allowlisted_extension_ids if allowlisted_extension_ids is not None else get_allowed_extension_ids()
        ) or None
        self.max_concurrency = max_concurrency
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.audit = AuditLogger()

        self._active_conn: Optional[GatewayClientConnection] = None
        self._active_since: Optional[float] = None
        self._lock = threading.RLock()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Bind transport
        self.transport = WebSocketTransport(
            send_fn=self.broadcast_or_send,
            is_connected_fn=lambda: self.is_connected,
            max_concurrency=self.max_concurrency,
        )

    def rotate_token(self) -> str:
        """Rotates the active pairing token and terminates current connection."""
        with self._lock:
            new_token = generate_pairing_token()
            self.token = new_token
            conn = self._active_conn
            self._active_conn = None
            self._active_since = None
            self.transport.reset_pending("Gateway stopped")
        if conn:
            conn.close()
        logger.info("Rotated pairing token to %s", mask_token(new_token))
        return new_token

    def revoke_token(self) -> None:
        """Revokes pairing token and closes any active extension connection."""
        with self._lock:
            self.token = ""
            conn = self._active_conn
            self._active_conn = None
            self._active_since = None
        if conn:
            conn.close()
        logger.info("Revoked pairing token")

    def __enter__(self) -> "BrowserGatewayServer":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._active_conn is not None and not self._active_conn.closed

    @property
    def connected(self) -> bool:
        return self.is_connected

    def send(self, payload: Dict[str, Any] | MessageEnvelope) -> bool:
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        return self.broadcast_or_send(payload)

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
            self._active_since = None
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

    def _connection_info_locked(self, conn: GatewayClientConnection) -> Dict[str, Any]:
        return {
            "extensionId": conn.extension_id,
            "clientId": conn.client_id,
            "connectedAt": self._active_since,
        }

    def active_connection_info(self) -> Optional[Dict[str, Any]]:
        """Return non-sensitive metadata for the single active extension owner."""
        with self._lock:
            conn = self._active_conn
            if not conn or conn.closed:
                return None
            return self._connection_info_locked(conn)

    def _register(
        self,
        conn: GatewayClientConnection,
        *,
        takeover: bool = False,
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        rejected_owner: Optional[Dict[str, Any]] = None
        with self._lock:
            prev = self._active_conn
            if prev and not prev.closed and not takeover:
                rejected_owner = self._connection_info_locked(prev)
            else:
                self._active_conn = conn
                self._active_since = time.time()
                if prev and prev is not conn:
                    self.transport.reset_pending("Extension connection replaced by explicit takeover")
                self.transport.set_state(ExtensionState.CONNECTED)

        if rejected_owner:
            logger.info(
                "Rejected duplicate extension connection client_id=%s; owner=%s",
                conn.client_id,
                rejected_owner.get("clientId"),
            )
            self.audit.record(
                "client.rejected",
                details={
                    "reason": "client_already_connected",
                    "extension_id": conn.extension_id,
                    "client_id": conn.client_id,
                    "owner_client_id": rejected_owner.get("clientId"),
                },
            )
            return False, rejected_owner

        if prev and prev is not conn:
            prev.close()

        self.audit.record(
            "client.connected",
            details={"extension_id": conn.extension_id, "client_id": conn.client_id, "takeover": takeover},
        )

        # Send greeting envelope
        hello_env = MessageEnvelope(
            type=MessageType.HELLO,
            params={
                "v": 1,
                "protocolVersion": "1.0",
                "heartbeatMs": 20000,
                "maxPayloadBytes": MAX_MESSAGE_BYTES,
                "connectionPolicy": "exclusive",
                "clientId": conn.client_id,
            },
        )
        conn.send(hello_env.to_dict())
        if self.on_connect:
            try:
                self.on_connect()
            except Exception as e:
                logger.warning("on_connect callback raised: %s", e)
        return True, None

    def _unregister(self, conn: GatewayClientConnection) -> None:
        was_active = False
        with self._lock:
            if self._active_conn is conn:
                self._active_conn = None
                self._active_since = None
                was_active = True
                self.transport.set_state(ExtensionState.DISCONNECTED)
                self.transport.reset_pending("Extension disconnected")
        conn.close()
        if was_active:
            self.audit.record("client.disconnected", details={"extension_id": conn.extension_id})
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception as e:
                    logger.warning("on_disconnect callback raised: %s", e)

    def _upgrade_and_register(
        self,
        sock: socket.socket,
        sec_key: str,
        extension_id: str,
        client_id: str,
        takeover: bool,
        initial_data: bytes = b"",
    ) -> None:
        accept = base64.b64encode(hashlib.sha1((sec_key + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
        sock.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )

        conn = GatewayClientConnection(sock, extension_id=extension_id, client_id=client_id)
        registered, owner = self._register(conn, takeover=takeover)
        if not registered:
            try:
                conn.send(
                    MessageEnvelope(
                        type=MessageType.ERROR,
                        error=BridgeError.create(
                            ErrorCode.CLIENT_ALREADY_CONNECTED,
                            "Gateway đang được một extension client khác sử dụng",
                            retryable=False,
                            details={"owner": owner, "policy": "exclusive"},
                        ),
                    ).to_dict()
                )
            finally:
                conn.close()
            return

        self._run_frame_loop(sock, conn, initial_data=initial_data)

    def upgrade_http_connection(
        self,
        sock: socket.socket,
        path: str,
        query: str,
        headers: Dict[str, str],
        initial_data: bytes = b"",
    ) -> None:
        """Upgrades an already-parsed HTTP connection on an existing server to WebSocket."""
        sock.settimeout(60.0)
        query_params = urllib.parse.parse_qs(query)
        supplied_token = query_params.get("token", [""])[0]
        client_id = query_params.get("client_id", [""])[0][:128] or "unknown"
        takeover = query_params.get("takeover", ["0"])[0] == "1"
        origin = headers.get("origin", "")

        valid_origin, ext_id = validate_extension_origin(origin, self.allowlisted_extension_ids)
        token_valid = verify_pairing_token(supplied_token, self.token)
        if not valid_origin and (origin or not token_valid):
            logger.warning("Rejected WebSocket connection with unauthorized origin: %s", origin)
            self.audit.record("auth.rejected", details={"reason": "origin_forbidden", "origin": origin})
            self._reject(sock, 403, "Forbidden: Invalid Extension Origin")
            return

        if not token_valid:
            logger.warning("Rejected WebSocket connection with invalid token: %s", mask_token(supplied_token))
            self.audit.record("auth.rejected", details={"reason": "token_mismatch", "ext_id": ext_id})
            self._reject(sock, 401, "Unauthorized: Invalid Pairing Token")
            return

        version = headers.get("sec-websocket-version", "")
        if version != "13":
            self._reject(sock, 400, "Bad Request: Sec-WebSocket-Version 13 Required")
            return

        connection = headers.get("connection", "").lower()
        if "upgrade" not in connection:
            self._reject(sock, 400, "Bad Request: Connection header must contain Upgrade")
            return

        sec_key = headers.get("sec-websocket-key", "")
        if headers.get("upgrade", "").lower() != "websocket" or not sec_key:
            self._reject(sock, 400, "Bad Request: Missing sec-websocket-key or Upgrade websocket")
            return

        self._upgrade_and_register(
            sock,
            sec_key,
            ext_id or "unknown",
            client_id,
            takeover,
            initial_data,
        )

    def _run_frame_loop(
        self,
        sock: socket.socket,
        conn: GatewayClientConnection,
        initial_data: bytes = b"",
    ) -> None:
        reader = _BufferedSocketReader(sock, initial_data)
        try:
            while True:
                opcode, raw = _read_frame(reader)
                if opcode == 0x8:  # Close
                    if not conn.close_sent:
                        try:
                            conn.send_control(0x8, raw[:125])
                        except Exception:
                            pass
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
            sock.settimeout(60.0)
            request_line, headers, leftover = self._read_http_handshake(sock)
            method, target, _ = request_line.split(" ", 2)
            parsed = urllib.parse.urlsplit(target)
            query_params = urllib.parse.parse_qs(parsed.query)
            supplied_token = query_params.get("token", [""])[0]
            client_id = query_params.get("client_id", [""])[0][:128] or "unknown"
            takeover = query_params.get("takeover", ["0"])[0] == "1"
            origin = headers.get("origin", "")

            # Path check
            if method != "GET" or parsed.path not in ("/browser/v1/ws", "/browser-extension"):
                self._reject(sock, 404, "Not Found")
                return

            # Origin check
            valid_origin, ext_id = validate_extension_origin(origin, self.allowlisted_extension_ids)
            token_valid = verify_pairing_token(supplied_token, self.token)
            if not valid_origin and (origin or not token_valid):
                logger.warning("Rejected WebSocket connection with unauthorized origin: %s", origin)
                self.audit.record("auth.rejected", details={"reason": "origin_forbidden", "origin": origin})
                self._reject(sock, 403, "Forbidden: Invalid Extension Origin")
                return

            # Token check
            if not token_valid:
                logger.warning("Rejected WebSocket connection with invalid token: %s", mask_token(supplied_token))
                self.audit.record("auth.rejected", details={"reason": "token_mismatch", "ext_id": ext_id})
                self._reject(sock, 401, "Unauthorized: Invalid Pairing Token")
                return

            # RFC 6455 checks
            version = headers.get("sec-websocket-version", "")
            if version != "13":
                self._reject(sock, 400, "Bad Request: Sec-WebSocket-Version 13 Required")
                return

            connection = headers.get("connection", "").lower()
            if "upgrade" not in connection:
                self._reject(sock, 400, "Bad Request: Connection header must contain Upgrade")
                return

            sec_key = headers.get("sec-websocket-key", "")
            if headers.get("upgrade", "").lower() != "websocket" or not sec_key:
                self._reject(sock, 400, "Bad Request: Missing sec-websocket-key or Upgrade websocket")
                return

            self._upgrade_and_register(
                sock,
                sec_key,
                ext_id or "unknown",
                client_id,
                takeover,
                leftover,
            )
        except (ConnectionError, OSError, WebSocketProtocolError, ValueError) as exc:
            logger.debug("Client socket terminated: %s", exc)
        finally:
            if not conn:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _read_http_handshake(sock: socket.socket) -> tuple[str, dict[str, str], bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(2048)
            if not chunk:
                raise ConnectionError("Peer closed during handshake")
            data.extend(chunk)
            if len(data) > 16 * 1024:
                raise WebSocketProtocolError("Handshake headers exceed 16KB")
        parts = bytes(data).split(b"\r\n\r\n", 1)
        head = parts[0].decode("latin-1")
        leftover = parts[1] if len(parts) > 1 else b""
        lines = head.split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, sep, val = line.partition(":")
            if sep:
                headers[name.strip().lower()] = val.strip()
        return lines[0], headers, leftover

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
