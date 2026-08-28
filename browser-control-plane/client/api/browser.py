"""
Browser Control Plane - Browser API
"""
from typing import List
from .page import Page
from ..transport.itransport import IBrowserTransport

class Browser:
    def __init__(self, transport: IBrowserTransport):
        self._transport = transport
        self._pages: List[Page] = []
        
    async def new_page(self) -> Page:
        # For simplicity in this abstraction layer, we might assume 
        # the transport manages a single page or can multiplex.
        page = Page(self._transport)
        self._pages.append(page)
        return page
        
    @property
    def pages(self) -> List[Page]:
        return self._pages
        
    async def close(self):
        await self._transport.call("agent.shutdown", {})
