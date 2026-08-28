"""
Input Manager — input.* methods.
Handles mouse, keyboard, scroll, drag-and-drop, file upload.
Actionability is enforced at the client (Locator); the manager trusts the dispatcher.
"""
from typing import Any, Dict, List, Optional


class InputManager:
    def __init__(self, backend):
        self.backend = backend

    async def click(
        self,
        target_id: Optional[str],
        handle_id: Optional[str],
        selector: Optional[str],
        button: str = "left",
        click_count: int = 1,
        modifiers: List[str] = (),
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        await self.backend.click(
            target_id,
            handle_id=handle_id,
            selector=selector,
            button=button,
            click_count=click_count,
            modifiers=list(modifiers),
            timeout_ms=timeout_ms,
        )
        return {}

    async def hover(
        self,
        target_id: Optional[str],
        selector: Optional[str],
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        await self.backend.hover(target_id, selector=selector, timeout_ms=timeout_ms)
        return {}

    async def type_text(
        self,
        target_id: Optional[str],
        selector: Optional[str],
        text: str,
        delay_ms: int = 0,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        await self.backend.type_text(
            target_id, selector=selector, text=text, delay_ms=delay_ms, timeout_ms=timeout_ms
        )
        return {}

    async def press(
        self,
        target_id: Optional[str],
        selector: Optional[str],
        key: str,
        modifiers: List[str] = (),
    ) -> Dict[str, Any]:
        await self.backend.press(
            target_id, selector=selector, key=key, modifiers=list(modifiers)
        )
        return {}

    async def scroll(
        self,
        target_id: Optional[str],
        dx: int = 0,
        dy: int = 0,
        point: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        await self.backend.scroll(target_id, dx=dx, dy=dy, point=point)
        return {}

    async def drag_and_drop(
        self,
        target_id: Optional[str],
        from_point: Dict[str, float],
        to_point: Dict[str, float],
    ) -> Dict[str, Any]:
        await self.backend.drag_and_drop(target_id, from_point=from_point, to_point=to_point)
        return {}

    async def upload_files(
        self,
        target_id: Optional[str],
        handle_id: str,
        paths: List[str],
    ) -> Dict[str, Any]:
        """
        Upload files to a file input element.
        §10: paths must be within the configured allow-listed directory.
        """
        await self.backend.upload_files(target_id, handle_id=handle_id, paths=paths)
        return {}
