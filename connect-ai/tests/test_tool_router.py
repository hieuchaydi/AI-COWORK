"""Tests for Dynamic Tool Router (coworker.tools.router)."""

from __future__ import annotations

import os
from coworker.providers import ToolCall
from coworker.tools import (
    ToolCategory,
    ToolRegistry,
    categorize_tool,
    route_tools_for_context,
)
from coworker.tools.router import get_active_categories


def test_categorize_tool_mappings():
    # CORE tools
    assert categorize_tool("read_file") == ToolCategory.CORE
    assert categorize_tool("write_file") == ToolCategory.CORE
    assert categorize_tool("list_files") == ToolCategory.CORE
    assert categorize_tool("todo_write") == ToolCategory.CORE
    assert categorize_tool("current_time") == ToolCategory.CORE
    assert categorize_tool("ask_user") == ToolCategory.CORE
    assert categorize_tool("web_search") == ToolCategory.CORE
    assert categorize_tool("load_skill") == ToolCategory.CORE
    assert categorize_tool("remember") == ToolCategory.CORE

    # BROWSER tools
    assert categorize_tool("browser_open") == ToolCategory.BROWSER
    assert categorize_tool("browser_click") == ToolCategory.BROWSER
    assert categorize_tool("browser_fill") == ToolCategory.BROWSER
    assert categorize_tool("browser_screenshot") == ToolCategory.BROWSER
    assert categorize_tool("browser_evaluate") == ToolCategory.BROWSER

    # CRAWL tools
    assert categorize_tool("crawl_urls") == ToolCategory.CRAWL
    assert categorize_tool("parse_sitemap") == ToolCategory.CRAWL
    assert categorize_tool("extract_html") == ToolCategory.CRAWL
    assert categorize_tool("extract_table") == ToolCategory.CRAWL
    assert categorize_tool("save_csv") == ToolCategory.CRAWL
    assert categorize_tool("commerce_monitor_check") == ToolCategory.CRAWL

    # DEV_SHELL tools
    assert categorize_tool("run_shell") == ToolCategory.DEV_SHELL
    assert categorize_tool("git_status") == ToolCategory.DEV_SHELL
    assert categorize_tool("git_diff") == ToolCategory.DEV_SHELL
    assert categorize_tool("explorer_subagent") == ToolCategory.DEV_SHELL

    # CONNECTORS tools
    assert categorize_tool("send_message") == ToolCategory.CONNECTORS
    assert categorize_tool("send_file") == ToolCategory.CONNECTORS
    assert categorize_tool("mcp__telegram-bot__send_message") == ToolCategory.CONNECTORS
    assert categorize_tool("gmail_send") == ToolCategory.CONNECTORS
    assert categorize_tool("slack_post") == ToolCategory.CONNECTORS

    # AUTOMATION tools
    assert categorize_tool("schedule_task") == ToolCategory.AUTOMATION
    assert categorize_tool("list_scheduled_tasks") == ToolCategory.AUTOMATION
    assert categorize_tool("selfwake_sleep") == ToolCategory.AUTOMATION


def test_intent_detection_vietnamese():
    # Browser
    msgs = [{"role": "user", "content": "Vào web click nút đăng nhập giúp tôi"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.BROWSER in cats
    assert ToolCategory.CORE in cats

    # Crawl
    msgs = [{"role": "user", "content": "Cào 30 bài viết từ vnexpress và trích xuất bảng"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.CRAWL in cats

    msgs = [{"role": "user", "content": "Theo dõi giá sản phẩm này giúp tôi"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.CRAWL in cats

    # Dev shell
    msgs = [{"role": "user", "content": "Chạy lệnh pytest để test toàn bộ repo"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.DEV_SHELL in cats

    # Connectors
    msgs = [{"role": "user", "content": "Gửi tin nhắn Telegram tới sếp báo cáo"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.CONNECTORS in cats

    # Automation
    msgs = [{"role": "user", "content": "Hẹn giờ 10 phút nữa chạy lại script"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.AUTOMATION in cats


def test_intent_detection_english():
    # Browser
    msgs = [{"role": "user", "content": "Open playwright browser and take a screenshot"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.BROWSER in cats

    # Crawl
    msgs = [{"role": "user", "content": "Scrape the sitemap.xml and save csv"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.CRAWL in cats

    # Dev shell
    msgs = [{"role": "user", "content": "Run git commit and npm install"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.DEV_SHELL in cats

    # Connectors
    msgs = [{"role": "user", "content": "Send a slack message to #general"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.CONNECTORS in cats

    # Automation
    msgs = [{"role": "user", "content": "Schedule task in 15 minutes"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.AUTOMATION in cats


def test_history_continuity():
    # Assistant called a browser tool in previous turn; user just says "tiếp tục đi"
    msgs = [
        {"role": "user", "content": "Đăng nhập trang web X"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tc1",
                    "function": {"name": "browser_open", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": "opened"},
        {"role": "user", "content": "Tiếp tục đi"},
    ]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert ToolCategory.BROWSER in cats


def test_small_registry_bypass():
    # Total tools <= 10 (e.g. test fixtures) -> all categories active
    msgs = [{"role": "user", "content": "hello"}]
    cats = get_active_categories(msgs, total_registered_tools=8)
    assert cats == set(ToolCategory)


def test_env_var_override(monkeypatch):
    monkeypatch.setenv("COWORKER_DYNAMIC_TOOL_ROUTING", "0")
    msgs = [{"role": "user", "content": "hello"}]
    cats = get_active_categories(msgs, total_registered_tools=50)
    assert cats == set(ToolCategory)


def test_route_tools_for_context():
    registry = ToolRegistry()

    def dummy():
        pass

    dummy.__name__ = "dummy"

    # Register 15 tools across different categories
    tool_names = [
        "read_file",
        "write_file",
        "todo_write",
        "current_time",
        "browser_open",
        "browser_click",
        "crawl_urls",
        "extract_html",
        "run_shell",
        "git_status",
        "send_message",
        "gmail_send",
        "schedule_task",
        "selfwake_sleep",
        "custom_tool",
    ]
    for name in tool_names:

        def fn():
            pass

        fn.__name__ = name
        registry.register(fn, schema={"type": "function", "function": {"name": name}})

    # User asking for telegram/slack
    msgs = [{"role": "user", "content": "Gửi tin nhắn telegram"}]
    routed = route_tools_for_context(registry, msgs)

    # Must contain CORE + CONNECTORS
    assert "read_file" in routed
    assert "send_message" in routed
    assert "gmail_send" in routed
    # Must NOT contain browser/crawl/automation unless matched
    assert "browser_open" not in routed
    assert "crawl_urls" not in routed
    assert "schedule_task" not in routed
