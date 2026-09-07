"""Browser Bridge Protocol v1: Envelopes, Message Types, Errors, and Serialization.

Defines the bidirectional JSON-RPC/message contract between Codex/Gateway
and Manifest V3 Chrome Extension.
"""

from __future__ import annotations

import json
import re
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


class MessageType(str, Enum):
    HELLO = "hello"
    AUTHENTICATE = "authenticate"
    COMMAND = "command"
    ACCEPTED = "accepted"
    PROGRESS = "progress"
    RESULT = "result"
    ERROR = "error"
    CANCEL = "cancel"
    PING = "ping"
    PONG = "pong"
    TAB_STATE = "tab.state"
    VERIFICATION_REQUIRED = "verification.required"
    VERIFICATION_RESOLVED = "verification.resolved"


class ErrorCode(str, Enum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    ORIGIN_FORBIDDEN = "ORIGIN_FORBIDDEN"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    SAME_ORIGIN_VIOLATION = "SAME_ORIGIN_VIOLATION"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    CLIENT_ALREADY_CONNECTED = "CLIENT_ALREADY_CONNECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BridgeError(BaseModel):
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error explanation")
    retryable: bool = Field(default=False, description="Whether the caller may retry")
    details: Dict[str, Any] = Field(default_factory=dict, description="Diagnostic payload")

    @classmethod
    def create(
        cls,
        code: Union[ErrorCode, str],
        message: str,
        *,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> "BridgeError":
        return cls(
            code=code.value if isinstance(code, ErrorCode) else str(code),
            message=message,
            retryable=retryable,
            details=details or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class MessageEnvelope(BaseModel):
    v: int = Field(default=1, description="Protocol version (must be 1)")
    type: str = Field(..., description="Message type from MessageType enum")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique command or message correlation ID")
    sessionId: Optional[str] = Field(default=None, description="Correlated session or tab identifier")
    deadlineMs: Optional[int] = Field(default=30000, ge=0, description="Execution deadline in milliseconds")
    action: Optional[str] = Field(default=None, description="Action name for command messages")
    params: Dict[str, Any] = Field(default_factory=dict, description="Parameters payload")
    result: Optional[Any] = Field(default=None, description="Result payload for result messages")
    error: Optional[BridgeError] = Field(default=None, description="Error structure for error messages")
    progress: Optional[Dict[str, Any]] = Field(default=None, description="Progress payload")
    auth: Optional[Dict[str, Any]] = Field(default=None, description="Authentication token payload")

    @field_validator("v")
    @classmethod
    def validate_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"Unsupported protocol version {v}, expected 1")
        return v

    def __init__(self, **data: Any) -> None:
        try:
            raw_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            if len(raw_bytes) > MAX_MESSAGE_BYTES:
                raise ValueError(f"Message payload exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes")
        except (TypeError, OverflowError):
            pass
        super().__init__(**data)

    @model_validator(mode="after")
    def validate_total_size(self) -> "MessageEnvelope":
        raw = json.dumps(self.model_dump(exclude_none=True), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError(f"Message payload exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes")
        return self

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageEnvelope":
        raw_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(raw_bytes) > MAX_MESSAGE_BYTES:
            raise ValueError(f"Message payload exceeds maximum allowed size of {MAX_MESSAGE_BYTES} bytes")
        return cls.model_validate(data)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# Maximum size limit: 8 MiB
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

_SENSITIVE_KEYS_RE = re.compile(
    r"(token|auth|cookie|password|secret|key|bearer)",
    re.IGNORECASE,
)


def redact_sensitive_data(val: Any) -> Any:
    """Recursively scrub sensitive keys and tokens from dict/list structures for safe logging."""
    if isinstance(val, dict):
        redacted = {}
        for k, v in val.items():
            if _SENSITIVE_KEYS_RE.search(str(k)):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive_data(v)
        return redacted
    elif isinstance(val, list):
        return [redact_sensitive_data(item) for item in val]
    elif isinstance(val, str) and len(val) > 128:
        # Avoid logging massive base64 or payload strings verbatim
        if "data:image" in val or "base64," in val:
            return f"{val[:32]}...[DATA_TRUNCATED_{len(val)}B]"
    return val
