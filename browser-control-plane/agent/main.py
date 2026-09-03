r"""
bcp-agent — Agent Entry Point & Socket Server.

Supports:
- Windows: Named Pipe  \\.\pipe\bcp-<profileId>
- Linux/macOS: Unix domain socket  $RUNTIME_DIR/bcp/<profileId>.sock
- Fallback TCP: 127.0.0.1 only, loopback

Handshake: first message must be agent.hello (§4.2.5).
Any other method before successful hello → UNAUTHENTICATED + close.
"""
import asyncio
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional

from .framing import parse_frame, encode_json_frame, ResponseEnvelope, ErrorData, EventEnvelope
from .managers.session_manager import SessionManager
from .managers.target_manager import TargetManager
from .managers.frame_manager import FrameManager
from .managers.runtime_manager import RuntimeManager
from .managers.dom_manager import DomManager
from .managers.input_manager import InputManager
from .managers.network_manager import NetworkManager
from .managers.storage_manager import StorageManager
from .backends.cdp.cdp_backend import CdpBackend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"

# Full method catalog (§4.3) — used in hello response
ALL_METHODS = [
    "agent.hello", "agent.ping", "agent.stats", "agent.shutdown",
    "target.list", "target.create", "target.activate", "target.close",
    "page.navigate", "page.reload", "page.goBack", "page.goForward",
    "page.waitForLoadState", "page.content", "page.screenshot",
    "page.setViewport", "page.frames", "page.handleDialog", "page.setDownloadBehavior",
    "runtime.evaluate", "runtime.callFunction", "runtime.releaseHandle", "runtime.addInitScript",
    "dom.query", "dom.queryAll", "dom.waitForSelector", "dom.attributes",
    "dom.text", "dom.html", "dom.boundingBox", "dom.scrollIntoView", "dom.snapshot",
    "input.click", "input.hover", "input.type", "input.press",
    "input.scroll", "input.dragAndDrop", "input.uploadFiles",
    "network.enable", "network.disable", "network.getBody",
    "storage.getCookies", "storage.setCookies", "storage.clearCookies",
    "storage.getLocal", "storage.setLocal", "storage.getSession",
    "storage.exportState", "storage.importState",
]


class WindowsPipeServer:
    """Wraps asyncio PipeServer instances on Windows to provide asyncio.Server compatibility."""

    def __init__(self, servers: list, pipe_name: str):
        self._servers = servers
        self.pipe_name = pipe_name
        self._closed = asyncio.Event()

    def close(self) -> None:
        for s in self._servers:
            try:
                s.close()
            except Exception:
                pass
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def serve_forever(self) -> None:
        try:
            await self._closed.wait()
        except asyncio.CancelledError:
            self.close()
            raise

    def is_serving(self) -> bool:
        return not self._closed.is_set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()
        await self.wait_closed()


