"""
Wire Protocol Framing & Payload definitions.

Handles encoding/decoding of framing: `[uint32 BE length][uint8 kind][payload]`
kind = 0: UTF-8 JSON payload (request/response/event)
kind = 1: `[16-byte attachmentId][raw bytes]` (screenshots, response bodies)
"""
import struct
import json
from typing import Any, Dict, Optional, Tuple, Union
from pydantic import BaseModel, Field

class RequestEnvelope(BaseModel):
    id: int
    method: str
    params: Dict[str, Any] = Field(default_factory=dict)
    timeoutMs: Optional[int] = None

class ErrorData(BaseModel):
    kind: str
    code: int
    message: str
    retryable: bool = False
    data: Optional[Dict[str, Any]] = None

class ResponseEnvelope(BaseModel):
    id: int
    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[ErrorData] = None

class EventEnvelope(BaseModel):
    event: str
    seq: int
    params: Dict[str, Any] = Field(default_factory=dict)

def encode_json_frame(payload: Union[RequestEnvelope, ResponseEnvelope, EventEnvelope]) -> bytes:
    """Encode a JSON payload (kind = 0)."""
    json_bytes = json.dumps(payload.model_dump(exclude_none=True)).encode("utf-8")
    length = len(json_bytes) + 1 # +1 for kind byte
    return struct.pack(">I", length) + b"\x00" + json_bytes

def encode_binary_frame(attachment_id: bytes, raw_bytes: bytes) -> bytes:
    """Encode a binary payload (kind = 1). attachment_id must be 16 bytes."""
    if len(attachment_id) != 16:
        raise ValueError("attachment_id must be exactly 16 bytes")
    length = 1 + 16 + len(raw_bytes)
    return struct.pack(">I", length) + b"\x01" + attachment_id + raw_bytes

def parse_frame(buffer: bytes) -> Tuple[Optional[Union[Dict[str, Any], Tuple[bytes, bytes]]], bytes]:
    """Parse a frame from the buffer. Returns (payload, remaining_buffer)."""
    if len(buffer) < 4:
        return None, buffer
    
    length = struct.unpack(">I", buffer[:4])[0]
    if len(buffer) < 4 + length:
        return None, buffer
        
    kind = buffer[4]
    payload_bytes = buffer[5:4+length]
    remaining = buffer[4+length:]
    
    if kind == 0:
        return json.loads(payload_bytes.decode("utf-8")), remaining
    elif kind == 1:
        attachment_id = payload_bytes[:16]
        raw_bytes = payload_bytes[16:]
        return (attachment_id, raw_bytes), remaining
    else:
        raise ValueError(f"Unknown frame kind: {kind}")
