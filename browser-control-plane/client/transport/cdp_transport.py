"""
Browser Control Plane - CDP Transport Implementation
Wraps Playwright in-process.
"""
import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Dict

from .itransport import IBrowserTransport, BcpError

class CdpTransport(IBrowserTransport):
    def __init__(self):
        self._playwright: Any = None
        self._browser_context: Any = None
        self._page: Any = None
        self._handlers: Dict[str, list[Callable[[dict], Any]]] = {}

    async def connect(self, endpoint: str = "", token: str = "") -> dict:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BcpError(f"Playwright is not installed: {e}", "UNSUPPORTED")

        self._playwright = await async_playwright().start()
        
        browser_state_dir = os.environ.get("BROWSER_STATE_DIR", str(Path.home() / ".coworker-browser"))
        self._browser_context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=browser_state_dir,
            headless=True
        )
        
        pages = self._browser_context.pages
        if pages:
            self._page = pages[0]
        else:
            self._page = await self._browser_context.new_page()
            
        return {"protocolVersion": "1.0", "backend": "cdp"}

    async def call(self, method: str, params: dict, timeout_ms: int = 30000) -> dict:
        if not self._page:
            raise BcpError("Not connected", "ConnectionError")
            
        try:
            if method == "agent.hello":
                return {
                    "protocolVersion": "1.0",
                    "backend": "cdp",
                    "capabilities": list(self.capabilities)
                }
            elif method == "page.navigate":
                await self._page.goto(params["url"], wait_until=params.get("waitUntil", "load"), timeout=timeout_ms)
                return {}
            elif method == "page.content":
                content = await self._page.content()
                return {"content": content}
            elif method == "page.screenshot":
                screenshot = await self._page.screenshot(full_page=params.get("fullPage", False))
                return {"data": screenshot} # Assuming bytes are returned, might need b64 encoding in real life
            elif method == "page.setViewport":
                await self._page.set_viewport_size({"width": params["width"], "height": params["height"]})
                return {}
            elif method == "runtime.evaluate":
                result = await self._page.evaluate(params["expression"], params.get("arg"))
                return {"result": result}
            elif method == "dom.query":
                # Assuming simple query selector
                element = await self._page.query_selector(params["selector"])
                return {"found": element is not None}
            elif method == "dom.waitForSelector":
                element = await self._page.wait_for_selector(params["selector"], state=params.get("state", "visible"), timeout=timeout_ms)
                return {"found": element is not None}
            elif method == "dom.snapshot":
                # Basic mock for DOM snapshot
                snapshot = await self._page.evaluate("() => document.body.outerHTML")
                return {"snapshot": snapshot}
            elif method == "dom.text":
                text = await self._page.inner_text(params["selector"])
                return {"text": text}
            elif method == "dom.html":
                html = await self._page.evaluate(f"() => document.querySelector('{params['selector']}').outerHTML")
                return {"html": html}
            elif method == "input.click":
                await self._page.click(params["selector"], timeout=timeout_ms)
                return {}
            elif method == "input.type":
                await self._page.type(params["selector"], params["text"], delay=params.get("delay", 0))
                return {}
            elif method == "input.press":
                await self._page.press(params["selector"], params["key"])
                return {}
            elif method == "input.scroll":
                await self._page.mouse.wheel(params.get("dx", 0), params.get("dy", 0))
                return {}
            elif method == "storage.getCookies":
                cookies = await self._browser_context.cookies(params.get("urls"))
                return {"cookies": cookies}
            elif method == "storage.setCookies":
                await self._browser_context.add_cookies(params["cookies"])
                return {}
            elif method == "storage.clearCookies":
                await self._browser_context.clear_cookies()
                return {}
            elif method == "agent.shutdown":
                await self.close()
                return {}
            else:
                raise BcpError(f"Unsupported method: {method}", "NotImplementedError")
        except Exception as e:
            raise BcpError(str(e), "CallError")

    def on(self, event: str, handler: Callable[[dict], Any]) -> Callable[[], None]:
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)
        
        def unsubscribe():
            if handler in self._handlers.get(event, []):
                self._handlers[event].remove(handler)
        return unsubscribe

    @property
    def capabilities(self) -> set[str]:
        return {"network.interception", "input.trusted", "page.download", "dom.snapshot"}

    async def close(self):
        if self._browser_context:
            await self._browser_context.close()
        if self._playwright:
            await self._playwright.stop()
