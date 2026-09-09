"""Browser Bridge Package: Realtime WebSocket Gateway & Transports for Chrome Extension."""

from .actions import ActionName, ALL_ACTIONS, validate_action_params
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
    is_same_origin,
    validate_extension_origin,
    verify_pairing_token,
)
from .server import BrowserGatewayServer, GatewayClientConnection
from .captcha_detector import (
    is_captcha_detected,
    match_template_grayscale,
    save_evidence_screenshot,
)
from .transport import (
    BrowserTransport,
    ExtensionState,
    HttpPollingTransport,
    TransportManager,
    WebSocketTransport,
)

__all__ = [
    "ActionName",
    "ALL_ACTIONS",
    "validate_action_params",
    "BridgeError",
    "ErrorCode",
    "MAX_MESSAGE_BYTES",
    "MessageEnvelope",
    "MessageType",
    "redact_sensitive_data",
    "AuditLogger",
    "generate_pairing_token",
    "is_same_origin",
    "validate_extension_origin",
    "verify_pairing_token",
    "BrowserGatewayServer",
    "GatewayClientConnection",
    "BrowserTransport",
    "ExtensionState",
    "HttpPollingTransport",
    "TransportManager",
    "WebSocketTransport",
    "is_captcha_detected",
    "match_template_grayscale",
    "save_evidence_screenshot",
]

