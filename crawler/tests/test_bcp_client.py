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


def test_browser_fetcher_with_target_id():
    from crawler.fetcher import BrowserFetcher
    from crawler.models import Task, Source

    async def _test():
        class MockBcpTransport(IBrowserTransport):
            def __init__(self):
                self.calls = []

            async def connect(self, endpoint: str = "", token: str = "") -> dict:
                return {"protocolVersion": "1.0"}

            async def call(self, method: str, params: dict, timeout_ms: int = 30000) -> dict:
                self.calls.append((method, params))
                if method == "target.create":
                    return {"targetId": "target_abc_123"}
                elif method == "page.content":
                    return {"content": "<html><body>Fetched with targetId</body></html>"}
                elif method == "runtime.evaluate":
                    return {"result": "https://example.com/final"}
                elif method == "target.close":
                    return {"ok": True}
                return {}

            def on(self, event: str, handler):
                return lambda: None

            @property
            def capabilities(self) -> set[str]:
                return set()

            async def close(self):
                pass

        transport = MockBcpTransport()
        fetcher = BrowserFetcher(transport=transport)
        from crawler.models import Task, Source, IdentityConfig

        task = Task(source_id="test_src", url="https://example.com/article", dedup_key="art_1")
        res = await fetcher.fetch(task, Source(source_id="test_src", identity=IdentityConfig(id_regex=r"/(\d+)")))
        assert res.status == 200
        assert b"Fetched with targetId" in res.body
        assert res.final_url == "https://example.com/final"

        # Verify targetId was passed in calls
        methods_called = [c[0] for c in transport.calls]
        assert "target.create" in methods_called
        assert "page.navigate" in methods_called
        assert "page.content" in methods_called
        assert "runtime.evaluate" in methods_called
        assert "target.close" in methods_called

        for m, p in transport.calls:
            if m in ("page.navigate", "page.content", "runtime.evaluate", "target.close"):
                assert p.get("targetId") == "target_abc_123"

    asyncio.run(_test())

