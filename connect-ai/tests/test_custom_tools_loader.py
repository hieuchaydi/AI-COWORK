"""Unit tests for Coworker custom tools loader, registry and base decorator."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.base import coworker_tool, tool_success, tool_error, ToolMetadata
from coworker.tools.registry import ToolRegistry
from coworker.tools.custom_loader import discover_custom_tools


def test_coworker_tool_decorator():
    @coworker_tool(name="test_tool", description="A test tool", category="custom")
    def dummy_tool(x: int) -> dict:
        return tool_success(value=x * 2)

    meta = getattr(dummy_tool, "__coworker_tool_metadata__", None)
    assert isinstance(meta, ToolMetadata)
    assert meta.name == "test_tool"
    assert meta.description == "A test tool"
    assert meta.category == "custom"

    # Execution success
    res = dummy_tool(x=5)
    assert res == {"ok": True, "value": 10}


def test_coworker_tool_exception_handling():
    @coworker_tool(name="failing_tool")
    def will_fail():
        raise RuntimeError("Simulated failure")

    # Wrapper intercepts exceptions safely
    res = will_fail()
    assert res["ok"] is False
    assert "Simulated failure" in res["error"]


def test_registry_with_coworker_tool():
    @coworker_tool(name="registry_test", description="Test registration")
    def tool_fn(msg: str) -> dict:
        """Process a message."""
        return tool_success(data=msg)

    reg = ToolRegistry()
    spec = reg.register(tool_fn)
    assert spec.name == "registry_test"
    assert spec.metadata.category == "custom"

    # Schema generated
    schemas = reg.schemas()
    assert len(schemas) == 1
    fn_schema = schemas[0].get("function", {})
    assert fn_schema.get("name") == "registry_test"


def test_discover_custom_tools():
    tools = discover_custom_tools()
    assert len(tools) >= 1
    tool_names = [t.__name__ for t in tools]
    assert "example_echo" in tool_names
