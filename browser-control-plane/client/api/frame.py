"""
Browser Control Plane - Frame API
Mirrors Page functionality for frames.
"""
from typing import Any, Callable, Optional
from ..transport.itransport import IBrowserTransport
from .locator import Locator

class Frame:
    def __init__(self, transport: IBrowserTransport, frame_id: str):
        self._transport = transport
        self.frame_id = frame_id

    async def evaluate(self, expression: str, *args) -> Any:
        arg = args[0] if args else None
        # We might pass frame_id in a real implementation
        result = await self._transport.call("runtime.evaluate", {"expression": expression, "arg": arg, "frameId": self.frame_id})
        return result.get("result")

    def locator(self, selector: str) -> Locator:
        # Locator might need to know about frame_id
        return Locator(self._transport, selector, frame_id=self.frame_id)

    async def wait_for_selector(self, selector: str, state: str = "visible", timeout_ms: int = 30000) -> bool:
        result = await self._transport.call("dom.waitForSelector", {"selector": selector, "state": state, "frameId": self.frame_id}, timeout_ms)
        return result.get("found", False)
