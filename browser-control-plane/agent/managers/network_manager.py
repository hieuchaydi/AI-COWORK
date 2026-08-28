"""
Network Manager — network.* methods.
Network monitoring, body capture, optional interception (capability-gated).
"""
from typing import Any, Dict, List, Optional


class NetworkManager:
    def __init__(self, backend):
        self.backend = backend

    async def enable(
        self,
        target_id: Optional[str],
        capture_bodies: str = "none",  # "none" | "text" | "all"
        max_body_bytes: int = 1_048_576,
    ) -> Dict[str, Any]:
        await self.backend.enable_network(
            target_id, capture_bodies=capture_bodies, max_body_bytes=max_body_bytes
        )
        return {}

    async def disable(self, target_id: Optional[str]) -> Dict[str, Any]:
        await self.backend.disable_network(target_id)
        return {}

    async def get_body(self, request_id: str) -> Dict[str, Any]:
        """Returns the response body as an attachment reference."""
        body = await self.backend.get_response_body(request_id)
        return {"body": body, "requestId": request_id}

    # ── Capability-gated interception ────────────────────────────────────
    # Only available when "network.interception" is in agent.hello.capabilities

    async def set_interception(
        self, target_id: Optional[str], patterns: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        await self.backend.set_interception(target_id, patterns)
        return {}

    async def continue_request(
        self, request_id: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        await self.backend.continue_request(request_id, overrides or {})
        return {}

    async def fulfill_request(
        self, request_id: str, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        await self.backend.fulfill_request(request_id, response)
        return {}

    async def abort_request(self, request_id: str, error: str = "Aborted") -> Dict[str, Any]:
        await self.backend.abort_request(request_id, error)
        return {}