class AgentServer:
    def __init__(self, profile_id: str, token_file: str):
        self.profile_id = profile_id
        self.session_manager = SessionManager(token_file)
        self.backend = CdpBackend()

        self.server: Optional[Any] = None
        self.tcp_address: Optional[tuple[str, int]] = None
        self.pipe_name: Optional[str] = None
        self._active_writers: set[asyncio.StreamWriter] = set()

        # Managers
        self.target_manager = TargetManager(self.backend)
        self.frame_manager = FrameManager(self.backend)
        self.runtime_manager = RuntimeManager(self.backend)
        self.dom_manager = DomManager(self.backend)
        self.input_manager = InputManager(self.backend)
        self.network_manager = NetworkManager(self.backend)
        self.storage_manager = StorageManager(self.backend)

        self._stats: Dict[str, int] = {
            "requests_total": 0,
            "errors_total": 0,
            "events_dropped": 0,
        }

    def _error(self, req_id: Optional[int], kind: str, code: int, message: str,
                retryable: bool = False) -> bytes:
        resp = ResponseEnvelope(
            id=req_id or 0,
            ok=False,
            error=ErrorData(kind=kind, code=code, message=message, retryable=retryable),
        )
        return encode_json_frame(resp)

    def _ok(self, req_id: int, result: Dict[str, Any]) -> bytes:
        resp = ResponseEnvelope(id=req_id, ok=True, result=result)
        return encode_json_frame(resp)

    async def _dispatch(self, method: str, params: Dict[str, Any], req_id: int,
                        timeout_ms: int) -> Dict[str, Any]:
        """Dispatch a BCP method to the appropriate manager."""
        # agent.*
        if method == "agent.ping":
            return {"pong": True}
        if method == "agent.stats":
            return {
                **self._stats,
                "open_targets": len(await self.target_manager.list_targets()),
            }
        if method == "agent.shutdown":
            raise SystemExit(0)

        # target.*
        if method == "target.list":
            return {"targets": await self.target_manager.list_targets()}
        if method == "target.create":
            return await self.target_manager.create_target(
                params.get("url", "about:blank"), params.get("incognito", False)
            )
        if method == "target.activate":
            return await self.target_manager.activate_target(params["targetId"])
        if method == "target.close":
            return await self.target_manager.close_target(params["targetId"])

        # page.*
        if method == "page.navigate":
            return await self.frame_manager.navigate(
                params["targetId"], params["url"],
                wait_until=params.get("waitUntil", "load"),
                timeout_ms=timeout_ms,
            )
        if method == "page.reload":
            return await self.frame_manager.reload(params["targetId"], timeout_ms=timeout_ms)
        if method == "page.goBack":
            return await self.frame_manager.go_back(params["targetId"], timeout_ms=timeout_ms)
        if method == "page.goForward":
            return await self.frame_manager.go_forward(params["targetId"], timeout_ms=timeout_ms)
        if method == "page.content":
            return await self.frame_manager.content(
                params["targetId"], frame_id=params.get("frameId")
            )
        if method == "page.screenshot":
            return await self.frame_manager.screenshot(params["targetId"], params)
        if method == "page.setViewport":
            return await self.frame_manager.set_viewport(params["targetId"], params)
        if method == "page.frames":
            return await self.frame_manager.list_frames(params["targetId"])
        if method == "page.waitForLoadState":
            return await self.frame_manager.wait_for_load_state(
                params["targetId"], params.get("state", "load"), timeout_ms=timeout_ms
            )
        if method == "page.handleDialog":
            return await self.frame_manager.handle_dialog(
                params["targetId"], params["action"], params.get("promptText")
            )
        if method == "page.setDownloadBehavior":
            return await self.frame_manager.set_download_behavior(
                params["targetId"], params["mode"], params.get("path")
            )

        # runtime.*
        if method == "runtime.evaluate":
            return await self.runtime_manager.evaluate(
                params["targetId"] if "targetId" in params else None,
                params["expression"],
                frame_id=params.get("frameId"),
                world=params.get("world", "main"),
                await_promise=params.get("awaitPromise", False),
                return_by_value=params.get("returnByValue", True),
                timeout_ms=timeout_ms,
            )
        if method == "runtime.callFunction":
            return await self.runtime_manager.call_function(
                params["handleId"], params["functionDeclaration"], params.get("args", []),
                timeout_ms=timeout_ms,
            )
        if method == "runtime.releaseHandle":
            return await self.runtime_manager.release_handle(params["handleId"])
        if method == "runtime.addInitScript":
            return await self.runtime_manager.add_init_script(
                params["source"], params.get("world", "main")
            )

        # dom.*
        if method == "dom.query":
            return await self.dom_manager.query(
                params.get("targetId"), params["selector"],
                engine=params.get("engine", "css"), frame_id=params.get("frameId")
            )
        if method == "dom.queryAll":
            return await self.dom_manager.query_all(
                params.get("targetId"), params["selector"],
                engine=params.get("engine", "css"), frame_id=params.get("frameId")
            )
        if method == "dom.waitForSelector":
            return await self.dom_manager.wait_for_selector(
                params.get("targetId"), params["selector"],
                state=params.get("state", "visible"), timeout_ms=timeout_ms,
                frame_id=params.get("frameId")
            )
        if method == "dom.attributes":
            return await self.dom_manager.attributes(params["handleId"])
        if method == "dom.text":
            return await self.dom_manager.text(
                params.get("targetId"), params["selector"], frame_id=params.get("frameId")
            )
        if method == "dom.html":
            return await self.dom_manager.html(
                params.get("targetId"), params["selector"], frame_id=params.get("frameId")
            )
        if method == "dom.boundingBox":
            return await self.dom_manager.bounding_box(
                params.get("targetId"), params["selector"], frame_id=params.get("frameId")
            )
        if method == "dom.scrollIntoView":
            return await self.dom_manager.scroll_into_view(
                params.get("targetId"), params["selector"], frame_id=params.get("frameId")
            )
        if method == "dom.snapshot":
            return await self.dom_manager.snapshot(
                params.get("targetId"),
                mode=params.get("mode", "flat"),
                max_nodes=params.get("maxNodes", 10000),
                frame_id=params.get("frameId"),
            )

        # input.*
        if method == "input.click":
            return await self.input_manager.click(
                params.get("targetId"), params.get("handleId"), params.get("selector"),
                button=params.get("button", "left"),
                click_count=params.get("clickCount", 1),
                modifiers=params.get("modifiers", []),
                timeout_ms=timeout_ms,
            )
        if method == "input.hover":
            return await self.input_manager.hover(
                params.get("targetId"), params.get("selector"), timeout_ms=timeout_ms
            )
        if method == "input.type":
            return await self.input_manager.type_text(
                params.get("targetId"), params.get("selector"), params["text"],
                delay_ms=params.get("delayMs", 0), timeout_ms=timeout_ms
            )
        if method == "input.press":
            return await self.input_manager.press(
                params.get("targetId"), params.get("selector"), params["key"],
                modifiers=params.get("modifiers", [])
            )
        if method == "input.scroll":
            return await self.input_manager.scroll(
                params.get("targetId"),
                dx=params.get("dx", 0), dy=params.get("dy", 0),
                point=params.get("point")
            )
        if method == "input.dragAndDrop":
            return await self.input_manager.drag_and_drop(
                params.get("targetId"), params["from"], params["to"]
            )
        if method == "input.uploadFiles":
            return await self.input_manager.upload_files(
                params.get("targetId"), params["handleId"], params["paths"]
            )

        # network.*
        if method == "network.enable":
            return await self.network_manager.enable(
                params.get("targetId"),
                capture_bodies=params.get("captureBodies", "none"),
                max_body_bytes=params.get("maxBodyBytes", 1048576),
            )
        if method == "network.disable":
            return await self.network_manager.disable(params.get("targetId"))
        if method == "network.getBody":
            return await self.network_manager.get_body(params["requestId"])

        # storage.*
        if method == "storage.getCookies":
            return await self.storage_manager.get_cookies(
                params.get("targetId"), urls=params.get("urls")
            )
        if method == "storage.setCookies":
            return await self.storage_manager.set_cookies(
                params.get("targetId"), params["cookies"]
            )
        if method == "storage.clearCookies":
            return await self.storage_manager.clear_cookies(params.get("targetId"))
        if method == "storage.getLocal":
            return await self.storage_manager.get_local(
                params.get("targetId"), frame_id=params.get("frameId")
            )
        if method == "storage.setLocal":
            return await self.storage_manager.set_local(
                params.get("targetId"), params["items"], frame_id=params.get("frameId")
            )
        if method == "storage.getSession":
            return await self.storage_manager.get_session(
                params.get("targetId"), frame_id=params.get("frameId")
            )
        if method == "storage.exportState":
            return await self.storage_manager.export_state(params.get("targetId"))
        if method == "storage.importState":
            return await self.storage_manager.import_state(
                params.get("targetId"), params["state"]
            )

        raise ValueError(f"UNSUPPORTED method: {method}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._active_writers.add(writer)
        buffer = b""
        authenticated = False
        peer = writer.get_extra_info("peername")
        logger.info("Client connected: %s", peer)

        try:
            # Reject non-loopback TCP connections
            if isinstance(peer, tuple) and len(peer) >= 2:
                host = peer[0]
                if isinstance(host, str):
                    try:
                        ip = ipaddress.ip_address(host)
                        if not ip.is_loopback:
                            logger.warning("Rejecting non-loopback connection from %s", host)
                            return
                    except ValueError:
                        logger.warning("Rejecting invalid IP address from peer: %s", host)
                        return

            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer += chunk

                while True:
                    payload, buffer = parse_frame(buffer)
                    if payload is None:
                        break

                    if not isinstance(payload, dict):
                        # Binary frame before authenticated — ignore
                        continue

                    req_id = payload.get("id", 0)
                    method = payload.get("method", "")
                    params = payload.get("params", {})
                    timeout_ms = payload.get("timeoutMs", 30000)
                    self._stats["requests_total"] += 1

                    # ── Handshake ──────────────────────────────────────────
                    if method == "agent.hello":
                        client_token = params.get("token", "")
                        client_version = params.get("protocolVersion", "")
                        if not self.session_manager.authenticate(client_token):
                            writer.write(self._error(
                                req_id, "UNAUTHENTICATED", 4010,
                                "Invalid or missing token"
                            ))
                            await writer.drain()
                            return

                        # Version check — reject major mismatch
                        if client_version and client_version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
                            writer.write(self._error(
                                req_id, "BAD_REQUEST", 4000,
                                f"Protocol major version mismatch: got {client_version}, want {PROTOCOL_VERSION}"
                            ))
                            await writer.drain()
                            return

                        authenticated = True
                        writer.write(self._ok(req_id, {
                            "protocolVersion": PROTOCOL_VERSION,
                            "backend": "cdp",
                            "capabilities": [
                                "network.interception", "input.trusted",
                                "page.download", "dom.snapshot",
                            ],
                            "methods": ALL_METHODS,
                        }))
                        await writer.drain()
                        continue

                    if not authenticated:
                        writer.write(self._error(
                            req_id, "UNAUTHENTICATED", 4010,
                            "Send agent.hello first"
                        ))
                        await writer.drain()
                        return

                    # ── Dispatch ───────────────────────────────────────────
                    try:
                        result = await asyncio.wait_for(
                            self._dispatch(method, params, req_id, timeout_ms),
                            timeout=timeout_ms / 1000.0,
                        )
                        writer.write(self._ok(req_id, result))
                    except asyncio.TimeoutError:
                        self._stats["errors_total"] += 1
                        writer.write(self._error(
                            req_id, "TIMEOUT", 4001,
                            f"{method} exceeded {timeout_ms}ms", retryable=True
                        ))
                    except ValueError as exc:
                        # UNSUPPORTED method
                        self._stats["errors_total"] += 1
                        writer.write(self._error(req_id, "UNSUPPORTED", 4003, str(exc)))
                    except Exception as exc:
                        self._stats["errors_total"] += 1
                        logger.exception("Error handling %s", method)
                        writer.write(self._error(
                            req_id, "INTERNAL", 5000, str(exc), retryable=True
                        ))
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            logger.info("Client disconnected: %s", peer)
            self._active_writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start_listening(self, use_tcp: bool = False, tcp_host: str = "127.0.0.1", tcp_port: int = 0) -> None:
        """Start the agent socket server without blocking."""
        if self.server is not None:
            raise RuntimeError("Server already started")

        if use_tcp:
            try:
                ip = ipaddress.ip_address(tcp_host)
            except ValueError as e:
                raise ValueError(f"Invalid TCP host: {tcp_host}") from e

            if not ip.is_loopback:
                raise ValueError(f"TCP transport must bind to a loopback address, got {tcp_host}")

            logger.warning("WARNING: TCP transport enabled. This is explicitly for dev/test only!")
            self.server = await asyncio.start_server(
                self.handle_client, host=tcp_host, port=tcp_port
            )
            sock = self.server.sockets[0]
            self.tcp_address = sock.getsockname()[:2]
            logger.info("bcp-agent listening on TCP loopback: %s", self.tcp_address)
        elif sys.platform == "win32":
            pipe_name = rf"\\.\pipe\bcp-{self.profile_id}"
            try:
                loop = asyncio.get_running_loop()
                if not hasattr(loop, "start_serving_pipe"):
                    raise RuntimeError(
                        f"Current event loop ({type(loop).__name__}) does not support Windows named pipes"
                    )

                def factory():
                    reader = asyncio.StreamReader(loop=loop)
                    return asyncio.StreamReaderProtocol(
                        reader, client_connected_cb=self.handle_client, loop=loop
                    )

                pipe_servers = await loop.start_serving_pipe(factory, pipe_name)
                self.server = WindowsPipeServer(pipe_servers, pipe_name)
                self.pipe_name = pipe_name
                logger.info("bcp-agent listening on named pipe %s", pipe_name)
            except Exception as exc:
                logger.warning(
                    "Named pipe startup failed (%s): %s. Falling back to TCP loopback (127.0.0.1)",
                    pipe_name,
                    exc,
                )
                self.server = await asyncio.start_server(
                    self.handle_client, host="127.0.0.1", port=0
                )
                sock = self.server.sockets[0]
                self.tcp_address = sock.getsockname()[:2]
                logger.info("bcp-agent listening on fallback TCP loopback: %s", self.tcp_address)
        else:
            runtime_dir = os.environ.get("RUNTIME_DIR", "/tmp")
            sock_dir = Path(runtime_dir) / "bcp"
            sock_dir.mkdir(parents=True, exist_ok=True)
            sock_path = sock_dir / f"{self.profile_id}.sock"
            if sock_path.exists():
                sock_path.unlink()
            self.server = await asyncio.start_unix_server(
                self.handle_client, path=str(sock_path)
            )
            sock_path.chmod(0o600)
            logger.info("bcp-agent listening on unix socket %s", sock_path)

    async def serve_forever(self):
        """Block until the server is closed."""
        server = self.server
        if server is None:
            raise RuntimeError("Server not started. Call start_listening first.")
        async with server:
            await server.serve_forever()

    async def close(self):
        """Close the server and wait for it to be fully closed."""
        server = self.server
        self.server = None
        self.tcp_address = None
        self.pipe_name = None

        if server is not None:
            server.close()

        writers = list(self._active_writers)
        for w in writers:
            w.close()

        tasks = []
        if server is not None:
            tasks.append(server.wait_closed())
        for w in writers:
            tasks.append(w.wait_closed())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._active_writers.clear()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="bcp-agent — Browser Control Plane agent process")
    parser.add_argument("--profile-id", required=True, help="Browser profile ID")
    parser.add_argument("--token-file", required=True, help="Path to authentication token file")
    parser.add_argument("--use-tcp", action="store_true", help="Use TCP transport (dev/test only)")
    parser.add_argument("--tcp-host", default="127.0.0.1", help="TCP loopback host to bind (default 127.0.0.1)")
    parser.add_argument("--tcp-port", type=int, default=0, help="TCP port to listen on (0 for random)")
    args = parser.parse_args()

    server = AgentServer(args.profile_id, args.token_file)

    async def main_run():
        try:
            await server.start_listening(use_tcp=args.use_tcp, tcp_host=args.tcp_host, tcp_port=args.tcp_port)
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await server.close()

    try:
        asyncio.run(main_run())
    except KeyboardInterrupt:
        pass
