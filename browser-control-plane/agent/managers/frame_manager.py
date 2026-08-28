"""
Frame Manager — page.* methods.
Manages navigation, lifecycle, screenshots, dialogs, downloads.
Backend delegates to cdp/extension/embedder backends via CdpBackend.
"""
from typing import Any, Dict, List, Optional


class FrameManager:
    def __init__(self, backend):
        self.backend = backend

    async def navigate(
        self,
        target_id: str,
        url: str,
        wait_until: str = "load",
        referrer: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        return await self.backend.navigate(target_id, url, wait_until, referrer, timeout_ms)

    async def reload(self, target_id: str, timeout_ms: int = 30000) -> Dict[str, Any]:
        return await self.backend.reload(target_id, timeout_ms)

    async def go_back(self, target_id: str, timeout_ms: int = 30000) -> Dict[str, Any]:
        return await self.backend.go_back(target_id, timeout_ms)

    async def go_forward(self, target_id: str, timeout_ms: int = 30000) -> Dict[str, Any]:
        return await self.backend.go_forward(target_id, timeout_ms)

    async def content(
        self, target_id: str, frame_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self.backend.get_content(target_id, frame_id)

    async def screenshot(
        self, target_id: str, options: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.backend.screenshot(
            target_id,
            fmt=options.get("format", "png"),
            quality=options.get("quality"),
            full_page=options.get("fullPage", False),
            clip=options.get("clip"),
        )

    async def set_viewport(
        self, target_id: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.backend.set_viewport(
            target_id,
            width=params.get("width", 1280),
            height=params.get("height", 720),
            dpr=params.get("dpr", 1.0),
            mobile=params.get("mobile", False),
        )

    async def list_frames(self, target_id: str) -> Dict[str, Any]:
        return await self.backend.get_frame_tree(target_id)

    async def wait_for_load_state(
        self, target_id: str, state: str = "load", timeout_ms: int = 30000
    ) -> Dict[str, Any]:
        return await self.backend.wait_for_load_state(target_id, state, timeout_ms)

    async def handle_dialog(
        self, target_id: str, action: str, prompt_text: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self.backend.handle_dialog(target_id, action, prompt_text)

    async def set_download_behavior(
        self, target_id: str, mode: str, path: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self.backend.set_download_behavior(target_id, mode, path)
