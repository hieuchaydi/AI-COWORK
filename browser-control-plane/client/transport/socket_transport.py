"""
Client SocketTransport implementation.
Supports Windows Named Pipes, Unix domain sockets, and TCP loopback.
Implements IBrowserTransport.
"""
import asyncio
import json
import logging
from pathlib import Path
import struct
import sys
from typing import Any, Callable, Dict, List, Optional, Set

from .itransport import IBrowserTransport, BcpError

logger = logging.getLogger(__name__)


class SocketTransport(IBrowserTransport):
    def __init__(self):
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.next_id = 1
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self.listeners: Dict[str, List[Callable[[dict], Any]]] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._capabilities: Set[str] = set()

    def _start_receive_loop(self) -> None:
        if self._receive_task is None or self._receive_task.done():
            self._receive_task = asyncio.create_task(self.receive_loop())

    async def connect_pipe(self, pipe_name: str) -> None:
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            if not hasattr(loop, "create_pipe_connection"):
                raise RuntimeError(
                    f"Current event loop ({type(loop).__name__}) does not support Windows named pipes"
                )
            reader = asyncio.StreamReader(loop=loop)
            protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
            transport, _ = await loop.create_pipe_connection(lambda: protocol, pipe_name)
            writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            self.reader = reader
            self.writer = writer
            self._start_receive_loop()
        else:
            raise NotImplementedError("Named pipes only supported on Windows in this implementation")

    async def connect_tcp(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)
        self._start_receive_loop()

    async def connect_unix(self, path: str) -> None:
        if hasattr(asyncio, "open_unix_connection"):
            self.reader, self.writer = await asyncio.open_unix_connection(path)
            self._start_receive_loop()
        else:
            raise NotImplementedError("Unix domain sockets not supported on this platform")

    async def connect(self, endpoint: str = "", token: str = "") -> dict:
        """Connect to endpoint (pipe, tcp, or unix socket) and complete agent.hello handshake."""
        if not endpoint:
            if sys.platform == "win32":
                endpoint = r"\\.\pipe\bcp-default"
            else:
                endpoint = "/tmp/bcp/default.sock"

        if endpoint.startswith(r"\\.\pipe"):
            await self.connect_pipe(endpoint)
        elif endpoint.startswith("tcp://"):
            addr = endpoint[6:]
            if ":" in addr:
                host, port_str = addr.split(":", 1)
                port = int(port_str)
            else:
                host, port = addr, 8765
            await self.connect_tcp(host, port)
        elif ":" in endpoint and not Path(endpoint).exists():
            host, port_str = endpoint.split(":", 1)
            await self.connect_tcp(host, int(port_str))
        elif sys.platform == "win32":
            if not endpoint.startswith(r"\\.\pipe"):
                endpoint = rf"\\.\pipe\{endpoint}"
            await self.connect_pipe(endpoint)
        else:
            await self.connect_unix(endpoint)

        hello_res = await self.send_hello(token)
        self._capabilities = set(hello_res.get("capabilities", []))
        return hello_res

    @property
    def capabilities(self) -> Set[str]:
        return self._capabilities

    async def send_hello(self, token: str) -> Dict[str, Any]:
        return await self.send_request("agent.hello", {"token": token, "protocolVersion": "1.0"})

    async def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.writer is None or self.writer.is_closing():
            raise BcpError("Transport is not connected", kind="ConnectionError")

        if params is None:
            params = {}

        req_id = self.next_id
        self.next_id += 1

        payload = {
            "id": req_id,
            "method": method,
            "params": params,
        }

        json_bytes = json.dumps(payload).encode("utf-8")
        length = len(json_bytes) + 1
        frame = struct.pack(">I", length) + b"\x00" + json_bytes

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_requests[req_id] = future

        self.writer.write(frame)
        await self.writer.drain()

        return await future

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout_ms: int = 30000) -> Dict[str, Any]:
        """Call a BCP method with a timeout."""
        timeout_sec = max(0.1, timeout_ms / 1000.0)
        return await asyncio.wait_for(self.send_request(method, params), timeout=timeout_sec)

    async def receive_loop(self) -> None:
        buffer = b""
        try:
            while self.reader is not None:
                data = await self.reader.read(4096)
                if not data:
                    break
                buffer += data

                while True:
                    if len(buffer) < 4:
                        break
                    length = struct.unpack(">I", buffer[:4])[0]
                    if len(buffer) < 4 + length:
                        break

                    kind = buffer[4]
                    payload_bytes = buffer[5 : 4 + length]
                    buffer = buffer[4 + length :]

                    if kind == 0:
                        msg = json.loads(payload_bytes.decode("utf-8"))
                        req_id = msg.get("id")
                        if req_id in self.pending_requests:
                            future = self.pending_requests.pop(req_id)
                            if not future.done():
                                if msg.get("ok"):
                                    future.set_result(msg.get("result", {}))
                                else:
                                    err = msg.get("error", {})
                                    if isinstance(err, dict):
                                        future.set_exception(
                                            BcpError(
                                                message=err.get("message", "Error"),
                                                kind=err.get("kind", "UnknownError"),
                                                code=err.get("code", 500),
                                                retryable=err.get("retryable", False),
                                                data=err.get("data"),
                                            )
                                        )
                                    else:
                                        future.set_exception(BcpError(str(err)))
                        elif "event" in msg:
                            event = msg["event"]
                            if event in self.listeners:
                                for handler in list(self.listeners[event]):
                                    try:
                                        handler(msg.get("params", {}))
                                    except Exception as exc:
                                        logger.exception("Error in listener for event %s: %s", event, exc)
                    elif kind == 1:
                        logger.info("Received binary frame (%d bytes)", len(payload_bytes))
        except (asyncio.CancelledError, ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            for req_id, fut in list(self.pending_requests.items()):
                if not fut.done():
                    fut.set_exception(BcpError("Transport disconnected", kind="ConnectionClosed"))
            self.pending_requests.clear()

    def on(self, event: str, handler: Callable[[dict], Any]) -> Callable[[], None]:
        if event not in self.listeners:
            self.listeners[event] = []
        self.listeners[event].append(handler)

        def unsubscribe():
            if event in self.listeners and handler in self.listeners[event]:
                self.listeners[event].remove(handler)

        return unsubscribe

    async def close(self) -> None:
        """Cleanly close transport and background tasks."""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._receive_task = None

        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None

        for fut in list(self.pending_requests.values()):
            if not fut.done():
                fut.set_exception(BcpError("Transport closed", kind="ConnectionClosed"))
        self.pending_requests.clear()
