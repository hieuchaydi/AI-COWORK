"""Security, Origin validation, Pairing tokens, Same-Origin checks, and Audit Logging."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from .protocol import redact_sensitive_data

logger = logging.getLogger("browser_bridge.security")
audit_logger = logging.getLogger("browser_bridge.audit")


def generate_pairing_token() -> str:
    """Generates a secure, cryptographically random pairing token with at least 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def verify_pairing_token(supplied_token: str, expected_token: str) -> bool:
    """Constant-time token verification to mitigate timing attacks."""
    if not supplied_token or not expected_token:
        return False
    return hmac.compare_digest(supplied_token, expected_token)


def validate_extension_origin(
    origin: str,
    allowlisted_ids: Optional[Set[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Validates that the HTTP Origin header corresponds to an authorized Chrome Extension.

    Returns:
        (is_valid, extension_id)
    """
    if not origin or not origin.startswith("chrome-extension://"):
        return False, None

    ext_id = origin[len("chrome-extension://") :].rstrip("/")
    # Chrome extension IDs are 32 lowercase alpha characters [a-p]
    if not re.match(r"^[a-p]{32}$", ext_id) and not re.match(r"^[a-zA-Z0-9_\-]+$", ext_id):
        return False, None

    if allowlisted_ids and ext_id not in allowlisted_ids:
        return False, ext_id

    return True, ext_id


def is_same_origin(target_url: str, tab_url: str) -> bool:
    """Verifies that target_url and tab_url share the exact same scheme, host, and port."""
    if not target_url or not tab_url:
        return False

    # Relative paths like '/api/v1/item' are inherently same-origin
    if target_url.startswith("/") and not target_url.startswith("//"):
        return True

    try:
        t_parsed = urllib.parse.urlsplit(target_url)
        tab_parsed = urllib.parse.urlsplit(tab_url)

        if not t_parsed.scheme or not t_parsed.netloc:
            # Not an absolute URL with scheme and host
            return False

        t_port = t_parsed.port or (443 if t_parsed.scheme == "https" else 80)
        tab_port = tab_parsed.port or (443 if tab_parsed.scheme == "https" else 80)

        return (
            t_parsed.scheme.lower() == tab_parsed.scheme.lower()
            and (t_parsed.hostname or "").lower() == (tab_parsed.hostname or "").lower()
            and t_port == tab_port
        )
    except Exception:
        return False


class AuditLogger:
    """Records audit logs for browser actions without persisting sensitive credentials or cookies."""

    def __init__(self, in_memory_capacity: int = 1000):
        self.in_memory_capacity = in_memory_capacity
        self.entries: List[Dict[str, Any]] = []

    def record(
        self,
        action: str,
        *,
        job_id: Optional[str] = None,
        tab_id: Optional[int] = None,
        hostname: Optional[str] = None,
        status: str = "ok",
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "job_id": job_id,
            "tab_id": tab_id,
            "hostname": hostname,
            "status": status,
            "error_code": error_code,
            "details": redact_sensitive_data(details or {}),
        }
        self.entries.append(entry)
        if len(self.entries) > self.in_memory_capacity:
            self.entries.pop(0)

        audit_logger.info(
            "AUDIT action=%s job=%s tab=%s host=%s status=%s code=%s",
            action,
            job_id,
            tab_id,
            hostname,
            status,
            error_code,
        )
        return entry

