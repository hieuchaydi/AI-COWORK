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


def reload_custom_tools(registry: Any, workspace: Optional[Path] = None) -> list[str]:
    """Scan custom tool directories and register any new/updated tools into the registry."""
    discovered = discover_custom_tools(workspace)
    added: list[str] = []
    for tool_fn in discovered:
        name = getattr(tool_fn, "__name__", None)
        if name:
            registry.register(tool_fn)
            added.append(name)
    return added


def make_create_custom_tool(registry: Any, workspace: Optional[Path] = None) -> Callable[..., Any]:
    """Return a tool function that allows the agent to create and hot-register custom tools."""
    import ast
    import re
    from .base import coworker_tool, tool_success, tool_error

    @coworker_tool(
        name="create_custom_tool",
        category="core",
        requires_approval=True,
        description=(
            "Tạo và nạp một công cụ Python mới vào hệ thống ngay lập tức (hot-reload). "
            "Code Python phải định nghĩa hàm cần tạo."
        ),
    )
    def create_custom_tool(
        name: str,
        code: str,
        description: str = "",
        requires_approval: bool = False,
    ) -> dict[str, Any]:
        cleaned_name = name.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]*$", cleaned_name):
            return tool_error("Tool name must be valid snake_case (lowercase letters, numbers, underscores).")

        # 1. AST syntax check
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return tool_error(f"Python syntax error in tool code: {exc}")

        # 2. Determine target custom_tools directory
        target_dir = (workspace / "custom_tools") if workspace else (
            Path(__file__).resolve().parent.parent.parent.parent / "custom_tools"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{cleaned_name}.py"

        # 3. Ensure necessary imports and decorator if not present
        prepared_code = code
        if "@coworker_tool" not in code and f"def {cleaned_name}(" in code:
            prepared_code = (
                "from coworker.tools.base import coworker_tool, tool_success, tool_error\n\n"
                f'@coworker_tool(name="{cleaned_name}", description="{description}", requires_approval={requires_approval})\n'
                + code
            )

        # 4. Write to disk
        try:
            target_file.write_text(prepared_code, encoding="utf-8")
        except Exception as exc:
            return tool_error(f"Cannot write file {target_file}: {exc}")

        # 5. Hot-reload into registry
        try:
            module_name = f"coworker_custom_tool_{cleaned_name}"
            spec = importlib.util.spec_from_file_location(module_name, target_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                tool_func = getattr(module, cleaned_name, None)
                if tool_func and callable(tool_func):
                    registry.register(tool_func)
                    return tool_success(
                        data={
                            "tool": cleaned_name,
                            "file": str(target_file),
                            "hot_reloaded": True,
                            "note": f"Custom tool '{cleaned_name}' successfully created and hot-reloaded.",
                        }
                    )
        except Exception as exc:
            return tool_error(f"File created at {target_file} but registry hot-reload failed: {exc}")

        return tool_success(data={"tool": cleaned_name, "file": str(target_file), "hot_reloaded": False})

    return create_custom_tool
