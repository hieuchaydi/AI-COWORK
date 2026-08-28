"""
DOM Manager — dom.* methods.
Manages element querying, attribute reading, snapshot generation.
"""
from typing import Any, Dict, List, Optional


class DomManager:
    def __init__(self, backend):
        self.backend = backend

    async def query(
        self,
        target_id: Optional[str],
        selector: str,
        engine: str = "css",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.backend.query_selector(target_id, selector, engine=engine, frame_id=frame_id)

    async def query_all(
        self,
        target_id: Optional[str],
        selector: str,
        engine: str = "css",
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.backend.query_selector_all(target_id, selector, engine=engine, frame_id=frame_id)

    async def wait_for_selector(
        self,
        target_id: Optional[str],
        selector: str,
        state: str = "visible",
        timeout_ms: int = 30000,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.backend.wait_for_selector(
            target_id, selector, state=state, timeout_ms=timeout_ms, frame_id=frame_id
        )

    async def attributes(self, handle_id: str) -> Dict[str, Any]:
        return await self.backend.get_attributes(handle_id)

    async def text(
        self,
        target_id: Optional[str],
        selector: str,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = await self.backend.get_text(target_id, selector, frame_id=frame_id)
        return {"text": text}

    async def html(
        self,
        target_id: Optional[str],
        selector: str,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        html = await self.backend.get_html(target_id, selector, frame_id=frame_id)
        return {"html": html}

    async def bounding_box(
        self,
        target_id: Optional[str],
        selector: str,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        box = await self.backend.get_bounding_box(target_id, selector, frame_id=frame_id)
        return {"boundingBox": box}

    async def scroll_into_view(
        self,
        target_id: Optional[str],
        selector: str,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.backend.scroll_into_view(target_id, selector, frame_id=frame_id)
        return {}

    async def snapshot(
        self,
        target_id: Optional[str],
        mode: str = "flat",
        max_nodes: int = 10000,
        frame_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Serialize DOM to a flat or accessibility tree snapshot.
        §4.3: dom.snapshot is a first-class method with a stable, versioned schema.
        """
        return await self.backend.dom_snapshot(
            target_id, mode=mode, max_nodes=max_nodes, frame_id=frame_id
        )
