"""MCP stdio server: user-defined slash commands.

Đọc `commands/*.md` — mỗi file 1 command với frontmatter (name, description,
args) và body = template prompt (Python format string {arg1} {arg2} ...).

Agent gọi `list_commands()` để xem list, `run_command(name, args)` để lấy prompt
đã substitute rồi execute.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip()
    body = text[end + 4:].lstrip()
    meta: dict = {}
    for line in header.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [s.strip().strip('"').strip("'") for s in v[1:-1].split(",") if s.strip()]
        else:
            v = v.strip('"').strip("'")
        meta[k.strip()] = v
    return meta, body


def _load_all() -> list[dict]:
    if not COMMANDS_DIR.is_dir():
        return []
    out = []
    for p in sorted(COMMANDS_DIR.glob("*.md")):
        try:
            meta, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        args_field = meta.get("args", [])
        if isinstance(args_field, str):
            args_field = [a.strip() for a in args_field.split(",") if a.strip()]
        out.append(
            {
                "name": meta.get("name") or p.stem,
                "description": meta.get("description", ""),
                "args": args_field,
                "file": p.name,
                "template": body,
            }
        )
    return out


mcp = FastMCP("commands")


@mcp.tool()
def list_commands() -> list[dict]:
    """List mọi command trong `commands/` folder. Trả name, description, args yêu
    cầu, file. Body template KHÔNG include."""
    return [
        {k: v for k, v in c.items() if k != "template"}
        for c in _load_all()
    ]


@mcp.tool()
def run_command(name: str, args: dict = None) -> dict:
    """Render 1 command template với args. Trả prompt final để agent execute như
    prompt bình thường. Nếu args thiếu key mà template cần → prompt hiển thị
    `{missing_arg}` giữ nguyên (agent phải hỏi user)."""
    args = args or {}
    for c in _load_all():
        if c["name"].lower() == name.lower():
            try:
                rendered = c["template"].format_map(_SafeDict(args))
            except Exception as exc:
                return {"error": f"format error: {exc}"}
            return {
                "name": c["name"],
                "description": c["description"],
                "required_args": c["args"],
                "provided_args": args,
                "prompt": rendered,
            }
    return {"error": f"command '{name}' not found. Use list_commands() first."}


class _SafeDict(dict):
    """format_map that leaves {missing} untouched instead of raising KeyError."""

    def __missing__(self, key):
        return "{" + key + "}"


if __name__ == "__main__":
    print(f"[commands_mcp] COMMANDS_DIR={COMMANDS_DIR}", file=sys.stderr)
    mcp.run()
