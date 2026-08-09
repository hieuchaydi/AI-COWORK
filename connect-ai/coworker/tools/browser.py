"""Built-in browser control tools — Playwright-backed, no MCP layer.

Registered like `web_fetch` / `current_time` so every OpenWorker agent has them
without an MCP server round-trip. Persistent user-data-dir keeps cookies /
localStorage across restarts — sign in once, stay signed in.

Sync-facing (each tool blocks the calling thread while the async Playwright
call runs on a dedicated loop). Fine for OpenWorker's tool-per-turn model.

Env overrides:
  BROWSER_HEADLESS=1      run without visible window (default: visible)
  BROWSER_STATE_DIR=path  where to persist cookies (default: ~/.coworker-browser)
"""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aisuite as ai

from ..connectors.tool_defs import approval_for_tool

# Lazy-load Playwright — importing playwright.async_api at module load costs
# ~150-300ms per engine build (agent.py imports this file every session). Defer
# until the first browser_* call so idle sessions pay nothing.
async_playwright = None
PlaywrightTimeout = None
_import_err = None
_playwright_loaded = False


def _ensure_playwright_imported() -> tuple[bool, str]:
    global async_playwright, PlaywrightTimeout, _import_err, _playwright_loaded
    if _playwright_loaded:
        return async_playwright is not None, str(_import_err) if _import_err else ""
    _playwright_loaded = True
    try:
        from playwright.async_api import (
            async_playwright as _ap,
            TimeoutError as _pt,
        )
        async_playwright = _ap
        PlaywrightTimeout = _pt
    except Exception as exc:  # noqa: BLE001
        _import_err = exc
        return False, str(exc)
    return True, ""


def _state_dir() -> Path:
    """Chromium user_data_dir — cookies/localStorage. Kept OUT of project
    outputs so `git clean` / rm outputs/ doesn't log you out of every site."""
    base = os.environ.get("BROWSER_STATE_DIR")
    if base:
        return Path(base).expanduser().resolve()
    return Path.home() / ".coworker-browser"


def _outputs_root() -> Path:
    """Project outputs folder — same one crawl.py writes to. Screenshots land
    in outputs/screenshots/ so users find them alongside CSV/PDF/…"""
    env = os.environ.get("COWORKER_OUTPUT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # browser.py is at openworker/coworker/tools/, parents[3] = project root
    return Path(__file__).resolve().parents[3] / "outputs"


def _screenshots_dir() -> Path:
    d = _outputs_root() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


_OUTPUTS_BASE_URL = os.environ.get(
    "OUTPUTS_BASE_URL", "http://localhost:8766/outputs"
)


HEADLESS = os.environ.get("BROWSER_HEADLESS", "0") == "1"


# ─── Async singleton on a dedicated worker thread ────────────────────────────
# Playwright wants an event loop it fully owns. We spin one up in a background
# thread the first time any tool is called and reuse it forever after.

class _AsyncWorker:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = threading.Lock()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop,
                name="browser-tool-loop",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=5)

    def run(self, coro):
        self.start()
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=120)

    async def _ensure_page(self):
        ok, err = _ensure_playwright_imported()
        if not ok:
            return None, {"error": f"playwright not available: {err}"}
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._context is None:
            _state_dir().mkdir(parents=True, exist_ok=True)
            # Tried driving the user's installed Chrome via channel="chrome" (2026-08-08)
            # on the theory that the bundled-Chromium build is what Shopee scores. Measured
            # side by side on a fresh profile: BOTH land on /verify/traffic/error, so the
            # channel buys nothing here and mixing browser builds over one profile dir risks
            # the saved logins. Reverted deliberately — don't re-add without a measurement.
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(_state_dir()),
                headless=HEADLESS,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
        if self._page is None or self._page.is_closed():
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return self._page, None

    async def _shutdown(self):
        try:
            if self._context is not None:
                await self._context.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._playwright = None
        self._page = None

    def shutdown(self) -> None:
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=10)
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass


_worker = _AsyncWorker()
atexit.register(_worker.shutdown)


