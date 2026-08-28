"""
Compliance engine for crawler.
Implements non-bypassable constraints: robots.txt, rate limits, circuit breakers, and access class gates.
"""

import asyncio
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx

from .models import AccessClass, Source, Task


class ComplianceResult(Enum):
    ALLOW = "allow"
    DENY = "deny"
    DELAY = "delay"


@dataclass
class ComplianceDecision:
    result: ComplianceResult
    reason: str = ""
    delay_ms: int = 0


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class CircuitBreaker:
    """Per-host circuit breaker."""

    def __init__(self):
        self._states: Dict[str, CircuitState] = {}
        self._failures: Dict[str, int] = {}
        self._429_failures: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    def check(self, host: str) -> bool:
        """Return True if circuit is closed or half-open."""
        state = self._states.get(host, CircuitState.CLOSED)
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.OPEN:
            if time.time() >= self._open_until.get(host, 0):
                self._states[host] = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow 1 request
        return True

    def record_response(self, host: str, status: int):
        if status < 400 or status in (404, 410):
            # Success resets breaker
            self._failures[host] = 0
            self._429_failures[host] = 0
            self._states[host] = CircuitState.CLOSED
        elif status == 429:
            self._429_failures[host] = self._429_failures.get(host, 0) + 1
            if self._429_failures[host] >= 3:
                self._open(host)
        elif status >= 500:
            self._failures[host] = self._failures.get(host, 0) + 1
            if self._failures[host] >= 5:
                self._open(host)

    def _open(self, host: str):
        self._states[host] = CircuitState.OPEN
        self._open_until[host] = time.time() + 600  # 10 minutes


class RateLimiter:
    """Token bucket per host, jittered."""

    def __init__(self):
        self._last_request_time: Dict[str, float] = {}
        self._penalized_until: Dict[str, float] = {}

    def get_delay_ms(self, host: str, default_delay_ms: int = 1000) -> int:
        now = time.time()
        
        # Check if we are penalized due to 429
        delay = default_delay_ms / 1000.0
        if now < self._penalized_until.get(host, 0):
            delay *= 2.0  # Halve rate (double delay)
            
        last = self._last_request_time.get(host, 0)
        elapsed = now - last
        if elapsed < delay:
            # Jitter 10%
            jitter = delay * 0.1 * random.uniform(0, 1)
            return int((delay - elapsed + jitter) * 1000)
        
        return 0

    def record_request(self, host: str):
        self._last_request_time[host] = time.time()

    def record_429(self, host: str, retry_after: Optional[int] = None):
        penalty_duration = retry_after if retry_after else 1800  # 30 min
        self._penalized_until[host] = time.time() + penalty_duration


class RobotsCache:
    """Fetch, parse, cache robots.txt per host. Cache 24h. Fail closed on 5xx."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient()
        self._parsers: Dict[str, urllib.robotparser.RobotFileParser] = {}
        self._expires: Dict[str, float] = {}
        self._deny_until: Dict[str, float] = {}

    async def check(self, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc
        scheme = parsed.scheme
        
        now = time.time()
        if now < self._deny_until.get(host, 0):
            return False

        if host not in self._parsers or now > self._expires.get(host, 0):
            robots_url = f"{scheme}://{host}/robots.txt"
            rp = urllib.robotparser.RobotFileParser(url=robots_url)
            
            try:
                resp = await self._client.get(robots_url, timeout=5.0)
                if resp.status_code == 404:
                    # Allow all
                    rp.allow_all = True
                elif resp.status_code >= 500:
                    # Fail closed
                    self._deny_until[host] = now + 600  # Wait 10 mins before retry
                    return False
                else:
                    rp.parse(resp.text.splitlines())
                    
                self._parsers[host] = rp
                self._expires[host] = now + 86400  # 24h cache
            except Exception:
                # Network error -> fail closed temporarily
                self._deny_until[host] = now + 300
                return False

        rp = self._parsers[host]
        return rp.can_fetch(user_agent, url)


class ComplianceEngine:
    """Main gate. Every request passes through authorize()."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._client = http_client or httpx.AsyncClient()
        self._robots = RobotsCache(self._client)
        self._limiter = RateLimiter()
        self._breaker = CircuitBreaker()

    async def authorize(self, task: Task, source: Source) -> ComplianceDecision:
        # 1. Check access_class gate
        if source.access_class != AccessClass.PUBLIC:
            if not source.authorization_ref:
                return ComplianceDecision(
                    result=ComplianceResult.DENY,
                    reason="Restricted access class without authorization_ref"
                )

        parsed = urlparse(task.url or "")
        host = parsed.netloc

        # 2. Check circuit breaker
        if not self._breaker.check(host):
            return ComplianceDecision(
                result=ComplianceResult.DENY,
                reason="Circuit breaker open"
            )

        # 3. Check robots.txt
        user_agent = source.user_agent
        if task.url and not await self._robots.check(task.url, user_agent):
            return ComplianceDecision(
                result=ComplianceResult.DENY,
                reason="Denied by robots.txt"
            )

        # 4. Check rate limiter
        delay_ms = self._limiter.get_delay_ms(
            host,
            default_delay_ms=source.politeness.min_interval_ms if source.politeness else 1000
        )
        if delay_ms > 0:
            return ComplianceDecision(
                result=ComplianceResult.DELAY,
                delay_ms=delay_ms,
                reason="Rate limiting"
            )

        self._limiter.record_request(host)
        return ComplianceDecision(result=ComplianceResult.ALLOW)

    def record_response(self, host: str, status: int, retry_after: Optional[int] = None):
        """Update circuit breaker and rate limiter state based on responses."""
        self._breaker.record_response(host, status)
        if status == 429:
            self._limiter.record_429(host, retry_after)
