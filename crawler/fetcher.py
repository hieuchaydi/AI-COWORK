"""
Fetcher interface and implementations for different fetch rungs.

Rung 0 = structured feed (RSS/sitemap/JSON API)
Rung 1 = plain HTTP GET + HTML parse
Rung 2 = HTTP + embedded JSON extraction
Rung 3 = HTTP to site's own XHR/JSON endpoints
Rung 4 = Browser (BCP Page API)
"""

import time
import gzip
import zlib
from abc import ABC, abstractmethod
from typing import Dict, Optional, Any

import httpx

from .models import FetchResult, Source, Task

# Cache store for conditional GET headers (in-memory; production would use storage)
_etag_cache: Dict[str, str] = {}
_last_modified_cache: Dict[str, str] = {}


class Fetcher(ABC):
    @abstractmethod
    async def fetch(self, task: Task, source: Source) -> FetchResult:
        """Fetch the resource and return a FetchResult."""


class HttpFetcher(Fetcher):
    """
    HTTP-based fetcher (rungs 0–3).

    Features:
    - Conditional GET via ETag/Last-Modified (304 short-circuit)
    - Accept gzip/br/deflate, auto-decompressed by httpx
    - follow_redirects, stores final_url
    - Returns error string on network failure (no raise)
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(
            headers={"Accept-Encoding": "gzip, deflate, br"},
            follow_redirects=True,
            timeout=30.0,
        )

    async def fetch(self, task: Task, source: Source) -> FetchResult:
        url = task.url or ""
        ua = source.user_agent
        timeout = source.politeness.timeout_seconds if source.politeness else 30.0

        headers: Dict[str, str] = {"User-Agent": ua}

        # ── Conditional GET ────────────────────────────────────────────────
        if url in _etag_cache:
            headers["If-None-Match"] = _etag_cache[url]
        if url in _last_modified_cache:
            headers["If-Modified-Since"] = _last_modified_cache[url]

        start = time.monotonic()
        try:
            resp = await self._client.get(url, headers=headers, timeout=timeout)
            duration_ms = int((time.monotonic() - start) * 1000)
            final_url = str(resp.url)

            # Cache validators for future conditional GETs
            if "ETag" in resp.headers:
                _etag_cache[url] = resp.headers["ETag"]
            if "Last-Modified" in resp.headers:
                _last_modified_cache[url] = resp.headers["Last-Modified"]

            return FetchResult(
                task_id=task.task_id,
                status=resp.status_code,
                headers=dict(resp.headers),
                body=resp.content,
                final_url=final_url,
                from_cache=(resp.status_code == 304),
                content_type=resp.headers.get("content-type", ""),
                timings={"total_ms": duration_ms},
            )

        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - start) * 1000)
            return FetchResult(
                task_id=task.task_id,
                status=0,
                headers={},
                body=b"",
                final_url=url,
                from_cache=False,
                content_type="",
                timings={"total_ms": duration_ms},
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return FetchResult(
                task_id=task.task_id,
                status=0,
                headers={},
                body=b"",
                final_url=url,
                from_cache=False,
                content_type="",
                timings={"total_ms": duration_ms},
            )


class BrowserFetcher(Fetcher):
    """
    Rung-4 fetcher backed by BCP Page API.

    Requires a connected `IBrowserTransport`. The BCP client's `Page` object
    handles navigation, waitUntil semantics, and HTML extraction.
    """

    def __init__(self, transport: Any = None, default_target_id: Optional[str] = None):
        """
        :param transport: An IBrowserTransport-compatible object (optional).
                         Pass None to keep as a soft stub — fetch() will raise.
        :param default_target_id: Default BCP targetId to reuse across requests.
        """
        self._transport = transport
        self._default_target_id = default_target_id

    async def fetch(self, task: Task, source: Source) -> FetchResult:
        if self._transport is None:
            raise NotImplementedError(
                "BrowserFetcher requires a connected IBrowserTransport. "
                "Ensure BCP agent is running and pass transport= at construction."
            )

        url = task.url or ""
        start = time.monotonic()
        try:
            # Ensure targetId per BCP wire protocol schema (§4.1, §4.3)
            task_params = task.params or {}
            target_id = task_params.get("targetId") or self._default_target_id
            created_target = False
            if not target_id:
                create_res = await self._transport.call("target.create", {"url": "about:blank"})
                target_id = create_res.get("targetId")
                created_target = True

            # Use BCP Page API with valid targetId
            await self._transport.call(
                "page.navigate",
                {"targetId": target_id, "url": url, "waitUntil": "load"},
                60000,
            )
            result = await self._transport.call(
                "page.content",
                {"targetId": target_id},
                10000,
            )
            html = result.get("content", "")
            duration_ms = int((time.monotonic() - start) * 1000)

            # Get the final URL (after redirects) via runtime.evaluate
            try:
                loc_result = await self._transport.call(
                    "runtime.evaluate",
                    {
                        "targetId": target_id,
                        "expression": "window.location.href",
                        "returnByValue": True,
                    },
                    5000,
                )
                final_url = loc_result.get("result", url)
            except Exception:
                final_url = url

            if created_target:
                try:
                    await self._transport.call("target.close", {"targetId": target_id}, 5000)
                except Exception:
                    pass

            return FetchResult(
                task_id=task.task_id,
                status=200,
                headers={},
                body=html.encode("utf-8"),
                final_url=final_url,
                from_cache=False,
                content_type="text/html",
                timings={"total_ms": duration_ms},
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            raise RuntimeError(f"BrowserFetcher failed for {url}: {exc}") from exc


class ApiFetcher(Fetcher):
    """
    Rung-0 API fetcher for authorized vendor API connectors (e.g., Shopee).

    The actual HTTP call is delegated to the connector's own signing logic;
    this class provides the uniform Fetcher interface the frontier/runner expects.
    """

    def __init__(self, signed_client: Any):
        """
        :param signed_client: Any object with async method
                              `request(method, path, **kwargs) -> (status, headers, body_bytes)`.
        """
        self._client = signed_client

    async def fetch(self, task: Task, source: Source) -> FetchResult:
        start = time.monotonic()
        try:
            # task.params holds the API call parameters (path, body, etc.)
            params = task.params or {}
            method = params.get("http_method", "GET")
            path = params.get("path", task.url or "")
            body = params.get("body")

            status, headers, resp_body = await self._client.request(method, path, body=body)
            duration_ms = int((time.monotonic() - start) * 1000)

            return FetchResult(
                task_id=task.task_id,
                status=status,
                headers=headers,
                body=resp_body if isinstance(resp_body, bytes) else resp_body.encode(),
                final_url=path,
                from_cache=False,
                content_type=headers.get("content-type", "application/json"),
                timings={"total_ms": duration_ms},
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            raise RuntimeError(f"ApiFetcher failed: {exc}") from exc
