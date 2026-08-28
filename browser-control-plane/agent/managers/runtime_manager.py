"""
Runtime Manager — runtime.* methods.
Manages JS evaluation, handle lifecycle, init scripts, execution worlds.
"""
from typing import Any, Dict, List, Optional


class RuntimeManager:
    def __init__(self, backend):
        self.backend = backend

    async def evaluate(
        self,
        target_id: Optional[str],
        expression: str,
        frame_id: Optional[str] = None,
        world: str = "main",
        await_promise: bool = False,
        return_by_value: bool = True,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        return await self.backend.evaluate(
            target_id, expression,
            frame_id=frame_id,
            world=world,
            await_promise=await_promise,
            return_by_value=return_by_value,
            timeout_ms=timeout_ms,
        )

    async def call_function(
        self,
        handle_id: str,
        function_declaration: str,
        args: List[Any],
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        return await self.backend.call_function(
            handle_id, function_declaration, args, timeout_ms=timeout_ms
        )

    async def release_handle(self, handle_id: str) -> Dict[str, Any]:
        await self.backend.release_handle(handle_id)
        return {}

    async def add_init_script(self, source: str, world: str = "main") -> Dict[str, Any]:
        await self.backend.add_init_script(source, world)
        return {}
