"""
Browser Control Plane - Page API
"""
from typing import Any, Callable, Optional
from ..transport.itransport import IBrowserTransport
from .locator import Locator

class Page:
    def __init__(self, transport: IBrowserTransport):
        self._transport = transport

    async def goto(self, url: str, wait_until: str = "load", timeout_ms: int = 30000) -> None:
        await self._transport.call("page.navigate", {"url": url, "waitUntil": wait_until}, timeout_ms)

    async def evaluate(self, expression: str, *args) -> Any:
        # Simplification: passing args as a single 'arg' to transport
        arg = args[0] if args else None
        result = await self._transport.call("runtime.evaluate", {"expression": expression, "arg": arg})
        return result.get("result")

    async def content(self) -> str:
        result = await self._transport.call("page.content", {})
        return result.get("content", "")

    def locator(self, selector: str) -> Locator:
        return Locator(self._transport, selector)

    async def wait_for_selector(self, selector: str, state: str = "visible", timeout_ms: int = 30000) -> bool:
        result = await self._transport.call("dom.waitForSelector", {"selector": selector, "state": state}, timeout_ms)
        return result.get("found", False)

    async def snapshot(self, mode: str = "accessibility", max_nodes: int = -1) -> str:
        result = await self._transport.call("dom.snapshot", {"mode": mode, "maxNodes": max_nodes})
        return result.get("snapshot", "")

    async def screenshot(self, full_page: bool = False, path: Optional[str] = None) -> Any:
        result = await self._transport.call("page.screenshot", {"fullPage": full_page})
        data = result.get("data")
        if path and data:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def on(self, event: str, handler: Callable[[dict], Any]) -> Callable[[], None]:
        return self._transport.on(event, handler)

    async def close(self):
        # We assume closing a page might not be fully supported in basic transport
        pass
