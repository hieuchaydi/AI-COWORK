#!/usr/bin/env python3
"""
BCP Codegen Pipeline (T3).

Reads protocol/schema.json and generates:
  - browser-control-plane/protocol/gen/types.py   — Python Pydantic models for all params/results
  - browser-control-plane/protocol/gen/stubs.py   — Agent handler stubs

Usage:
  python tools/codegen_bcp.py

Rule: Generated files are in protocol/gen/ and must NOT be hand-edited.
Regenerate by running this script after editing schema.json.
"""

import json
import sys
import textwrap
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
SCHEMA_PATH = WORKSPACE / "browser-control-plane" / "protocol" / "schema.json"
GEN_DIR = WORKSPACE / "browser-control-plane" / "protocol" / "gen"


def snake(name: str) -> str:
    """Convert camelCase or PascalCase to snake_case."""
    import re
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def gen_types(schema: dict) -> str:
    lines = [
        "# AUTO-GENERATED — DO NOT HAND-EDIT. Regenerate with tools/codegen_bcp.py",
        "# fmt: off",
        "from __future__ import annotations",
        "from typing import Any, Dict, List, Optional",
        "from pydantic import BaseModel, Field",
        "",
    ]
    for method_name, method_def in schema.get("methods", {}).items():
        class_base = method_name.replace(".", "_").replace("-", "_")

        # Params class
        params_schema = method_def.get("params") or {}
        params_props = params_schema.get("properties", {}) if isinstance(params_schema, dict) else {}
        params_required = set(params_schema.get("required", [])) if isinstance(params_schema, dict) else set()
        if params_props:
            cls_name = class_base.replace("_", " ").title().replace(" ", "") + "Params"
            lines.append(f"class {cls_name}(BaseModel):")
            for field_name, field_def in params_props.items():
                if not isinstance(field_def, dict):
                    field_def = {}
                field_type = _py_type(field_def)
                required = field_name in params_required
                if required:
                    lines.append(f"    {snake(field_name)}: {field_type}")
                else:
                    lines.append(f"    {snake(field_name)}: Optional[{field_type}] = None")
            lines.append("")

        # Result class
        result_schema = method_def.get("result") or {}
        result_props = result_schema.get("properties", {}) if isinstance(result_schema, dict) else {}
        if result_props:
            cls_name = class_base.replace("_", " ").title().replace(" ", "") + "Result"
            lines.append(f"class {cls_name}(BaseModel):")
            for field_name, field_def in result_props.items():
                if not isinstance(field_def, dict):
                    field_def = {}
                field_type = _py_type(field_def)
                lines.append(f"    {snake(field_name)}: Optional[{field_type}] = None")
            lines.append("")

    return "\n".join(lines)


def _py_type(field_def: dict) -> str:
    t = field_def.get("type", "any")
    mapping = {
        "string": "str", "integer": "int", "number": "float",
        "boolean": "bool", "array": "List[Any]", "object": "Dict[str, Any]",
        "any": "Any",
    }
    return mapping.get(t, "Any")


def gen_stubs(schema: dict) -> str:
    lines = [
        "# AUTO-GENERATED — DO NOT HAND-EDIT. Regenerate with tools/codegen_bcp.py",
        "# fmt: off",
        '"""Agent handler stubs — implement each raise NotImplementedError."""',
        "from typing import Any, Dict",
        "",
        "",
        "class GeneratedHandlerStubs:",
        '    """One method per BCP method. Override in the concrete backend."""',
        "",
    ]
    for method_name in schema.get("methods", {}):
        fn_name = snake(method_name.replace(".", "_"))
        lines.append(f"    async def handle_{fn_name}(self, params: Dict[str, Any]) -> Dict[str, Any]:")
        lines.append(f'        raise NotImplementedError("{method_name} not implemented")')
        lines.append("")

    return "\n".join(lines)


def main():
    if not SCHEMA_PATH.exists():
        print(f"ERROR: schema.json not found at {SCHEMA_PATH}", file=sys.stderr)
        sys.exit(1)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    GEN_DIR.mkdir(parents=True, exist_ok=True)

    # Write __init__.py
    (GEN_DIR / "__init__.py").write_text(
        "# AUTO-GENERATED package. Do not edit.\n", encoding="utf-8"
    )

    types_code = gen_types(schema)
    (GEN_DIR / "types.py").write_text(types_code, encoding="utf-8")
    print(f"Generated {GEN_DIR / 'types.py'}")

    stubs_code = gen_stubs(schema)
    (GEN_DIR / "stubs.py").write_text(stubs_code, encoding="utf-8")
    print(f"Generated {GEN_DIR / 'stubs.py'}")

    print("Codegen complete.")


if __name__ == "__main__":
    main()
