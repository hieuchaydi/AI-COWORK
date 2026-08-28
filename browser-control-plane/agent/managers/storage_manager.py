"""
Storage Manager — storage.* methods.
Manages cookies, localStorage, sessionStorage, full-profile state snapshots.
"""
from typing import Any, Dict, List, Optional


class StorageManager:
    def __init__(self, backend):
        self.backend = backend

    async def get_cookies(
        self, target_id: Optional[str], urls: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        cookies = await self.backend.get_cookies(target_id, urls=urls)
        return {"cookies": cookies}

    async def set_cookies(
        self, target_id: Optional[str], cookies: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        await self.backend.set_cookies(target_id, cookies)
        return {}

    async def clear_cookies(self, target_id: Optional[str]) -> Dict[str, Any]:
        await self.backend.clear_cookies(target_id)
        return {}

    async def get_local(
        self, target_id: Optional[str], frame_id: Optional[str] = None
    ) -> Dict[str, Any]:
        items = await self.backend.get_local_storage(target_id, frame_id=frame_id)
        return {"items": items}

    async def set_local(
        self,
        target_id: Optional[str],
        items: Dict[str, str],
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.backend.set_local_storage(target_id, items, frame_id=frame_id)
        return {}

    async def get_session(
        self, target_id: Optional[str], frame_id: Optional[str] = None
    ) -> Dict[str, Any]:
        items = await self.backend.get_session_storage(target_id, frame_id=frame_id)
        return {"items": items}

    async def export_state(self, target_id: Optional[str]) -> Dict[str, Any]:
        """Export whole-profile state (cookies + storage) for persistence."""
        state = await self.backend.export_state(target_id)
        return {"state": state}

    async def import_state(
        self, target_id: Optional[str], state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Restore profile state from a previous exportState snapshot."""
        await self.backend.import_state(target_id, state)
        return {}
