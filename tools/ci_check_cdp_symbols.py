#!/usr/bin/env python3
"""
CI Grep Rule (T8, §5): Verify no CDP symbols leak outside allowed folders.

Rule from BCP plan §5:
  No `CDP` / `Protocol.` symbols may appear outside:
    - browser-control-plane/client/transport/cdp_transport.py
    - browser-control-plane/agent/backends/cdp/

Usage:
  python tools/ci_check_cdp_symbols.py

Returns exit code 0 if clean, 1 if violations found.
"""

import re
import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent

# Patterns that indicate CDP type leakage
CDP_PATTERNS = [
    r"\bCDP\b",
    r"\bProtocol\.",
    r"from playwright",
    r"import playwright",
    r"chromium\.",
    r"playwright\.async_api",
]

# Allowed file paths (these MAY contain CDP references)
ALLOWED_PATHS = {
    WORKSPACE / "browser-control-plane" / "client" / "transport" / "cdp_transport.py",
    WORKSPACE / "browser-control-plane" / "agent" / "backends" / "cdp" / "cdp_backend.py",
}

# Directories to scan (Python files only)
SCAN_DIRS = [
    WORKSPACE / "browser-control-plane" / "client" / "api",
    WORKSPACE / "browser-control-plane" / "agent" / "managers",
    WORKSPACE / "browser-control-plane" / "agent" / "main.py",
    WORKSPACE / "crawler",
    WORKSPACE / "bcp",
]


def check():
    violations = []
    pattern = re.compile("|".join(CDP_PATTERNS))

    for scan_target in SCAN_DIRS:
        if scan_target.is_file():
            files = [scan_target]
        else:
            files = list(scan_target.rglob("*.py"))

        for path in files:
            if path in ALLOWED_PATHS:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        violations.append(f"{path}:{lineno}: {line.strip()}")
            except Exception as exc:
                print(f"Warning: could not read {path}: {exc}", file=sys.stderr)

    if violations:
        print("CDP SYMBOL LEAK VIOLATIONS (§5 CI rule):")
        for v in violations:
            print(f"  {v}")
        print(f"\n{len(violations)} violation(s) found. Fix before merging.")
        return 1
    else:
        print("OK: No CDP symbol leaks detected outside allowed folders.")
        return 0


if __name__ == "__main__":
    sys.exit(check())
