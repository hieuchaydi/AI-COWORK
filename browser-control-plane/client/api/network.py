"""
Browser Control Plane - Network API
Manages network lifecycle and interception.
"""
from typing import Callable, Any
from ..transport.itransport import IBrowserTransport

class Network:
    def __init__(self, transport: IBrowserTransport):
        self._transport = transport

    def on_request(self, handler: Callable[[dict], Any]) -> Callable[[], None]:
        return self._transport.on("network.request", handler)

    def on_response(self, handler: Callable[[dict], Any]) -> Callable[[], None]:
        return self._transport.on("network.response", handler)
