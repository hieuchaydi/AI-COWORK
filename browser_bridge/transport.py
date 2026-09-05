"""Transport Abstraction Layer: WebSocketTransport, HttpPollingTransport, and TransportManager."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .actions import validate_action_params
from .protocol import (
    BridgeError,
    ErrorCode,
    MessageEnvelope,
    MessageType,
    redact_sensitive_data,
)

logger = logging.getLogger("browser_bridge.transport")


class ExtensionState:
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    BUSY = "busy"
    AWAITING_USER_VERIFICATION = "awaiting_user_verification"
    PERMISSION_REQUIRED = "permission_required"


class BrowserTransport(ABC):
    """Abstract interface for communicating with the browser extension."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Returns True if the transport is actively connected and ready for commands."""
        pass

    @abstractmethod
    def get_state(self) -> str:
        """Returns the current ExtensionState string."""
        pass

    @abstractmethod
    def execute_command(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        deadline_ms: int = 30000,
        command_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, Any, Optional[BridgeError]]:
        """Synchronously or coroutine-compatible execution of a typed action."""
        pass

    @abstractmethod
    def cancel_command(self, command_id: str) -> bool:
        """Attempts to cancel an in-flight command."""
        pass


class _PendingCommand:
    def __init__(self, command_id: str, action: str, deadline: float):
        self.command_id = command_id
        self.action = action
        self.deadline = deadline
        self.event = threading.Event()
        self.accepted = False
        self.result: Optional[Any] = None
        self.error: Optional[BridgeError] = None
        self.progress: Optional[Dict[str, Any]] = None


class _IdempotencyCache:
    """Bounded, TTL-based idempotency cache to prevent unbounded memory growth."""

    def __init__(self, capacity: int = 500, ttl_sec: float = 300.0):
        self.capacity = capacity
        self.ttl_sec = ttl_sec
        self.cache: Dict[str, Tuple[float, Tuple[bool, Any, Optional[BridgeError]]]] = {}
        self.lock = threading.Lock()

    def get(self, command_id: str) -> Optional[Tuple[bool, Any, Optional[BridgeError]]]:
        now = time.monotonic()
        with self.lock:
            if command_id in self.cache:
                ts, val = self.cache[command_id]
                if now - ts <= self.ttl_sec:
                    return val
                del self.cache[command_id]
            return None

    def set(self, command_id: str, val: Tuple[bool, Any, Optional[BridgeError]]) -> None:
        now = time.monotonic()
        with self.lock:
            if len(self.cache) >= self.capacity:
                expired = [k for k, (ts, _) in self.cache.items() if now - ts > self.ttl_sec]
                for k in expired:
                    del self.cache[k]
                while len(self.cache) >= self.capacity:
                    oldest_k = next(iter(self.cache))
                    del self.cache[oldest_k]
            self.cache[command_id] = (now, val)

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()


