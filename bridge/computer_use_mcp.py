"""MCP stdio server: computer use — click/type/screenshot via pyautogui.

⚠️ DANGEROUS: mọi tool ở đây điều khiển trực tiếp chuột/bàn phím máy user. Bridge
CHỈ nên chạy khi user chủ động cần automate GUI app không có API. Mỗi tool có
`safety_delay=0.5s` giữa các action + `FAILSAFE=True` (di chuột góc trên trái để
abort).

Tools:
    screenshot(region?) — save screenshot to artifacts/, return path + url
    get_screen_size()
    mouse_click(x, y, button, clicks)
    mouse_move(x, y, duration)
    mouse_drag(x1, y1, x2, y2)
    keyboard_type(text, interval)
    keyboard_hotkey(keys)
    keyboard_press(key)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

try:
    import pyautogui  # type: ignore

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.3  # global delay between actions
except Exception as exc:  # noqa: BLE001
    pyautogui = None
    _import_err = exc
else:
    _import_err = None

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = ROOT / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("ARTIFACTS_BASE_URL", "http://localhost:8766/artifacts")


def _check() -> Optional[dict]:
    if pyautogui is None:
        return {"error": f"pyautogui not available: {_import_err}"}
    return None


mcp = FastMCP("computer-use")


@mcp.tool()
def screenshot(x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> dict:
    """Capture screen. Nếu tất cả tham số = 0 → full screen. Ngược lại capture 1
    vùng `(x, y, width, height)` (pixel, top-left origin). Lưu vào artifacts/
    dưới tên `screen-<timestamp>.png`, trả path + URL để user xem."""
    if err := _check():
        return err
    fname = f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    fpath = ARTIFACTS_DIR / fname
    if width and height:
        img = pyautogui.screenshot(region=(x, y, width, height))
    else:
        img = pyautogui.screenshot()
    img.save(fpath)
    w, h = img.size
    return {
        "path": str(fpath),
        "url": f"{BASE_URL}/{fname}",
        "size": {"width": w, "height": h},
    }


@mcp.tool()
def get_screen_size() -> dict:
    """Return kích thước màn hình chính (pixels)."""
    if err := _check():
        return err
    w, h = pyautogui.size()
    return {"width": w, "height": h}


@mcp.tool()
def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
    """Click chuột tại `(x, y)`. `button` = "left" | "right" | "middle". `clicks`
    = 1 (single) hoặc 2 (double). Có delay 0.3s trước khi click để user thấy được
    hành động."""
    if err := _check():
        return err
    time.sleep(0.3)
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    return {"ok": True, "at": [x, y], "button": button, "clicks": clicks}


@mcp.tool()
def mouse_move(x: int, y: int, duration: float = 0.3) -> dict:
    """Di chuột tới `(x, y)` với animation `duration` giây."""
    if err := _check():
        return err
    pyautogui.moveTo(x, y, duration=duration)
    return {"ok": True, "at": [x, y]}


@mcp.tool()
def mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, button: str = "left") -> dict:
    """Drag từ `(x1, y1)` đến `(x2, y2)`, giữ button `button`."""
    if err := _check():
        return err
    pyautogui.moveTo(x1, y1, duration=0.3)
    pyautogui.dragTo(x2, y2, duration=duration, button=button)
    return {"ok": True, "from": [x1, y1], "to": [x2, y2]}


@mcp.tool()
def keyboard_type(text: str, interval: float = 0.05) -> dict:
    """Gõ text (như typing). `interval` = delay giây giữa từng ký tự (0.05 = fast).
    Ký tự đặc biệt (VD Enter, Tab) dùng `keyboard_hotkey` hoặc `keyboard_press`."""
    if err := _check():
        return err
    pyautogui.typewrite(text, interval=interval)
    return {"ok": True, "typed_chars": len(text)}


@mcp.tool()
def keyboard_hotkey(keys: list) -> dict:
    """Gõ combo phím. VD `["ctrl", "c"]` = Ctrl+C, `["alt", "tab"]` = Alt+Tab,
    `["win", "e"]` = Win+E. Key names lowercase (xem pyautogui docs)."""
    if err := _check():
        return err
    pyautogui.hotkey(*keys)
    return {"ok": True, "keys": keys}


@mcp.tool()
def keyboard_press(key: str, presses: int = 1) -> dict:
    """Nhấn 1 phím `presses` lần. VD `key="enter"`, `key="tab"`, `key="esc"`."""
    if err := _check():
        return err
    pyautogui.press(key, presses=presses)
    return {"ok": True, "key": key, "presses": presses}


if __name__ == "__main__":
    print(f"[computer_use_mcp] pyautogui={'ok' if pyautogui else _import_err}", file=sys.stderr)
    mcp.run()
