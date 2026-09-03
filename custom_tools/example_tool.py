"""Example custom tool demonstration for AI-COWORK.

To create a new tool quickly:
1. Create a Python file in `custom_tools/<your_tool>.py`
2. Decorate your function with `@coworker_tool`
3. Provide type annotations and a clear docstring
4. Return `tool_success(...)` or `tool_error(...)`
That is it! The runtime automatically registers it on startup.
"""

from __future__ import annotations

from typing import Any
from coworker.tools.base import coworker_tool, tool_success, tool_error


@coworker_tool(
    name="example_echo",
    category="custom",
    requires_approval=False,
    description="Echoes back a message with word count and metadata for testing custom tools.",
)
def example_echo(message: str, shout: bool = False) -> dict[str, Any]:
    """Echo back the input message with statistics.

    Args:
        message: The text message to echo back.
        shout: If True, convert the message to uppercase.
    """
    if not message.strip():
        return tool_error("Message cannot be empty.")

    text = message.upper() if shout else message
    words = len(text.split())
    return tool_success(
        data={
            "echoed": text,
            "word_count": words,
            "char_count": len(text),
        }
    )
