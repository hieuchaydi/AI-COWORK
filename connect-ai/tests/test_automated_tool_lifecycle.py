"""End-to-End Integration Tests for Automated Tool Creation and Lifecycle.

Tests that tool creation, schema generation, normalization, error handling,
and mid-session hot-reloading work reliably without crashes or edge-case failures.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.base import coworker_tool, tool_success, tool_error
from coworker.tools.registry import ToolRegistry, _schema_for
from coworker.tools.custom_loader import make_create_custom_tool, discover_custom_tools


def test_schema_resilience_for_untyped_parameters():
    """Verify that a tool without any type hints generates a valid schema without TypeError."""
    def untyped_func(arg_one, optional_arg=42):
        """Tool without type annotations."""
        return {"result": arg_one}

    # Must not raise TypeError
    schema = _schema_for(untyped_func)
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "untyped_func"
    assert "arg_one" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["arg_one"]


def test_output_normalization():
    """Verify that returning raw dict, primitive, or custom object is safely normalized."""
    @coworker_tool
    def raw_dict_tool(n: int):
        return {"raw_value": n * 10}

    @coworker_tool
    def primitive_tool(text: str):
        return f"Echo: {text}"

    res1 = raw_dict_tool(n=3)
    assert res1["ok"] is True
    assert res1["data"]["raw_value"] == 30

    res2 = primitive_tool(text="hello")
    assert res2["ok"] is True
    assert res2["data"] == "Echo: hello"


def test_automated_creation_and_hot_reload(tmp_path):
    """Verify full lifecycle: create_custom_tool creates file, parses AST, and hot-reloads into registry."""
    reg = ToolRegistry()
    creator = make_create_custom_tool(reg, workspace=tmp_path)

    tool_code = """
def dynamic_multiplier(multiplier: int, factor: int = 2) -> dict:
    '''Multiply numbers dynamically.'''
    return {"product": multiplier * factor}
"""

    # 1. Create tool
    res = creator(name="dynamic_multiplier", code=tool_code, description="Multiply dynamically")
    assert res["ok"] is True
    assert res["data"]["hot_reloaded"] is True

    # 2. Check registered in ToolRegistry
    spec = reg.get("dynamic_multiplier")
    assert spec is not None
    assert spec.name == "dynamic_multiplier"

    # 3. Check schema
    schema = spec.schema
    assert schema["function"]["name"] == "dynamic_multiplier"

    # 4. Execute via registry
    exec_res = reg.execute("dynamic_multiplier", {"multiplier": 7, "factor": 6})
    assert exec_res["ok"] is True
    assert exec_res["data"]["product"] == 42


def test_syntax_error_rejection(tmp_path):
    """Verify that Python code with syntax errors is rejected BEFORE writing to disk."""
    reg = ToolRegistry()
    creator = make_create_custom_tool(reg, workspace=tmp_path)

    broken_code = "def bad_syntax(: pass"
    res = creator(name="bad_tool", code=broken_code)

    assert res["ok"] is False
    assert "syntax error" in res["error"].lower()

    # File should not exist on disk
    target_file = tmp_path / "custom_tools" / "bad_tool.py"
    assert not target_file.exists()


def test_invalid_name_rejection(tmp_path):
    """Verify invalid tool names (non snake_case) are rejected."""
    reg = ToolRegistry()
    creator = make_create_custom_tool(reg, workspace=tmp_path)

    res = creator(name="Invalid Name With Spaces!", code="def foo(): pass")
    assert res["ok"] is False
    assert "snake_case" in res["error"].lower()
