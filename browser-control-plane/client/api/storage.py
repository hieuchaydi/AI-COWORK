"""
Browser Control Plane - Storage API
Manages cookies and local storage.
"""
from typing import List, Dict, Optional
from ..transport.itransport import IBrowserTransport

class Storage:
    def __init__(self, transport: IBrowserTransport):
        self._transport = transport

    async def get_cookies(self, urls: Optional[List[str]] = None) -> List[Dict]:
        params = {}
        if urls:
            params["urls"] = urls
        result = await self._transport.call("storage.getCookies", params)
        return result.get("cookies", [])

    async def set_cookies(self, cookies: List[Dict]) -> None:
        await self._transport.call("storage.setCookies", {"cookies": cookies})

    async def clear_cookies(self) -> None:
        await self._transport.call("storage.clearCookies", {})
