"""
Target management (pages, popups).
"""
from typing import Dict, Any, List

class TargetManager:
    def __init__(self, backend):
        self.backend = backend
        
    async def list_targets(self) -> List[Dict[str, Any]]:
        return await self.backend.list_targets()
        
    async def create_target(self, url: str) -> Dict[str, Any]:
        return await self.backend.create_target(url)
        
    async def activate_target(self, target_id: str):
        await self.backend.activate_target(target_id)
        
    async def close_target(self, target_id: str):
        await self.backend.close_target(target_id)
