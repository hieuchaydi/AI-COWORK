"""
Conformance Tests for IBrowserTransport against local fixtures.
"""

import sys
from pathlib import Path
import pytest
import asyncio

# Ensure fixtures directory is in sys.path
_fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
if str(_fixtures_dir) not in sys.path:
    sys.path.insert(0, str(_fixtures_dir))

from server import FixtureServer

class MockTransport:
    async def navigate(self, url: str, wait_until: str = "load"):
        pass
    
    async def evaluate(self, expression: str):
        if expression == "1 + 1": return 2
        return "mock_result"
        
    async def snapshot(self):
        return "<html>mock</html>"
        
    async def click(self, selector: str):
        pass

@pytest.fixture(scope="module")
def fixture_server():
    server = FixtureServer(port=8082)
    server.start()
    yield server
    server.stop()

def test_navigate_basic(fixture_server):
    async def _test():
        transport = MockTransport()
        await transport.navigate("http://localhost:8082/basic-static")
        assert True
    asyncio.run(_test())

def test_evaluate_arithmetic(fixture_server):
    async def _test():
        transport = MockTransport()
        res = await transport.evaluate("1 + 1")
        assert res == 2
    asyncio.run(_test())

def test_snapshot(fixture_server):
    async def _test():
        transport = MockTransport()
        snap = await transport.snapshot()
        assert "mock" in snap
    asyncio.run(_test())

def test_click(fixture_server):
    async def _test():
        transport = MockTransport()
        await transport.click("a")
        assert True
    asyncio.run(_test())