class WebSocketTransport(BrowserTransport):
    """Realtime WebSocket transport utilizing enveloped v1 protocol with correlation and timeouts."""

    def __init__(
        self,
        send_fn: Callable[[Dict[str, Any]], bool],
        *,
        is_connected_fn: Callable[[], bool],
        max_concurrency: int = 10,
    ):
        self._send_fn = send_fn
        self._is_connected_fn = is_connected_fn
        self._max_concurrency = max(1, max_concurrency)
        self._semaphore = threading.BoundedSemaphore(self._max_concurrency)
        self._pending: Dict[str, _PendingCommand] = {}
        self._idempotency_cache = _IdempotencyCache(capacity=500, ttl_sec=300.0)
        self._lock = threading.RLock()
        self._state = ExtensionState.DISCONNECTED
        self._verification_info: Optional[Dict[str, Any]] = None

    def set_state(self, state: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._state = state
            if state == ExtensionState.AWAITING_USER_VERIFICATION:
                self._verification_info = details
            elif state in (ExtensionState.CONNECTED, ExtensionState.DISCONNECTED):
                self._verification_info = None

    def get_state(self) -> str:
        with self._lock:
            if not self.is_connected():
                return ExtensionState.DISCONNECTED
            return self._state

    def is_connected(self) -> bool:
        return self._is_connected_fn()

    def get_verification_info(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._verification_info) if self._verification_info else None

    def resume_verification(self) -> None:
        with self._lock:
            if self._state == ExtensionState.AWAITING_USER_VERIFICATION:
                self._state = ExtensionState.CONNECTED
                self._verification_info = None

    def handle_inbound_envelope(self, envelope_data: Dict[str, Any]) -> None:
        """Processes an incoming message envelope from the extension."""
        try:
            env = MessageEnvelope.model_validate(envelope_data)
        except Exception as exc:
            logger.warning("Dropped malformed message envelope: %s", exc)
            return

        cmd_id = env.id
        with self._lock:
            pending = self._pending.get(cmd_id)

        if env.type == MessageType.ACCEPTED and pending:
            pending.accepted = True
            return

        if env.type == MessageType.PROGRESS and pending:
            pending.progress = env.progress
            return

        if env.type == MessageType.RESULT:
            if pending:
                pending.result = env.result
                pending.event.set()
                self._idempotency_cache.set(cmd_id, (True, env.result, None))
            return

        if env.type == MessageType.ERROR:
            err = env.error or BridgeError.create(ErrorCode.INTERNAL_ERROR, "Unknown error")
            if pending:
                pending.error = err
                pending.event.set()
                self._idempotency_cache.set(cmd_id, (False, None, err))
            return

        if env.type == MessageType.VERIFICATION_REQUIRED:
            self.set_state(ExtensionState.AWAITING_USER_VERIFICATION, env.params or env.progress)
            err = BridgeError.create(
                ErrorCode.VERIFICATION_REQUIRED,
                "Human verification required on page. Workflow paused.",
                retryable=True,
                details=env.params,
            )
            if pending:
                pending.error = err
                pending.event.set()
            return

        if env.type == MessageType.VERIFICATION_RESOLVED:
            self.resume_verification()
            return

        if env.type == MessageType.TAB_STATE:
            logger.debug("Tab state update: %s", env.params)
            return

    def reset_pending(self, reason: str = "Connection reset") -> None:
        """Aborts all currently in-flight pending commands (e.g. upon socket drop)."""
        with self._lock:
            pending_cmds = list(self._pending.values())
            self._pending.clear()
        for p in pending_cmds:
            p.error = BridgeError.create(ErrorCode.INTERNAL_ERROR, reason, retryable=True)
            p.event.set()

    def execute_command(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        deadline_ms: int = 30000,
        command_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, Any, Optional[BridgeError]]:
        cid = command_id or str(uuid.uuid4())
        pars = params or {}

        # 1. Check idempotency cache
        cached = self._idempotency_cache.get(cid)
        if cached is not None:
            logger.info("Returning cached result for idempotent command %s", cid)
            return cached

        # 2. Check connection
        if not self.is_connected():
            return False, None, BridgeError.create(
                ErrorCode.INTERNAL_ERROR,
                "Extension WebSocket is not connected",
                retryable=True,
            )

        # 3. Check verification pause state
        if self.get_state() == ExtensionState.AWAITING_USER_VERIFICATION:
            if action not in ("browser.health", "tab.list", "tab.getActive"):
                return False, None, BridgeError.create(
                    ErrorCode.VERIFICATION_REQUIRED,
                    "Human verification required on page. Workflow paused.",
                    retryable=True,
                    details=self.get_verification_info(),
                )

        # 4. Validate action & parameters
        try:
            validate_action_params(action, pars)
        except Exception as exc:
            return False, None, BridgeError.create(
                ErrorCode.INVALID_MESSAGE,
                f"Invalid parameters for action '{action}': {exc}",
                retryable=False,
            )

        # 5. Acquire concurrency semaphore (Backpressure)
        acquired = self._semaphore.acquire(blocking=True, timeout=2.0)
        if not acquired:
            return False, None, BridgeError.create(
                ErrorCode.INTERNAL_ERROR,
                f"Gateway concurrency limit ({self._max_concurrency}) reached",
                retryable=True,
            )

        try:
            # 6. Create pending command
            timeout_sec = max(1.0, deadline_ms / 1000.0)
            pending = _PendingCommand(cid, action, time.monotonic() + timeout_sec)
            with self._lock:
                self._pending[cid] = pending
                if self._state == ExtensionState.CONNECTED:
                    self._state = ExtensionState.BUSY

            # 7. Send command envelope
            env = MessageEnvelope(
                type=MessageType.COMMAND,
                id=cid,
                sessionId=session_id,
                deadlineMs=deadline_ms,
                action=action,
                params=pars,
            )

            sent = self._send_fn(env.to_dict())
            if not sent:
                with self._lock:
                    self._pending.pop(cid, None)
                return False, None, BridgeError.create(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to transmit command to extension socket",
                    retryable=True,
                )

            # 8. Wait for response or timeout
            finished = pending.event.wait(timeout=timeout_sec)

            with self._lock:
                self._pending.pop(cid, None)
                if self._state == ExtensionState.BUSY:
                    self._state = ExtensionState.CONNECTED

            if not finished:
                err = BridgeError.create(
                    ErrorCode.TIMEOUT,
                    f"Command '{action}' timed out after {timeout_sec:.1f}s",
                    retryable=True,
                )
                # Try to cancel
                self.cancel_command(cid)
                return False, None, err

            if pending.error:
                return False, None, pending.error

            return True, pending.result, None
        finally:
            self._semaphore.release()

    def cancel_command(self, command_id: str) -> bool:
        env = MessageEnvelope(
            type=MessageType.CANCEL,
            id=str(uuid.uuid4()),
            params={"targetCommandId": command_id},
        )
        return self._send_fn(env.to_dict())


class HttpPollingTransport(BrowserTransport):
    """Fallback transport utilizing HTTP polling compatibility queue."""

    def __init__(
        self,
        queue_fn: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        get_result_fn: Optional[Callable[[str], Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]] = None,
        *,
        base_url: Optional[str] = None,
    ):
        import urllib.request
        import urllib.parse
        import json

        self.base_url = (base_url or "http://127.0.0.1:8766").rstrip("/")

        def _def_queue(url: str, kind: str) -> Dict[str, Any]:
            target = f"{self.base_url}/ingest/job?url={urllib.parse.quote(url)}&kind={urllib.parse.quote(kind)}"
            try:
                with urllib.request.urlopen(target, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except Exception as exc:
                return {"ok": False, "error": f"HTTP queue request failed: {exc}"}

        def _def_result(job_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
            target = f"{self.base_url}/ingest/result?id={urllib.parse.quote(job_id)}"
            try:
                with urllib.request.urlopen(target, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    return data.get("result"), data.get("progress")
            except Exception:
                return None, None

        self._queue_fn = queue_fn or _def_queue
        self._get_result_fn = get_result_fn or _def_result

    def is_connected(self) -> bool:
        # HTTP polling is always structurally available as a fallback
        return True

    def get_state(self) -> str:
        return ExtensionState.CONNECTED

    def execute_command(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        deadline_ms: int = 30000,
        command_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, Any, Optional[BridgeError]]:
        pars = params or {}
        url = pars.get("url") or pars.get("pathOrUrl") or ""
        if not url:
            return False, None, BridgeError.create(
                ErrorCode.INVALID_MESSAGE,
                "HTTP polling transport requires 'url' in params",
                retryable=False,
            )

        job = self._queue_fn(url, action)
        job_id = job.get("id", "")
        if not job_id:
            return False, None, BridgeError.create(
                ErrorCode.INTERNAL_ERROR,
                job.get("error") or "Failed to queue job: missing job ID in response",
                retryable=True,
            )
        deadline = time.monotonic() + (deadline_ms / 1000.0)

        while time.monotonic() < deadline:
            result, progress = self._get_result_fn(job_id)
            if result:
                if result.get("ok") is False:
                    return False, None, BridgeError.create(
                        ErrorCode.INTERNAL_ERROR,
                        result.get("error", "Job failed"),
                        retryable=True,
                    )
                return True, result, None
            time.sleep(0.5)

        return False, None, BridgeError.create(
            ErrorCode.TIMEOUT,
            f"HTTP polling job {job_id} timed out",
            retryable=True,
        )

    def cancel_command(self, command_id: str) -> bool:
        # HTTP polling queue cancellation is best-effort
        return False


class TransportManager:
    """Manages active transports, prioritising WebSocket and automatically falling back to HTTP."""

    def __init__(
        self,
        ws_transport: WebSocketTransport,
        http_transport: Optional[HttpPollingTransport] = None,
    ):
        self.ws = ws_transport
        self.http = http_transport

    @property
    def active_transport_name(self) -> str:
        if self.ws.is_connected():
            return "websocket"
        return "http-polling"

    def get_state(self) -> str:
        return self.ws.get_state()

    def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        deadline_ms: int = 30000,
        command_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, Any, Optional[BridgeError]]:
        # 1. Prefer WebSocket when extension is online
        if self.ws.is_connected():
            return self.ws.execute_command(
                action,
                params,
                deadline_ms=deadline_ms,
                command_id=command_id,
                session_id=session_id,
            )

        # 2. Fallback to HTTP polling if available
        if self.http and self.http.is_connected():
            logger.info("WebSocket unavailable. Falling back to HTTP polling for action '%s'", action)
            return self.http.execute_command(
                action,
                params,
                deadline_ms=deadline_ms,
                command_id=command_id,
                session_id=session_id,
            )

        return False, None, BridgeError.create(
            ErrorCode.INTERNAL_ERROR,
            "No browser transport available (WebSocket disconnected and HTTP fallback disabled)",
            retryable=True,
        )