def _attach(func: Callable, schema: dict, risk: str = "medium") -> Callable:
    """Match the metadata shape web_fetch/current_time use so the registry accepts it."""
    name = schema["function"]["name"]
    func.__doc__ = schema["function"]["description"]
    func.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=name,
        # "connector" (not "browser"): permissions.py honors session-wide tool
        # allows only for non-connector tools — browser writes must ask EVERY
        # time, never be blanket-allowed for a session (§36 / upstream law).
        category="connector",
        risk_level=risk,
        capabilities=["browser"],
        # §36 law via tool_defs.py: reads never gate, writes ask first. Tools
        # without a registry entry stay approval-free (default False).
        requires_approval=approval_for_tool(name, default=False),
    )
    func.__coworker_schema__ = schema
    return func


def _schema(name: str, description: str, props: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


# ─── Tool implementations ────────────────────────────────────────────────────

def _browser_open(url: str, wait_until: str = "load", timeout_seconds: int = 30) -> dict[str, Any]:
    async def _go():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            r = await page.goto(
                url,
                wait_until=wait_until if wait_until in ("load", "domcontentloaded", "networkidle") else "load",
                timeout=max(1, timeout_seconds) * 1000,
            )
        except PlaywrightTimeout:
            return {"error": f"timeout after {timeout_seconds}s loading {url}"}
        except Exception as exc:
            return {"error": f"navigation failed: {exc}"}
        return {
            "ok": True,
            "final_url": page.url,
            "title": await page.title(),
            "status": r.status if r else None,
        }
    return _worker.run(_go())


def _browser_read(max_chars: int = 8000, format: str = "text") -> dict[str, Any]:
    async def _r():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            if format == "html":
                content = await page.content()
            else:
                content = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception as exc:
            return {"error": f"read failed: {exc}"}
        cap = max(500, min(int(max_chars or 8000), 100000))
        return {
            "url": page.url,
            "title": await page.title(),
            "text": (content or "")[:cap],
            "truncated": len(content or "") > cap,
            "total_chars": len(content or ""),
        }
    return _worker.run(_r())


def _browser_read_selector(selector: str, max_chars: int = 4000) -> dict[str, Any]:
    async def _r():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            loc = page.locator(selector)
            count = await loc.count()
            if count == 0:
                return {"error": f"no element matches {selector!r}"}
            texts = []
            for i in range(min(count, 20)):
                try:
                    texts.append((await loc.nth(i).inner_text())[:max_chars])
                except Exception:
                    pass
            return {"selector": selector, "count": count, "items": texts}
        except Exception as exc:
            return {"error": f"read_selector failed: {exc}"}
    return _worker.run(_r())


def _browser_click(target: str, by: str = "selector", timeout_seconds: int = 10) -> dict[str, Any]:
    async def _c():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            if by == "text":
                loc = page.get_by_text(target, exact=False)
            elif by == "role":
                role, _, name = target.partition(":")
                loc = page.get_by_role(role.strip(), name=name.strip() or None)
            else:
                loc = page.locator(target)
            await loc.first.click(timeout=max(1, timeout_seconds) * 1000)
            return {"ok": True, "clicked": target, "by": by, "url_after": page.url}
        except PlaywrightTimeout:
            return {"error": f"element not clickable in {timeout_seconds}s: {target}"}
        except Exception as exc:
            return {"error": f"click failed: {exc}"}
    return _worker.run(_c())


def _browser_fill(selector: str, value: str, submit: bool = False, timeout_seconds: int = 10) -> dict[str, Any]:
    async def _f():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            loc = page.locator(selector).first
            await loc.fill(value, timeout=max(1, timeout_seconds) * 1000)
            if submit:
                await loc.press("Enter")
            return {"ok": True, "filled": selector, "chars": len(value)}
        except Exception as exc:
            return {"error": f"fill failed: {exc}"}
    return _worker.run(_f())


def _browser_press(key: str) -> dict[str, Any]:
    async def _p():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            await page.keyboard.press(key)
            return {"ok": True, "key": key}
        except Exception as exc:
            return {"error": f"press failed: {exc}"}
    return _worker.run(_p())


def _browser_screenshot(full_page: bool = False, save_to: str = "") -> dict[str, Any]:
    async def _s():
        page, err = await _worker._ensure_page()
        if err:
            return err
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"screenshot-{stamp}.png"
        under_outputs = False
        if save_to:
            target = Path(save_to).expanduser().resolve()
        else:
            target = _screenshots_dir() / name
            under_outputs = True
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(target), full_page=bool(full_page))
        except Exception as exc:
            return {"error": f"screenshot failed: {exc}"}
        result = {
            "path": str(target),
            "size": target.stat().st_size,
            "current_url": page.url,
        }
        if under_outputs:
            result["url"] = f"{_OUTPUTS_BASE_URL}/screenshots/{target.name}"
        return result
    return _worker.run(_s())


