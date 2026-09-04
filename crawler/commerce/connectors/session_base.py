"""
Base class for Browser-Session & Human-in-the-Loop Commerce Connectors.
Strictly forbids automatic bypass or stealth evasions.
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from ..interfaces import (
    AccessDeniedException,
    CaptchaChallengeException,
    CommerceConnector,
    ProductIdentity,
    ProductSnapshot,
    ReviewPage,
    SessionExpiredException,
)

logger = logging.getLogger(__name__)


class BrowserSessionCommerceConnector(CommerceConnector):
    """
    Connector that leverages a legitimate user-authenticated browser profile.
    Detects security challenges and initiates Human-in-the-loop actions instead of bypass hacks.
    """

    CHALLENGE_INDICATORS = (
        "/verify/traffic",
        "turnstile",
        "challenge-platform",
        "captcha",
        "cf-chl-bypass",
        "distil_r_captcha",
        "security-verification",
    )

    LOGIN_INDICATORS = (
        "/login",
        "login_required",
        "auth/signin",
        "is_logged_in=false",
    )

    def check_for_challenges_or_auth(self, url: str, status_code: int, response_text: str = ""):
        """
        Inspects network responses for bot challenges or required user logins.
        Raises domain-specific exceptions to trigger Human-in-the-loop handling.
        """
        url_lower = url.lower()
        body_lower = response_text.lower() if response_text else ""

        # 1. Check for CAPTCHA / Cloudflare Turnstile / Bot Walls
        for indicator in self.CHALLENGE_INDICATORS:
            if indicator in url_lower or indicator in body_lower:
                logger.warning(
                    f"[{self.platform_name}] Challenge detected ('{indicator}'). "
                    f"Yielding to user human verification (URL: {url})."
                )
                raise CaptchaChallengeException(
                    platform=self.platform_name,
                    url=url,
                    message=f"Challenge '{indicator}' detected. Please complete verification in browser.",
                )

        # 2. Check for Login Required
        if status_code in (401, 403):
            for ind in self.LOGIN_INDICATORS:
                if ind in url_lower or ind in body_lower:
                    raise SessionExpiredException(
                        platform=self.platform_name,
                        message="User login required to access this resource.",
                    )
            raise AccessDeniedException(f"[{self.platform_name}] Access denied with HTTP {status_code}.")

        for ind in self.LOGIN_INDICATORS:
            if ind in url_lower:
                raise SessionExpiredException(
                    platform=self.platform_name,
                    message="Redirected to login page. Please sign in.",
                )
