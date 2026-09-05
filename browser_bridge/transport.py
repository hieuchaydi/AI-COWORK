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


class WebSocketTransport(BrowserTransport):
    """Realtime WebSocket transport utilizing enveloped v1 protocol with correlation and timeouts."""

    def __init__(
        self,
        send_fn: Callable[[Dict[str, Any]], bool],
        *,
        is_connected_fn: Callable[[], bool],
    ):
        self._send_fn = send_fn
        self._is_connected_fn = is_connected_fn
        self._pending: Dict[str, _PendingCommand] = {}
        self._idempotency_cache: Dict[str, Tuple[bool, Any, Optional[BridgeError]]] = {}
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
            with self._lock:
                self._idempotency_cache[cmd_id] = (True, env.result, None)
            return

        if env.type == MessageType.ERROR:
            err = env.error or BridgeError.create(ErrorCode.INTERNAL_ERROR, "Unknown error")
            if pending:
                pending.error = err
                pending.event.set()
            with self._lock:
                self._idempotency_cache[cmd_id] = (False, None, err)
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

        if env.type == MessageType.TAB_STATE:
            logger.debug("Tab state update: %s", env.params)
            return

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
        with self._lock:
            if cid in self._idempotency_cache:
                logger.info("Returning cached result for idempotent command %s", cid)
                return self._idempotency_cache[cid]

        # 2. Check connection
        if not self.is_connected():
            return False, None, BridgeError.create(
                ErrorCode.INTERNAL_ERROR,
                "Extension WebSocket is not connected",
                retryable=True,
            )

        # 3. Validate action & parameters
        try:
            validate_action_params(action, pars)
        except Exception as exc:
            return False, None, BridgeError.create(
                ErrorCode.INVALID_MESSAGE,
                f"Invalid parameters for action '{action}': {exc}",
                retryable=False,
            )

        # 4. Create pending command
        timeout_sec = max(1.0, deadline_ms / 1000.0)
        pending = _PendingCommand(cid, action, time.monotonic() + timeout_sec)
        with self._lock:
            self._pending[cid] = pending
            if self._state == ExtensionState.CONNECTED:
                self._state = ExtensionState.BUSY

        # 5. Send command envelope
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

        # 6. Wait for response or timeout
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
            with urllib.request.urlopen(target, timeout=10) as resp:
                return json.loads(resp.read().decode())

        def _def_result(job_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
            target = f"{self.base_url}/ingest/result?id={urllib.parse.quote(job_id)}"
            with urllib.request.urlopen(target, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("result"), data.get("progress")

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