def _browser_wait_for(selector: str, timeout_seconds: int = 15, state: str = "visible") -> dict[str, Any]:
    async def _w():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            await page.locator(selector).first.wait_for(
                state=state if state in ("visible", "hidden", "attached", "detached") else "visible",
                timeout=max(1, timeout_seconds) * 1000,
            )
            return {"ok": True, "selector": selector, "state": state}
        except PlaywrightTimeout:
            return {"error": f"not {state} after {timeout_seconds}s: {selector}"}
    return _worker.run(_w())


def _browser_evaluate(js: str) -> dict[str, Any]:
    async def _e():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            r = await page.evaluate(js)
            return {"ok": True, "result": r}
        except Exception as exc:
            return {"error": f"evaluate failed: {exc}"}
    return _worker.run(_e())


def _browser_get_links(filter: str = "", limit: int = 50) -> dict[str, Any]:
    async def _g():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            links = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map(a => ({href: a.href, text: (a.innerText||'').trim().slice(0,200)}))"
            )
        except Exception as exc:
            return {"error": f"get_links failed: {exc}"}
        if filter:
            f = filter.lower()
            links = [l for l in links if f in (l["href"] or "").lower() or f in (l["text"] or "").lower()]
        return {"count": len(links), "links": links[: max(1, min(int(limit or 50), 500))]}
    return _worker.run(_g())


def _browser_get_forms() -> dict[str, Any]:
    async def _f():
        page, err = await _worker._ensure_page()
        if err:
            return err
        try:
            forms = await page.evaluate("""
                () => Array.from(document.querySelectorAll('form')).map((f, fi) => ({
                    index: fi, action: f.action, method: f.method,
                    inputs: Array.from(f.querySelectorAll('input,textarea,select,button')).map(i => ({
                        tag: i.tagName.toLowerCase(),
                        type: i.type || null, name: i.name || null, id: i.id || null,
                        placeholder: i.placeholder || null,
                        value: i.tagName === 'BUTTON' ? (i.innerText || '').trim() : (i.value || '').slice(0, 50)
                    }))
                }))
            """)
        except Exception as exc:
            return {"error": f"get_forms failed: {exc}"}
        return {"count": len(forms), "forms": forms}
    return _worker.run(_f())


def _browser_current_url() -> dict[str, Any]:
    async def _c():
        page, err = await _worker._ensure_page()
        if err:
            return err
        return {"url": page.url, "title": await page.title()}
    return _worker.run(_c())


def _browser_close() -> dict[str, Any]:
    async def _c():
        await _worker._shutdown()
        return {"ok": True}
    return _worker.run(_c())


# ─── Factory (called by agent.py alongside make_web_fetch_tool etc.) ─────────

