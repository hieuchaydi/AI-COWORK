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


def mask_token(token: Optional[str]) -> str:
    """Masks a token string for safe logging."""
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def get_allowed_extension_ids() -> Set[str]:
    """Retrieves allowlisted extension IDs from environment or configuration."""
    import os
    env_val = os.environ.get("ALLOWED_EXTENSION_IDS", "").strip()
    if env_val:
        return {x.strip() for x in env_val.split(",") if x.strip()}
    return set()


def is_same_origin(target_url: str, tab_url: str) -> bool:
    """Verifies that target_url and tab_url share the exact same scheme, host, and port.

    Permits only 'http' and 'https' protocols. Rejects URLs containing userinfo (@),
    mismatched ports/subdomains, non-web schemes (chrome, file, data, javascript),
    or malformed URL structures. Relative paths are resolved securely against tab_url.
    """
    if not target_url or not tab_url:
        return False

    if "://" in target_url:
        lower_target = target_url.lower()
        if not (lower_target.startswith("http://") or lower_target.startswith("https://")):
            return False
        if lower_target.count("://") > 1:
            return False

    try:
        tab_parsed = urllib.parse.urlsplit(tab_url)
        if tab_parsed.scheme.lower() not in ("http", "https"):
            return False
        if not tab_parsed.hostname:
            return False
        if tab_parsed.username or tab_parsed.password or "@" in tab_parsed.netloc:
            return False

        # Resolve target relative URL safely against tab_url
        resolved = urllib.parse.urljoin(tab_url, target_url)
        t_parsed = urllib.parse.urlsplit(resolved)

        if t_parsed.scheme.lower() not in ("http", "https"):
            return False
        if not t_parsed.hostname:
            return False
        if t_parsed.username or t_parsed.password or "@" in t_parsed.netloc:
            return False

        t_port = t_parsed.port or (443 if t_parsed.scheme.lower() == "https" else 80)
        tab_port = tab_parsed.port or (443 if tab_parsed.scheme.lower() == "https" else 80)

        return (
            t_parsed.scheme.lower() == tab_parsed.scheme.lower()
            and t_parsed.hostname.lower() == tab_parsed.hostname.lower()
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

