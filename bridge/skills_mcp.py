"""MCP stdio server: expose reusable "skills" from `skills/*.md` files.

Each skill file has YAML frontmatter (name, description, triggers) + a Markdown
body containing the instructions the agent should follow when the skill is
loaded. Agent calls `list_skills()` to browse, `load_skill(name)` to inject the
body into its own context, or `suggest_skill(text)` to auto-match by triggers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Simple YAML frontmatter parser. Returns (meta_dict, body)."""
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
    if not SKILLS_DIR.is_dir():
        return []
    out = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        try:
            meta, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "name": meta.get("name") or p.stem,
                "description": meta.get("description", ""),
                "triggers": meta.get("triggers", []) if isinstance(meta.get("triggers"), list) else [],
                "file": p.name,
                "body": body,
                "body_preview": body[:200],
            }
        )
    return out


mcp = FastMCP("skills")


@mcp.tool()
def list_skills() -> list[dict]:
    """List every skill available in `skills/` folder. Returns name, description,
    trigger keywords, file. Body NOT included — use `load_skill(name)` to fetch."""
    return [
        {k: v for k, v in s.items() if k not in ("body",)}
        for s in _load_all()
    ]


@mcp.tool()
def load_skill(name: str) -> dict:
    """Load a specific skill by name — returns the full instruction body to inject
    into your context. Follow the body's guidelines when doing the task."""
    for s in _load_all():
        if s["name"].lower() == name.lower() or s["file"].startswith(name):
            return {
                "name": s["name"],
                "description": s["description"],
                "body": s["body"],
            }
    return {"error": f"skill '{name}' not found. Try list_skills() first."}


@mcp.tool()
def suggest_skill(text: str) -> list[dict]:
    """Auto-suggest skills whose triggers match `text`. Case-insensitive substring
    match on trigger keywords. Returns ranked list."""
    needle = text.lower()
    matches = []
    for s in _load_all():
        score = 0
        for trig in s.get("triggers", []):
            if trig.lower() in needle:
                score += 2
            elif any(t in needle for t in trig.lower().split()):
                score += 1
        if score:
            matches.append({"name": s["name"], "score": score, "description": s["description"]})
    matches.sort(key=lambda x: -x["score"])
    return matches


if __name__ == "__main__":
    print(f"[skills_mcp] SKILLS_DIR={SKILLS_DIR}", file=sys.stderr)
    mcp.run()
