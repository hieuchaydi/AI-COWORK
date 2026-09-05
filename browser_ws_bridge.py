"""Small dependency-free WebSocket server for the local Chrome extension.

Backed by browser_bridge package providing RFC 6455 loopback-only communication,
typed Action Registry, Enveloped v1 protocol, and Transport management.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Set

from browser_bridge.actions import ALL_ACTIONS, ActionName, validate_action_params
from browser_bridge.protocol import (
    BridgeError,
    ErrorCode,
    MAX_MESSAGE_BYTES,
    MessageEnvelope,
    MessageType,
    redact_sensitive_data,
)
from browser_bridge.security import (
    generate_pairing_token,
    is_same_origin,
    validate_extension_origin,
    verify_pairing_token,
)
from browser_bridge.server import BrowserGatewayServer, WebSocketProtocolError
from browser_bridge.transport import (
    BrowserTransport,
    ExtensionState,
    HttpPollingTransport,
    TransportManager,
    WebSocketTransport,
)


class BrowserWebSocketBridge(BrowserGatewayServer):
    """Single-extension WebSocket hub with token and Origin validation, backward-compatible with launch.py."""

    def __init__(
        self,
        token: str,
        *,
        on_message: Optional[Callable[[dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        allowlisted_extension_ids: Optional[Set[str]] = None,
    ) -> None:
        if not token:
            raise ValueError("bridge token must not be empty")
        super().__init__(
            token=token,
            allowlisted_extension_ids=allowlisted_extension_ids,
            on_message=on_message,
            on_connect=on_connect,
        )

    @property
    def connected(self) -> bool:
        return self.is_connected

    def send(self, payload: dict[str, Any]) -> bool:
        return self.broadcast_or_send(payload)


__all__ = [
    "BrowserWebSocketBridge",
    "WebSocketProtocolError",
    "BrowserGatewayServer",
    "WebSocketTransport",
    "HttpPollingTransport",
    "TransportManager",
    "ExtensionState",
    "MessageEnvelope",
    "MessageType",
    "BridgeError",
    "ErrorCode",
    "ActionName",
    "ALL_ACTIONS",
    "validate_action_params",
    "generate_pairing_token",
    "verify_pairing_token",
    "validate_extension_origin",
    "is_same_origin",
    "redact_sensitive_data",
]