def make_browser_tools() -> list[Callable[..., Any]]:
    """Return the 13 browser tools wrapped with schemas + metadata."""
    tools = []

    def _add(fn, name, desc, props, required, risk="medium"):
        fn.__name__ = name
        tools.append(_attach(fn, _schema(name, desc, props, required), risk))

    _add(
        _browser_open, "browser_open",
        "Điều hướng browser tới URL. Persistent Chromium — cookies + login state giữ giữa các lần gọi. Trả title, final URL (sau khi follow redirect), HTTP status. Gọi tool này trước khi read/click/fill trên page mới.",
        {
            "url": {"type": "string", "description": "http(s):// URL to navigate to"},
            "wait_until": {"type": "string", "description": "load | domcontentloaded | networkidle (default load)"},
            "timeout_seconds": {"type": "integer", "description": "default 30"},
        },
        ["url"],
    )
    _add(
        _browser_read, "browser_read",
        "Đọc text content của trang hiện tại (mặc định) — đã strip HTML, như reader mode. Đặt format='html' để lấy raw outerHTML. Dùng để hiểu nội dung page hoặc trích data.",
        {
            "max_chars": {"type": "integer", "description": "default 8000, max 100000"},
            "format": {"type": "string", "description": "text (default) | html"},
        },
        [],
        risk="low",
    )
    _add(
        _browser_read_selector, "browser_read_selector",
        "Đọc text của 1 element theo CSS selector (hoặc Playwright syntax: text=... / role=...). Precise pull dữ liệu — giá, tiêu đề, số liệu — thay vì đọc cả trang.",
        {
            "selector": {"type": "string", "description": "CSS/Playwright selector"},
            "max_chars": {"type": "integer"},
        },
        ["selector"],
        risk="low",
    )
    _add(
        _browser_click, "browser_click",
        "Click 1 element. `by`='selector' (CSS, default) | 'text' (visible label) | 'role' (ARIA, format 'button:Submit'). Trả URL sau click (biết được đã navigate hay chưa).",
        {
            "target": {"type": "string"},
            "by": {"type": "string", "description": "selector (default) | text | role"},
            "timeout_seconds": {"type": "integer"},
        },
        ["target"],
    )
    _add(
        _browser_fill, "browser_fill",
        "Điền input / textarea. `submit=True` → gõ Enter sau đó (submit form). Auto-clear giá trị cũ trước khi type.",
        {
            "selector": {"type": "string", "description": "CSS selector of input"},
            "value": {"type": "string"},
            "submit": {"type": "boolean", "description": "press Enter after fill"},
            "timeout_seconds": {"type": "integer"},
        },
        ["selector", "value"],
    )
    _add(
        _browser_press, "browser_press",
        "Gửi 1 phím cho browser (Enter, Escape, Tab, ArrowDown, Control+A, ...). Xem key names Playwright docs.",
        {"key": {"type": "string"}},
        ["key"],
    )
    _add(
        _browser_screenshot, "browser_screenshot",
        "Chụp screenshot browser. `full_page=True` capture cả scroll (dài). `save_to`= absolute path hoặc rỗng (auto vào outputs/screenshots/, có URL xem được). Trả path + size.",
        {
            "full_page": {"type": "boolean"},
            "save_to": {"type": "string"},
        },
        [],
        risk="low",
    )
    _add(
        _browser_wait_for, "browser_wait_for",
        "Chờ element xuất hiện / mất / attached. `state` ∈ visible | hidden | attached | detached. Trước khi click element load bằng JS async.",
        {
            "selector": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
            "state": {"type": "string"},
        },
        ["selector"],
        risk="low",
    )
    _add(
        _browser_evaluate, "browser_evaluate",
        "Chạy JavaScript trong context page. Trả giá trị JS trả về (phải JSON-serializable). Vd: browser_evaluate('document.title') → {result:'Google'}. HIGH RISK — không sandbox.",
        {"js": {"type": "string"}},
        ["js"],
        risk="high",
    )
    _add(
        _browser_get_links, "browser_get_links",
        "Liệt kê mọi `<a href>` trên page. `filter` = substring lọc theo href hoặc text (case-insensitive).",
        {"filter": {"type": "string"}, "limit": {"type": "integer"}},
        [],
        risk="low",
    )
    _add(
        _browser_get_forms, "browser_get_forms",
        "List forms + inputs trên page — dùng TRƯỚC khi gọi fill/click để biết selector chính xác.",
        {},
        [],
        risk="low",
    )
    _add(
        _browser_current_url, "browser_current_url",
        "URL + title hiện tại.",
        {},
        [],
        risk="low",
    )
    _add(
        _browser_close, "browser_close",
        "Đóng browser + free RAM. Cookies trên disk giữ nguyên — lần open kế tiếp vẫn login.",
        {},
        [],
        risk="low",
    )

    return tools
