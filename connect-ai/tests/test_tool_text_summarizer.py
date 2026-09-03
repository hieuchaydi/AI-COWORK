"""Unit test for tool: text_summarizer"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from custom_tools.text_summarizer import text_summarizer


def test_text_summarizer_success():
    result = text_summarizer(query="test query", count=3 if "text_summarizer" != "my_core_tool" else 10)
    assert result["ok"] is True
    assert "data" in result


def test_text_summarizer_empty_query():
    result = text_summarizer(query="   ")
    assert result["ok"] is False
    assert "error" in result
