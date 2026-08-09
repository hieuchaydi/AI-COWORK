"""MCP stdio server: publish local artifacts (HTML/MD/JSON) accessible via HTTP.

Writes to `artifacts/` folder in project root. Launch.py's helper HTTP (:8766)
serves files from that folder under `/artifacts/<filename>`, so agent can hand
user a URL like `http://localhost:8766/artifacts/report.html` for viewing.

Tools:
    publish_artifact(name, content, type)
    list_artifacts()
    get_artifact_url(name)
    delete_artifact(name)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = ROOT / "artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("ARTIFACTS_BASE_URL", "http://localhost:8766/artifacts")

EXT_MAP = {
    "html": ".html",
    "md": ".md",
    "markdown": ".md",
    "json": ".json",
    "txt": ".txt",
    "csv": ".csv",
    "svg": ".svg",
}


def _safe_name(name: str, ext: str) -> str:
    """Sanitize filename — strip path components, keep only ascii alnum + -_."""
    stem = Path(name).stem  # drop dirs
    stem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)[:80]
    stem = stem or "artifact"
    return f"{stem}{ext}"


mcp = FastMCP("artifacts")


@mcp.tool()
def publish_artifact(name: str, content: str, type: str = "html") -> dict:
    """Ghi 1 artifact file vào folder `artifacts/`, expose qua HTTP tại
    `http://localhost:8766/artifacts/<file>`. Trả URL để share cho user hoặc paste
    vào chat khác.

    `type`: "html" | "md" | "json" | "txt" | "csv" | "svg"
    `name`: filename gợi ý (auto-sanitize + gắn extension)."""
    ext = EXT_MAP.get(type.lower())
    if ext is None:
        return {"error": f"unsupported type '{type}'. Use: {list(EXT_MAP)}"}
    fname = _safe_name(name, ext)
    path = ARTIFACTS_DIR / fname
    path.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "name": fname,
        "path": str(path),
        "url": f"{BASE_URL}/{fname}",
        "size_bytes": len(content.encode("utf-8")),
        "published_at": datetime.now().isoformat(timespec="seconds"),
    }


@mcp.tool()
def list_artifacts() -> list[dict]:
    """List mọi artifact đã publish, sắp theo ngày sửa mới nhất."""
    if not ARTIFACTS_DIR.is_dir():
        return []
    files = list(ARTIFACTS_DIR.iterdir())
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "url": f"{BASE_URL}/{p.name}",
            "size_bytes": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for p in files
        if p.is_file()
    ]


@mcp.tool()
def get_artifact_url(name: str) -> dict:
    """Get URL cho 1 artifact đã publish. Trả error nếu không tồn tại."""
    fname = name if name.startswith(("artifact", "report")) or "." in name else _safe_name(name, ".html")
    # search by exact or stem match
    for p in ARTIFACTS_DIR.iterdir():
        if p.is_file() and (p.name == name or p.stem == Path(name).stem):
            return {
                "url": f"{BASE_URL}/{p.name}",
                "name": p.name,
                "path": str(p),
            }
    return {"error": f"artifact '{name}' not found. Use list_artifacts() to see options."}


@mcp.tool()
def delete_artifact(name: str) -> dict:
    """Xóa artifact khỏi folder + URL không truy cập được nữa."""
    for p in ARTIFACTS_DIR.iterdir():
        if p.is_file() and (p.name == name or p.stem == Path(name).stem):
            p.unlink()
            return {"ok": True, "deleted": p.name}
    return {"error": f"artifact '{name}' not found"}


if __name__ == "__main__":
    print(f"[artifacts_mcp] dir={ARTIFACTS_DIR} base={BASE_URL}", file=sys.stderr)
    mcp.run()
