"""MCP stdio server: dynamic tool registry — agent tự viết và gọi tool Python mới.

Agent sinh code Python (hàm ``run(args: dict) -> dict``), đăng ký qua MCP, và gọi
ngay trong cùng turn mà không cần deploy hay restart.

Tool được lưu sang disk tại ``outputs/dynamic_tools/<name>.json`` và load lại khi
helper khởi động. Mỗi lần gọi chạy code trong subprocess Python riêng (timeout 30s).

Tools:
    register_tool(name, description, python_code)  → đăng ký / cập nhật tool
    call_tool(name, arguments_json)                → gọi tool theo tên
    list_tools()                                   → liệt kê tất cả tools đã đăng ký
    delete_tool(name)                              → xoá tool khỏi registry + disk
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
HELPER_PORT = int(os.environ.get("HELPER_PORT", "8766"))
HELPER_BASE = f"http://127.0.0.1:{HELPER_PORT}"

# Chạy code trong venv của project, không phải interpreter hệ thống
_VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not _VENV_PY.exists():
    _VENV_PY = ROOT / "connect-ai" / ".venv" / "Scripts" / "python.exe"
if not _VENV_PY.exists():
    _VENV_PY = Path(sys.executable)  # fallback: interpreter hiện tại

# ── In-memory registry (mirror của helper 8766) ───────────────────────────────
# Bridge dùng helper như nguồn sự thật. Local cache giảm latency cho list_tools.
_TOOLS: dict[str, dict] = {}
_TOOLS_LOCK = threading.Lock()


def _outputs_root() -> Path:
    return Path(os.environ.get("COWORKER_OUTPUT_DIR") or (ROOT / "outputs")).expanduser()


def _tools_dir() -> Path:
    d = _outputs_root() / "dynamic_tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_from_disk() -> None:
    """Load tools đã persist từ lần chạy trước."""
    d = _tools_dir()
    count = 0
    for p in d.glob("*.json"):
        try:
            tool = json.loads(p.read_text(encoding="utf-8"))
            if tool.get("name") and tool.get("code"):
                with _TOOLS_LOCK:
                    _TOOLS[tool["name"]] = tool
                count += 1
        except Exception as e:  # noqa: BLE001
            print(f"[dynamic_tools_mcp] warn: skip {p.name}: {e}", file=sys.stderr)
    if count:
        print(f"[dynamic_tools_mcp] loaded {count} tool(s) from disk", file=sys.stderr)


def _persist(tool: dict) -> None:
    try:
        p = _tools_dir() / f"{tool['name']}.json"
        p.write_text(json.dumps(tool, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[dynamic_tools_mcp] warn: cannot persist {tool['name']}: {e}", file=sys.stderr)


def _run_sandbox(code: str, args: dict, timeout: int = 30) -> dict:
    """Chạy code trong subprocess Python riêng biệt.

    Code phải định nghĩa hàm ``run(args: dict) -> dict``.
    Trả về ``{"ok": True, "result": ...}`` hoặc ``{"ok": False, "error": "..."}``.
    """
    runner = (
        "import sys, json, traceback\n"
        "real_stdout = sys.stdout\n"
        "sys.stdout = sys.stderr\n"
        "args = json.loads(sys.argv[1])\n"
        "try:\n"
        + "\n".join("    " + ln for ln in code.splitlines())
        + "\n"
        "    result = run(args)\n"
        "    real_stdout.write(json.dumps({'ok': True, 'result': result}) + '\\n')\n"
        "except Exception:\n"
        "    real_stdout.write(json.dumps({'ok': False, 'error': traceback.format_exc()}) + '\\n')\n"
    )
    try:
        proc = subprocess.run(
            [str(_VENV_PY), "-", json.dumps(args)],
            input=runner,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
        stdout = proc.stdout.strip()
        if not stdout:
            snippet = (proc.stderr or "").strip()[:500] or "(no output)"
            return {"ok": False, "error": f"tool produced no output. stderr: {snippet}"}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"tool timed out after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"tool output is not valid JSON: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# Load on startup
_load_from_disk()

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("dynamic-tools")


@mcp.tool()
def register_tool(name: str, description: str, python_code: str) -> dict:
    """Đăng ký một Python tool mới (hoặc cập nhật nếu đã tồn tại).

    Tool được lưu vào disk và tồn tại qua restart. Gọi call_tool() ngay sau khi register.

    Args:
        name: Tên tool — chỉ dùng chữ cái, số, dấu gạch dưới (Python identifier).
              Ví dụ: "usd_rate", "parse_shopee_json", "send_weekly_report"
        description: Mô tả ngắn để agent sau biết khi nào nên dùng tool này.
        python_code: Code Python đầy đủ. PHẢI định nghĩa hàm::

                         def run(args: dict) -> dict:
                             ...

                     Có thể import bất kỳ thư viện nào đã cài trong venv (requests,
                     pandas, bs4, ...). Hàm nhận ``args`` dict và phải trả về dict.

    Returns:
        {"ok": True, "tool": name} nếu thành công.
        {"ok": False, "error": "..."} nếu có lỗi validation.

    Example python_code::

        import urllib.request, json

        def run(args: dict) -> dict:
            pair = args.get("pair", "USD/VND")
            base, quote = pair.split("/")
            url = f"https://open.er-api.com/v6/latest/{base}"
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            return {"rate": data["rates"].get(quote), "updated": data.get("time_last_update_utc")}
    """
    # Validate name
    if not name or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        return {
            "ok": False,
            "error": "name phải là Python identifier hợp lệ (chữ/số/gạch dưới, không bắt đầu bằng số)",
        }
    # Validate code có hàm run
    if "def run(" not in python_code and "def run (" not in python_code:
        return {
            "ok": False,
            "error": "python_code phải định nghĩa hàm run(args: dict) -> dict",
            "hint": "Ví dụ:\ndef run(args: dict) -> dict:\n    return {'result': args.get('x', 0) * 2}",
        }

    tool = {
        "name": name,
        "description": description,
        "code": python_code,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with _TOOLS_LOCK:
        _TOOLS[name] = tool
    _persist(tool)
    print(f"[dynamic_tools_mcp] registered: {name}", file=sys.stderr)
    return {
        "ok": True,
        "tool": name,
        "note": f"Tool '{name}' đã đăng ký. Gọi call_tool(name='{name}', arguments_json='{{...}}') ngay bây giờ.",
    }


@mcp.tool()
def call_tool(name: str, arguments_json: str = "{}", timeout_seconds: int = 30) -> dict:
    """Gọi một dynamic tool đã đăng ký và trả về kết quả.

    Tool chạy trong subprocess Python riêng biệt — crash hay lỗi không ảnh hưởng agent.

    Args:
        name: Tên tool (đã register trước đó). Dùng list_tools() để xem danh sách.
        arguments_json: JSON string chứa arguments. Ví dụ: '{"url": "https://...", "limit": 10}'
                        Để trống hoặc '{}' nếu tool không cần tham số.
        timeout_seconds: Thời gian tối đa (giây) cho tool chạy. Mặc định 30, tối đa 120.

    Returns:
        {"ok": True, "result": <dict từ hàm run>} nếu thành công.
        {"ok": False, "error": "..."} nếu tool lỗi hoặc timeout.

    Example::

        call_tool(name="usd_rate", arguments_json='{"pair": "USD/VND"}')
        # → {"ok": True, "result": {"rate": 25450, "updated": "..."}}
    """
    with _TOOLS_LOCK:
        tool = _TOOLS.get(name)

    if not tool:
        with _TOOLS_LOCK:
            available = list(_TOOLS.keys())
        return {
            "ok": False,
            "error": f"tool '{name}' không tồn tại",
            "available_tools": available,
            "hint": "Dùng list_tools() để xem danh sách hoặc register_tool() để tạo mới.",
        }

    # Parse arguments
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"arguments_json không phải JSON hợp lệ: {e}"}

    timeout = max(1, min(int(timeout_seconds), 120))
    print(f"[dynamic_tools_mcp] calling: {name} args={arguments_json[:200]}", file=sys.stderr)
    return _run_sandbox(tool["code"], args, timeout)


@mcp.tool()
def list_tools() -> list[dict]:
    """Liệt kê tất cả dynamic tools đã đăng ký.

    Returns:
        Danh sách tools với name, description, thời gian đăng ký, và số dòng code.

    Example::

        list_tools()
        # → [{"name": "usd_rate", "description": "Tỷ giá hối đoái", ...}, ...]
    """
    with _TOOLS_LOCK:
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "registered_at": t.get("registered_at", ""),
                "code_lines": len(t.get("code", "").splitlines()),
            }
            for t in _TOOLS.values()
        ]


@mcp.tool()
def delete_tool(name: str) -> dict:
    """Xoá một dynamic tool khỏi registry và disk.

    Args:
        name: Tên tool cần xoá.

    Returns:
        {"ok": True, "deleted": name} nếu thành công.
        {"ok": False, "error": "..."} nếu không tìm thấy.
    """
    with _TOOLS_LOCK:
        removed = _TOOLS.pop(name, None)

    if not removed:
        with _TOOLS_LOCK:
            available = list(_TOOLS.keys())
        return {
            "ok": False,
            "error": f"tool '{name}' không tồn tại",
            "available_tools": available,
        }

    # Xoá file persist
    p = _tools_dir() / f"{name}.json"
    p.unlink(missing_ok=True)
    print(f"[dynamic_tools_mcp] deleted: {name}", file=sys.stderr)
    return {"ok": True, "deleted": name}


if __name__ == "__main__":
    print(
        f"[dynamic_tools_mcp] venv_py={_VENV_PY} tools_dir={_tools_dir()}",
        file=sys.stderr,
    )
    mcp.run()
