"""Dynamic custom tools auto-discovery and loader.

Scans designated custom tool directories (e.g. `custom_tools/` in workspace root),
loads Python modules, and extracts functions decorated with `@coworker_tool`.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def get_custom_tool_dirs(workspace: Optional[Path] = None) -> list[Path]:
    """Return all directories scanned for custom tools."""
    dirs: list[Path] = []

    # 1. Project root `custom_tools/`
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    root_custom = repo_root / "custom_tools"
    if root_custom.exists() and root_custom.is_dir():
        dirs.append(root_custom)

    # 2. Workspace root `custom_tools/` (if different from repo root)
    if workspace is not None:
        ws_custom = workspace / "custom_tools"
        if ws_custom.exists() and ws_custom.is_dir() and ws_custom not in dirs:
            dirs.append(ws_custom)
        ws_hidden = workspace / ".coworker" / "tools"
        if ws_hidden.exists() and ws_hidden.is_dir() and ws_hidden not in dirs:
            dirs.append(ws_hidden)

    # 3. Environment variable override
    env_dir = os.environ.get("COWORKER_CUSTOM_TOOLS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists() and p.is_dir() and p not in dirs:
            dirs.append(p)

    return dirs


def discover_custom_tools(workspace: Optional[Path] = None) -> list[Callable[..., Any]]:
    """Scan custom tool directories and return all discovered tool callables."""
    tools: list[Callable[..., Any]] = []
    seen_names: set[str] = set()
    dirs = get_custom_tool_dirs(workspace)

    for directory in dirs:
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith(("_", ".")):
                continue

            module_name = f"coworker_custom_tool_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # Search module attributes for tools
                for attr_name in dir(module):
                    if attr_name.startswith("_"):
                        continue
                    obj = getattr(module, attr_name)
                    if callable(obj) and hasattr(obj, "__coworker_tool_metadata__"):
                        tool_name = getattr(obj, "__name__", attr_name)
                        if tool_name not in seen_names:
                            seen_names.add(tool_name)
                            tools.append(obj)
                            logger.info("Discovered custom tool: %s (%s)", tool_name, py_file.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load custom tool from %s: %s", py_file, exc)

    return tools
