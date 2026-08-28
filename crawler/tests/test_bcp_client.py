"""
Tests for BCP Client API and Transport interface.
"""
import pytest
import asyncio
from bcp import IBrowserTransport, BcpError, Page, Browser

class MockTransport(IBrowserTransport):
    def __init__(self):
        self.calls = []
        self._handlers = {}

    async def connect(self, endpoint: str = "", token: str = "") -> dict:
        return {"protocolVersion": "1.0", "backend": "mock"}

    async def call(self, method: str, params: dict, timeout_ms: int = 30000) -> dict:
        self.calls.append((method, params, timeout_ms))
        if method == "page.content":
            return {"content": "<html><body>Hello BCP</body></html>"}
        elif method == "runtime.evaluate":
            return {"result": "Mock Result"}
        elif method == "dom.text":
            return {"text": "Element Text"}
        elif method == "dom.waitForSelector":
            return {"found": True}
        elif method == "dom.snapshot":
            return {"snapshot": "<tree>Mock Snapshot</tree>"}
        elif method == "page.screenshot":
            return {"data": b"mock_png_bytes"}
        return {}

    def on(self, event: str, handler):
        self._handlers.setdefault(event, []).append(handler)
        return lambda: self._handlers[event].remove(handler)

    @property
    def capabilities(self) -> set[str]:
        return {"network.interception", "input.trusted", "page.download", "dom.snapshot"}

    async def close(self):
        pass

def test_bcp_page_api():
    async def _test():
        transport = MockTransport()
        page = Page(transport)

        # 1. goto
        await page.goto("https://example.com", wait_until="networkidle")
        assert transport.calls[-1][0] == "page.navigate"
        assert transport.calls[-1][1]["url"] == "https://example.com"
        assert transport.calls[-1][1]["waitUntil"] == "networkidle"

        # 2. content
        content = await page.content()
        assert "Hello BCP" in content

        # 3. evaluate
        eval_res = await page.evaluate("() => document.title")
        assert eval_res == "Mock Result"

        # 4. locator operations
        loc = page.locator("h1.title")
        text = await loc.inner_text()
        assert text == "Element Text"

        await loc.click()
        assert transport.calls[-1][0] == "input.click"
        assert transport.calls[-1][1]["selector"] == "h1.title"

        await loc.fill("test input")
        assert transport.calls[-1][0] == "input.type"
        assert transport.calls[-1][1]["text"] == "test input"

        # 5. snapshot
        snapshot = await page.snapshot(mode="accessibility")
        assert "Mock Snapshot" in snapshot

        # 6. screenshot
        data = await page.screenshot()
        assert data == b"mock_png_bytes"

    asyncio.run(_test())
