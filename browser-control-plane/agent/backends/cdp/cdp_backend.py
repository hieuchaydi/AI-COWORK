"""
CDP Backend (B0) — Playwright-based implementation of all manager operations.

This is the reference backend (§2 Backend B0). It drives Chromium via
Playwright's CDP interface internally. No Playwright types leak above this file.

Playwright is lazy-imported so tests that don't exercise this backend
(e.g. unit tests using mock transports) don't fail on ImportError.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PLAYWRIGHT_AVAILABLE = False
try:
    import playwright  # noqa: F401
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass


class CdpBackend:
    """
    Playwright-backed CDP backend.
    One instance per bcp-agent process (one per browser profile).
    """

    def __init__(self):
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        # Map targetId → Playwright Page object
        self._pages: Dict[str, Any] = {}
        self._next_target_id = 0

    def _new_target_id(self) -> str:
        self._next_target_id += 1
        return f"t{self._next_target_id}"

    def _get_page(self, target_id: Optional[str]) -> Any:
        if not self._pages:
            raise RuntimeError("No open targets. Call target.create first.")
        if target_id is None:
            # Default to first page
            return next(iter(self._pages.values()))
        page = self._pages.get(target_id)
        if page is None:
            raise ValueError(f"TARGET_NOT_FOUND: {target_id}")
        return page

    async def start(self, user_data_dir: str = "", headless: bool = True):
        """Launch Chromium. Called once by the agent process."""
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
            )
        from playwright.async_api import async_playwright
        import os
        from pathlib import Path

        self._playwright = await async_playwright().start()
        if user_data_dir:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir, headless=headless
            )
            # Adopt existing pages
            for page in self._context.pages:
                tid = self._new_target_id()
                self._pages[tid] = page
        else:
            self._browser = await self._playwright.chromium.launch(headless=headless)
            self._context = await self._browser.new_context()

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Target management ─────────────────────────────────────────────────

    async def list_targets(self) -> List[Dict[str, Any]]:
        return [
            {"targetId": tid, "url": page.url, "type": "page"}
            for tid, page in self._pages.items()
        ]

    async def create_target(self, url: str = "about:blank", incognito: bool = False) -> Dict[str, Any]:
        if incognito:
            ctx = await self._browser.new_context()
            page = await ctx.new_page()
        else:
            page = await self._context.new_page()
        tid = self._new_target_id()
        self._pages[tid] = page
        if url and url != "about:blank":
            await page.goto(url)
        return {"targetId": tid}

    async def activate_target(self, target_id: str) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.bring_to_front()
        return {"targetId": target_id}

    async def close_target(self, target_id: str) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.close()
        del self._pages[target_id]
        return {}

    # ── Navigation ────────────────────────────────────────────────────────

    async def navigate(
        self,
        target_id: Optional[str],
        url: str,
        wait_until: str = "load",
        referrer: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        kwargs: Dict[str, Any] = {"wait_until": wait_until, "timeout": timeout_ms}
        if referrer:
            kwargs["referer"] = referrer
        resp = await page.goto(url, **kwargs)
        frame_id = page.main_frame._impl_obj._guid if hasattr(page, "main_frame") else "f1"
        return {
            "frameId": frame_id,
            "httpStatus": resp.status if resp else 0,
        }

    async def reload(self, target_id: Optional[str], timeout_ms: int = 30000) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.reload(timeout=timeout_ms)
        return {}

    async def go_back(self, target_id: Optional[str], timeout_ms: int = 30000) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.go_back(timeout=timeout_ms)
        return {}

    async def go_forward(self, target_id: Optional[str], timeout_ms: int = 30000) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.go_forward(timeout=timeout_ms)
        return {}

    async def get_content(
        self, target_id: Optional[str], frame_id: Optional[str] = None
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        content = await page.content()
        return {"content": content}

    async def screenshot(
        self,
        target_id: Optional[str],
        fmt: str = "png",
        quality: Optional[int] = None,
        full_page: bool = False,
        clip: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        kwargs: Dict[str, Any] = {"type": fmt, "full_page": full_page}
        if quality is not None:
            kwargs["quality"] = quality
        if clip:
            kwargs["clip"] = clip
        data = await page.screenshot(**kwargs)
        return {"data": data, "format": fmt}

    async def set_viewport(
        self,
        target_id: Optional[str],
        width: int = 1280,
        height: int = 720,
        dpr: float = 1.0,
        mobile: bool = False,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.set_viewport_size({"width": width, "height": height})
        return {}

    async def get_frame_tree(self, target_id: Optional[str]) -> Dict[str, Any]:
        page = self._get_page(target_id)
        frames = [
            {"frameId": f._impl_obj._guid if hasattr(f, "_impl_obj") else str(i), "url": f.url}
            for i, f in enumerate(page.frames)
        ]
        return {"frames": frames}

    async def wait_for_load_state(
        self, target_id: Optional[str], state: str = "load", timeout_ms: int = 30000
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        await page.wait_for_load_state(state, timeout=timeout_ms)
        return {}

    async def handle_dialog(
        self, target_id: Optional[str], action: str, prompt_text: Optional[str] = None
    ) -> Dict[str, Any]:
        # Dialogs are handled via event-driven approach; this is a one-shot handler
        page = self._get_page(target_id)
        handled = {"done": False}

        async def _handler(dialog):
            if not handled["done"]:
                handled["done"] = True
                if action == "accept":
                    await dialog.accept(prompt_text or "")
                else:
                    await dialog.dismiss()

        page.once("dialog", _handler)
        return {}

    async def set_download_behavior(
        self, target_id: Optional[str], mode: str, path: Optional[str] = None
    ) -> Dict[str, Any]:
        # Playwright handles downloads via context-level settings
        return {}

    # ── Runtime ───────────────────────────────────────────────────────────

    async def evaluate(
        self,
        target_id: Optional[str],
        expression: str,
        frame_id: Optional[str] = None,
        world: str = "main",
        await_promise: bool = False,
        return_by_value: bool = True,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        try:
            result = await page.evaluate(expression)
        except Exception as exc:
            raise RuntimeError(f"JS_EXCEPTION: {exc}") from exc
        return {"result": result}

    async def call_function(
        self,
        handle_id: str,
        function_declaration: str,
        args: List[Any],
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        # Simplified: evaluate the function with args
        raise NotImplementedError("callFunction requires handle tracking")

    async def release_handle(self, handle_id: str):
        pass  # Playwright handles GC automatically

    async def add_init_script(self, source: str, world: str = "main"):
        if self._context:
            await self._context.add_init_script(source)

    # ── DOM ───────────────────────────────────────────────────────────────

    async def query_selector(
        self,
        target_id: Optional[str],
        selector: str,
        engine: str = "css",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        element = await page.query_selector(selector)
        return {"found": element is not None, "handleId": id(element) if element else None}

    async def query_selector_all(
        self,
        target_id: Optional[str],
        selector: str,
        engine: str = "css",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        elements = await page.query_selector_all(selector)
        return {"handles": [{"handleId": id(el)} for el in elements], "count": len(elements)}

    async def wait_for_selector(
        self,
        target_id: Optional[str],
        selector: str,
        state: str = "visible",
        timeout_ms: int = 30000,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        element = await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        return {"found": element is not None}

    async def get_attributes(self, handle_id: str) -> Dict[str, Any]:
        return {"attributes": {}}  # Requires handle tracking

    async def get_text(
        self, target_id: Optional[str], selector: str, frame_id: Optional[str] = None
    ) -> str:
        page = self._get_page(target_id)
        text = await page.inner_text(selector)
        return text

    async def get_html(
        self, target_id: Optional[str], selector: str, frame_id: Optional[str] = None
    ) -> str:
        page = self._get_page(target_id)
        html = await page.evaluate(
            f"document.querySelector('{selector}')?.outerHTML ?? ''"
        )
        return html

    async def get_bounding_box(
        self, target_id: Optional[str], selector: str, frame_id: Optional[str] = None
    ) -> Optional[Dict[str, float]]:
        page = self._get_page(target_id)
        element = await page.query_selector(selector)
        if element:
            box = await element.bounding_box()
            return box
        return None

    async def scroll_into_view(
        self, target_id: Optional[str], selector: str, frame_id: Optional[str] = None
    ):
        page = self._get_page(target_id)
        element = await page.query_selector(selector)
        if element:
            await element.scroll_into_view_if_needed()

    async def dom_snapshot(
        self,
        target_id: Optional[str],
        mode: str = "flat",
        max_nodes: int = 10000,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(target_id)
        if mode == "accessibility":
            snapshot = await page.accessibility.snapshot()
        else:
            # Flat DOM snapshot: outerHTML of body
            snapshot = await page.evaluate(
                f"(function(){{ const el = document.body; "
                f"const nodes = []; const walk = (n, depth) => {{"
                f"if (nodes.length >= {max_nodes}) return; "
                f"nodes.push({{tag: n.tagName, id: n.id, cls: n.className, depth}});"
                f"for (const c of n.children) walk(c, depth+1);"
                f"}}; walk(el, 0); return nodes; }})()"
            )
        return {"snapshot": snapshot, "mode": mode}

    # ── Input ─────────────────────────────────────────────────────────────

    async def click(
        self,
        target_id: Optional[str],
        handle_id: Optional[str] = None,
        selector: Optional[str] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: List[str] = (),
        timeout_ms: int = 30000,
    ):
        page = self._get_page(target_id)
        await page.click(
            selector or "body",
            button=button,
            click_count=click_count,
            timeout=timeout_ms,
        )

    async def hover(
        self, target_id: Optional[str], selector: Optional[str] = None, timeout_ms: int = 30000
    ):
        page = self._get_page(target_id)
        await page.hover(selector or "body", timeout=timeout_ms)

    async def type_text(
        self,
        target_id: Optional[str],
        selector: Optional[str] = None,
        text: str = "",
        delay_ms: int = 0,
        timeout_ms: int = 30000,
    ):
        page = self._get_page(target_id)
        await page.type(selector or "body", text, delay=delay_ms, timeout=timeout_ms)

    async def press(
        self,
        target_id: Optional[str],
        selector: Optional[str] = None,
        key: str = "Enter",
        modifiers: List[str] = (),
    ):
        page = self._get_page(target_id)
        await page.press(selector or "body", key)

    async def scroll(
        self,
        target_id: Optional[str],
        dx: int = 0,
        dy: int = 0,
        point: Optional[Dict[str, float]] = None,
    ):
        page = self._get_page(target_id)
        await page.mouse.wheel(dx, dy)

    async def drag_and_drop(
        self,
        target_id: Optional[str],
        from_point: Dict[str, float],
        to_point: Dict[str, float],
    ):
        page = self._get_page(target_id)
        await page.mouse.move(from_point["x"], from_point["y"])
        await page.mouse.down()
        await page.mouse.move(to_point["x"], to_point["y"])
        await page.mouse.up()

    async def upload_files(
        self, target_id: Optional[str], handle_id: str, paths: List[str]
    ):
        # Playwright: set_input_files on the element
        pass  # Requires handle → selector lookup

    # ── Network ───────────────────────────────────────────────────────────

    async def enable_network(
        self,
        target_id: Optional[str],
        capture_bodies: str = "none",
        max_body_bytes: int = 1_048_576,
    ):
        pass  # Playwright captures network via route handlers / request events

    async def disable_network(self, target_id: Optional[str]):
        pass

    async def get_response_body(self, request_id: str) -> bytes:
        return b""  # Requires tracking request objects

    async def set_interception(self, target_id: Optional[str], patterns: List[Dict]):
        pass

    async def continue_request(self, request_id: str, overrides: Dict):
        pass

    async def fulfill_request(self, request_id: str, response: Dict):
        pass

    async def abort_request(self, request_id: str, error: str):
        pass

    # ── Storage ───────────────────────────────────────────────────────────

    async def get_cookies(
        self, target_id: Optional[str], urls: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        if self._context:
            return await self._context.cookies(urls or [])
        return []

    async def set_cookies(self, target_id: Optional[str], cookies: List[Dict[str, Any]]):
        if self._context:
            await self._context.add_cookies(cookies)

    async def clear_cookies(self, target_id: Optional[str]):
        if self._context:
            await self._context.clear_cookies()

    async def get_local_storage(
        self, target_id: Optional[str], frame_id: Optional[str] = None
    ) -> Dict[str, str]:
        page = self._get_page(target_id)
        return await page.evaluate(
            "Object.fromEntries(Object.entries(localStorage))"
        )

    async def set_local_storage(
        self,
        target_id: Optional[str],
        items: Dict[str, str],
        frame_id: Optional[str] = None,
    ):
        page = self._get_page(target_id)
        for k, v in items.items():
            await page.evaluate(f"localStorage.setItem('{k}', '{v}')")

    async def get_session_storage(
        self, target_id: Optional[str], frame_id: Optional[str] = None
    ) -> Dict[str, str]:
        page = self._get_page(target_id)
        return await page.evaluate(
            "Object.fromEntries(Object.entries(sessionStorage))"
        )

    async def export_state(self, target_id: Optional[str]) -> Dict[str, Any]:
        if self._context:
            state = await self._context.storage_state()
            return state
        return {}

    async def import_state(self, target_id: Optional[str], state: Dict[str, Any]):
        # State import requires a new context; log for now
        logger.warning("import_state: full profile state import requires context recreation")
