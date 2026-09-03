"""Base definitions, standard response helpers, and decorators for Coworker tools."""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class ToolMetadata:
    """Metadata attached to a coworker tool."""

    name: str
    description: str
    category: str = "custom"
    requires_approval: bool = False
    schema: Optional[dict[str, Any]] = None


def tool_success(data: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Return a standardized success payload."""
    res: dict[str, Any] = {"ok": True}
    if data is not None:
        res["data"] = data
    res.update(kwargs)
    return res


def tool_error(error: str, **kwargs: Any) -> dict[str, Any]:
    """Return a standardized error payload."""
    res: dict[str, Any] = {"ok": False, "error": str(error)}
    res.update(kwargs)
    return res


def coworker_tool(
    name: Optional[str | Callable[..., Any]] = None,
    *,
    description: Optional[str] = None,
    category: str = "custom",
    requires_approval: bool = False,
    schema: Optional[dict[str, Any]] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]] | Callable[..., Any]:
    """Decorator to mark and configure a callable as a standardized Coworker tool.

    Usage:
        @coworker_tool
        def my_tool(param: str) -> dict[str, Any]:
            '''Docstring describing what the tool does.'''
            return tool_success(result="done")

    Or with options:
        @coworker_tool(
            name="custom_lookup",
            category="core",
            requires_approval=True,
            description="Performs an external lookup."
        )
        def my_tool(query: str) -> dict[str, Any]:
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        actual_name = (name if isinstance(name, str) else None) or getattr(
            func, "__name__", "unnamed_tool"
        )
        doc = description or inspect.getdoc(func) or f"Tool {actual_name}"
        meta = ToolMetadata(
            name=actual_name,
            description=doc.strip(),
            category=category,
            requires_approval=requires_approval,
            schema=schema,
        )
        setattr(func, "__coworker_tool_metadata__", meta)
        setattr(func, "__name__", actual_name)
        if schema:
            setattr(func, "__coworker_schema__", schema)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                res = func(*args, **kwargs)
                if isinstance(res, dict):
                    if "ok" not in res:
                        return tool_success(data=res)
                    return res
                return tool_success(data=res)
            except Exception as exc:  # noqa: BLE001
                return tool_error(f"Execution failed: {exc}")

        # Preserve metadata on wrapper
        setattr(wrapper, "__coworker_tool_metadata__", meta)
        setattr(wrapper, "__name__", actual_name)
        if schema:
            setattr(wrapper, "__coworker_schema__", schema)
        return wrapper

    if callable(name):
        actual_func = name
        name = None
        return decorator(actual_func)

    return decorator
