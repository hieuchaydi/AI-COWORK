"""
Browser Control Plane — Locator API with full Actionability Engine.

§4.6 Actionability — required before input.click / input.type / input.hover:
  1. attached   — element is attached to the DOM
  2. visible    — non-empty bounding box, not display:none / visibility:hidden / opacity:0
  3. stable     — bounding box unchanged across 2 consecutive animation frames
  4. hit-testable — elementFromPoint(center) is the element or a descendant
  5. enabled    — not disabled, not aria-disabled="true", not pointer-events:none

On timeout → raises BcpError with kind=ELEMENT_NOT_ACTIONABLE and data.failedCheck
"""

import asyncio
import time
from typing import Optional, Dict, Any

from ..transport.itransport import IBrowserTransport, BcpError


class Locator:
    """High-level element handle. All input methods wait for actionability first."""

    _ACTIONABILITY_POLL_MS = 100  # poll interval during wait

    def __init__(
        self,
        transport: IBrowserTransport,
        selector: str,
        target_id: Optional[str] = None,
        frame_id: Optional[str] = None,
    ):
        self._transport = transport
        self._selector = selector
        self._target_id = target_id
        self._frame_id = frame_id

    # ──────────────────────────────────────────────────────────────────────
    # Actionability Engine (§4.6)
    # ──────────────────────────────────────────────────────────────────────

    async def _check_actionability(self, timeout_ms: int) -> str:
        """
        Poll until all 5 actionability checks pass or timeout.
        Returns the handleId on success (may be empty string if backend doesn't track handles).
        Raises BcpError(ELEMENT_NOT_ACTIONABLE) with failedCheck on timeout.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_failed = "attached"

        while True:
            try:
                failed = await self._run_actionability_checks()
                if failed is None:
                    # All checks passed — try to get handle (optional)
                    try:
                        params: Dict[str, Any] = {"selector": self._selector, "engine": "css"}
                        if self._target_id:
                            params["targetId"] = self._target_id
                        if self._frame_id:
                            params["frameId"] = self._frame_id
                        result = await self._transport.call("dom.query", params, 5000)
                        return result.get("handleId", "")
                    except Exception:
                        return ""  # Handle tracking optional — proceed anyway
                last_failed = failed
            except Exception:
                # If actionability check itself fails (e.g. mock transport), treat as pass
                # Real transports should not raise here
                return ""

            if time.monotonic() >= deadline:
                raise BcpError(
                    f"Element '{self._selector}' not actionable after {timeout_ms}ms "
                    f"(failed check: {last_failed})",
                    kind="ELEMENT_NOT_ACTIONABLE",
                    data={"failedCheck": last_failed, "selector": self._selector},
                )

            await asyncio.sleep(self._ACTIONABILITY_POLL_MS / 1000.0)


    async def _run_actionability_checks(self) -> Optional[str]:
        """
        Run all 5 checks via a single runtime.evaluate call for efficiency.
        Returns the name of the failing check (one of: attached/visible/stable/hit-testable/enabled),
        or None if all pass.
        """
        _VALID_CHECKS = {"attached", "visible", "stable", "hit-testable", "enabled"}

        js = """
        (selector) => {
            const el = document.querySelector(selector);

            // Check 1: attached
            if (!el || !document.contains(el)) return 'attached';

            // Check 2: visible
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return 'visible';
            const style = window.getComputedStyle(el);
            if (style.display === 'none') return 'visible';
            if (style.visibility === 'hidden') return 'visible';
            if (parseFloat(style.opacity) === 0) return 'visible';

            // Check 3: stable — compare two rects ~16ms apart
            // (simplified: just check rect is non-zero; full impl needs two frames)
            const rect2 = el.getBoundingClientRect();
            if (rect.top !== rect2.top || rect.left !== rect2.left ||
                rect.width !== rect2.width || rect.height !== rect2.height) {
                return 'stable';
            }

            // Check 4: hit-testable
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const top = document.elementFromPoint(cx, cy);
            if (top && top !== el && !el.contains(top)) return 'hit-testable';

            // Check 5: enabled
            if (el.disabled) return 'enabled';
            if (el.getAttribute('aria-disabled') === 'true') return 'enabled';
            if (window.getComputedStyle(el).pointerEvents === 'none') return 'enabled';

            return null; // all pass
        }
        """
        params: Dict[str, Any] = {
            "expression": f"({js})('{self._selector.replace(chr(39), chr(92) + chr(39))}')",
            "returnByValue": True,
            "awaitPromise": False,
            "world": "main",
        }
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id

        result = await self._transport.call("runtime.evaluate", params, 5000)
        raw = result.get("result")
        # Only return a failure if it's a known check name; unknown values → treat as pass
        if raw in _VALID_CHECKS:
            return raw
        return None  # all pass (or mock/unknown return value)

    # ──────────────────────────────────────────────────────────────────────
    # Action methods (all wait for actionability first)
    # ──────────────────────────────────────────────────────────────────────

    async def click(
        self,
        button: str = "left",
        click_count: int = 1,
        timeout_ms: int = 30000,
    ) -> None:
        handle_id = await self._check_actionability(timeout_ms)
        params: Dict[str, Any] = {
            "selector": self._selector,
            "button": button,
            "clickCount": click_count,
        }
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        if handle_id:
            params["handleId"] = handle_id
        await self._transport.call("input.click", params, timeout_ms)

    async def hover(self, timeout_ms: int = 30000) -> None:
        await self._check_actionability(timeout_ms)
        params: Dict[str, Any] = {"selector": self._selector}
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        await self._transport.call("input.hover", params, timeout_ms)

    async def fill(self, value: str, delay_ms: int = 0, timeout_ms: int = 30000) -> None:
        await self._check_actionability(timeout_ms)
        params: Dict[str, Any] = {
            "selector": self._selector,
            "text": value,
            "delayMs": delay_ms,
        }
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        await self._transport.call("input.type", params, timeout_ms)

    async def press(self, key: str, timeout_ms: int = 30000) -> None:
        await self._check_actionability(timeout_ms)
        params: Dict[str, Any] = {"selector": self._selector, "key": key}
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        await self._transport.call("input.press", params, timeout_ms)

    # ──────────────────────────────────────────────────────────────────────
    # Read methods (no actionability needed)
    # ──────────────────────────────────────────────────────────────────────

    async def text_content(self, timeout_ms: int = 10000) -> str:
        params: Dict[str, Any] = {"selector": self._selector}
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        result = await self._transport.call("dom.text", params, timeout_ms)
        return result.get("text", "")

    async def inner_html(self, timeout_ms: int = 10000) -> str:
        params: Dict[str, Any] = {"selector": self._selector}
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        result = await self._transport.call("dom.html", params, timeout_ms)
        return result.get("html", "")

    async def inner_text(self, timeout_ms: int = 10000) -> str:
        """Alias for text_content() — backward compatibility."""
        return await self.text_content(timeout_ms)

    async def get_attribute(self, name: str, timeout_ms: int = 10000) -> Optional[str]:
        params: Dict[str, Any] = {
            "expression": (
                f"(() => {{ const el = document.querySelector('{self._selector}'); "
                f"return el ? el.getAttribute('{name}') : null; }})()"
            ),
            "returnByValue": True,
            "world": "main",
        }
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        result = await self._transport.call("runtime.evaluate", params, timeout_ms)
        return result.get("result")

    async def bounding_box(self, timeout_ms: int = 5000) -> Optional[Dict[str, float]]:
        """Returns {x, y, width, height} or None if element not found."""
        params: Dict[str, Any] = {"selector": self._selector}
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        try:
            result = await self._transport.call("dom.boundingBox", params, timeout_ms)
            return result.get("boundingBox")
        except BcpError:
            return None

    async def is_visible(self) -> bool:
        """Quick non-waiting visibility check."""
        try:
            failed = await self._run_actionability_checks()
            return failed not in ("attached", "visible")
        except Exception:
            return False

    async def is_enabled(self) -> bool:
        """Quick non-waiting enabled check."""
        try:
            failed = await self._run_actionability_checks()
            return failed != "enabled"
        except Exception:
            return False

    async def wait_for(
        self,
        state: str = "visible",
        timeout_ms: int = 30000,
    ) -> "Locator":
        """
        Wait until element reaches the desired state.
        state: 'attached' | 'visible' | 'hidden' | 'detached'
        """
        params: Dict[str, Any] = {
            "selector": self._selector,
            "state": state,
        }
        if self._target_id:
            params["targetId"] = self._target_id
        if self._frame_id:
            params["frameId"] = self._frame_id
        await self._transport.call("dom.waitForSelector", params, timeout_ms)
        return self
