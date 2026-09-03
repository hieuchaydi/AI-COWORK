"""Tool registry — wraps callables (incl. aisuite toolkit tools) into a registry the
runtime owns: JSON schemas for the model, plus execution. Permission checks live in the
PermissionEngine and are applied by the turn engine, not here.

Schema generation is reused from aisuite (`Tools`) so we don't reimplement
docstring/type-hint → JSON-schema extraction.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional

from aisuite.utils.tools import Tools


@dataclass
class ToolSpec:
    name: str
    schema: dict[str, Any]  # OpenAI-format function tool schema
    func: Callable[..., Any]
    metadata: Any = None  # aisuite ToolMetadata or None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        func: Callable[..., Any],
        *,
        metadata: Any = None,
        schema: Optional[dict[str, Any]] = None,
    ) -> ToolSpec:
        name = getattr(func, "__name__", None)
        if not name:
            raise ValueError("Tool function must have a __name__.")
        meta = (
            metadata
            or getattr(func, "__coworker_tool_metadata__", None)
            or getattr(func, "__aisuite_tool_metadata__", None)
        )
        # Allow an explicit schema override (param or a `__coworker_schema__` attribute)
        # for tools whose signature can't be auto-converted to a valid JSON schema.
        resolved_schema = (
            schema or getattr(func, "__coworker_schema__", None) or _schema_for(func)
        )
        spec = ToolSpec(name=name, schema=resolved_schema, func=func, metadata=meta)
        self._tools[name] = spec
        return spec

    def register_all(self, funcs: list[Callable[..., Any]]) -> None:
        for func in funcs:
            self.register(func)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def schemas(
        self, active_names: Optional[set[str] | list[str]] = None
    ) -> list[dict[str, Any]]:
        if active_names is not None:
            names_set = set(active_names)
            return [
                spec.schema for name, spec in self._tools.items() if name in names_set
            ]
        return [spec.schema for spec in self._tools.values()]

    def execute(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"Tool not registered: {name}")
        return spec.func(**(arguments or {}))


def _schema_for(func: Callable[..., Any]) -> dict[str, Any]:
    """Generate one OpenAI-format tool schema via aisuite's schema generator with fallback."""
    try:
        return Tools([func]).tools(format="openai")[0]
    except Exception:
        return _fallback_schema(func)


def _fallback_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Fallback schema generator for tools with missing or unconventional type hints."""
    name = getattr(func, "__name__", "unnamed_tool")
    doc = (inspect.getdoc(func) or f"Tool {name}").strip()
    properties: dict[str, Any] = {}
    required: list[str] = []

    type_map = {
        int: "integer",
        float: "number",
        str: "string",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    try:
        sig = inspect.signature(func)
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue
            param_type = "string"
            if param.annotation != inspect.Parameter.empty:
                ann = param.annotation
                for py_type, json_type in type_map.items():
                    if ann is py_type or (isinstance(ann, type) and issubclass(ann, py_type)):
                        param_type = json_type
                        break
            prop_def: dict[str, Any] = {"type": param_type, "description": f"Parameter {param_name}"}
            if param.default != inspect.Parameter.empty:
                prop_def["default"] = param.default
            else:
                required.append(param_name)
            properties[param_name] = prop_def
    except Exception:
        pass

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
