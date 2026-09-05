"""Dynamic Tool Router — categorizes tools and selects the optimal subset per turn.

Instead of flooding the LLM context with 70-100+ tool schemas every turn, the router:
1. Keeps CORE tools (file ops, todos, clock, ask_user, web search/fetch) always available.
2. Dynamically activates specialized tool categories (browser, crawl, dev_shell, connectors, automation)
   based on user intent keywords in prompt context or recent tool execution history.
3. Guarantees multi-turn continuity (if an assistant turn used category X, category X remains active).
4. Respects `COWORKER_DYNAMIC_TOOL_ROUTING=0` environment override (returns all tools if disabled).
"""

from __future__ import annotations

import os
import re
from enum import Enum
from typing import Any, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry


class ToolCategory(str, Enum):
    CORE = "core"
    BROWSER = "browser"
    CRAWL = "crawl"
    DEV_SHELL = "dev_shell"
    CONNECTORS = "connectors"
    AUTOMATION = "automation"
    CUSTOM = "custom"


# Patterns for categorizing tools by their function names
_BROWSER_TOOL_PREFIXES = ("browser_",)
_CRAWL_TOOL_NAMES = {
    "crawl_urls",
    "parse_sitemap",
    "extract_html",
    "extract_table",
    "save_page_snapshot",
    "save_csv",
    "save_artifact",
    "download_file",
    "zip_folder",
    "download_media_and_zip",
    "crawl_and_export_bundle",
    "commerce_monitor_track",
    "commerce_monitor_check",
    "commerce_monitor_list",
    "commerce_monitor_history",
    "commerce_monitor_check_all",
    "configure_profit_guard",
    "get_pricing_recommendation",
    "list_pending_price_approvals",
    "approve_price_change",
    "reject_price_change",
    "get_pricing_audit_history",
}
_DEV_SHELL_TOOL_NAMES = {
    "run_shell",
    "explorer_subagent",
    "git_status",
    "git_diff",
    "git_log",
    "git_commit",
    "git_checkout",
    "git_branch",
    "git_add",
    "code_search",
}
_CONNECTORS_PREFIXES = (
    "mcp__",
    "gmail_",
    "drive_",
    "gcal_",
    "slack_",
    "discord_",
    "telegram_",
    "notion_",
    "hubspot_",
    "jira_",
    "github_",
    "linear_",
    "asana_",
    "trello_",
    "monday_",
    "clickup_",
    "salesforce_",
)
_CONNECTORS_TOOL_NAMES = {
    "send_message",
    "send_file",
    "subscribe",
    "unsubscribe",
    "list_subscriptions",
}
_AUTOMATION_TOOL_NAMES = {
    "schedule_task",
    "list_scheduled_tasks",
    "cancel_scheduled_task",
    "selfwake_sleep",
    "selfwake_check",
}
_AUTOMATION_PREFIXES = ("schedule_", "selfwake_")


def categorize_tool(tool_name: str, metadata: Any = None) -> ToolCategory:
    """Classify a tool into a ToolCategory based on its name and metadata."""
    if metadata is not None:
        meta_cat = getattr(metadata, "category", None)
        if meta_cat:
            meta_cat_str = str(meta_cat).lower()
            if "custom" in meta_cat_str:
                return ToolCategory.CUSTOM
            if "browser" in meta_cat_str:
                return ToolCategory.BROWSER
            if "crawl" in meta_cat_str or "scrape" in meta_cat_str:
                return ToolCategory.CRAWL
            if "shell" in meta_cat_str or "dev" in meta_cat_str or "code" in meta_cat_str:
                return ToolCategory.DEV_SHELL
            if (
                "connector" in meta_cat_str
                or "messaging" in meta_cat_str
                or "integration" in meta_cat_str
            ):
                return ToolCategory.CONNECTORS
            if "automation" in meta_cat_str or "schedule" in meta_cat_str or "wake" in meta_cat_str:
                return ToolCategory.AUTOMATION

    name = tool_name.lower()
    if name.startswith(_BROWSER_TOOL_PREFIXES):
        return ToolCategory.BROWSER
    if name in _CRAWL_TOOL_NAMES:
        return ToolCategory.CRAWL
    if name in _DEV_SHELL_TOOL_NAMES or name.startswith("git_") or name.startswith("code_"):
        return ToolCategory.DEV_SHELL
    if name in _CONNECTORS_TOOL_NAMES or name.startswith(_CONNECTORS_PREFIXES):
        return ToolCategory.CONNECTORS
    if name in _AUTOMATION_TOOL_NAMES or name.startswith(_AUTOMATION_PREFIXES):
        return ToolCategory.AUTOMATION

    return ToolCategory.CORE


_BROWSER_KEYWORDS = {
    "browser",
    "playwright",
    "chromium",
    "trình duyệt",
    "mở web",
    "mở trang",
    "vào trang",
    "vào web",
    "đăng nhập",
    "chụp màn hình",
    "chụp web",
    "screenshot",
    "click",
    "bấm nút",
    "điền form",
    "nhập form",
    "chọn nút",
    "gõ phím",
    "tương tác web",
    "giao diện web",
    "login",
    "sign in",
    "signin",
    "signup",
    "sign up",
    "css selector",
    "xpath",
    "cookie",
}

_CRAWL_KEYWORDS = {
    "crawl",
    "cào web",
    "cào",
    "scrape",
    "scraper",
    "scraping",
    "sitemap",
    "sitemap.xml",
    "bóc tách",
    "tải nhiều trang",
    "bò web",
    "lấy danh sách link",
    "lấy bảng",
    "lưu csv",
    "trích xuất",
    "tải hàng loạt",
    "download_file",
    "save_csv",
    "extract_html",
    "extract_table",
    "thu thập dữ liệu",
    "zip",
    "nén zip",
    "nén file",
    "nén ảnh",
    "nén video",
    "tải ảnh",
    "tải video",
    "theo dõi giá",
    "giá sản phẩm",
    "price monitor",
    "commerce monitor",
    "cào ảnh",
    "cào video",
    "zip_folder",
    "download_media_and_zip",
    "profit_guard",
    "định giá",
    "giá bán",
    "lợi nhuận",
    "biên lợi nhuận",
    "giá sàn",
    "giá hòa vốn",
    "pricing",
    "margin",
    "phê duyệt giá",
    "bảo vệ lợi nhuận",
}

_DEV_SHELL_KEYWORDS = {
    "run_shell",
    "shell",
    "terminal",
    "bash",
    "powershell",
    "cmd",
    "git",
    "commit",
    "branch",
    "push",
    "pull",
    "chạy lệnh",
    "dòng lệnh",
    "cài đặt",
    "chạy test",
    "chạy file",
    "build",
    "compile",
    "pytest",
    "npm",
    "npx",
    "pip",
    "pnpm",
    "yarn",
    "python",
    "node",
    "cargo",
    "docker",
    "make",
    "code_search",
    "thực thi",
}

_CONNECTORS_KEYWORDS = {
    "telegram",
    "gmail",
    "email",
    "mail",
    "hòm thư",
    "hộp thư",
    "thư đến",
    "drive",
    "gdrive",
    "google drive",
    "slack",
    "discord",
    "notion",
    "jira",
    "github",
    "hubspot",
    "linear",
    "asana",
    "trello",
    "monday",
    "clickup",
    "salesforce",
    "gửi tin",
    "nhắn tin",
    "send_message",
    "send_file",
    "gửi file",
    "kênh",
    "channel",
    "bot",
    "subscribe",
    "unsubscribe",
    "inbox",
}

_AUTOMATION_KEYWORDS = {
    "schedule",
    "scheduled",
    "selfwake",
    "timer",
    "cron",
    "fire_at",
    "hẹn giờ",
    "lên lịch",
    "đặt lịch",
    "tự động chạy",
    "chạy định kỳ",
    "nhắc nhở",
    "phút nữa",
    "tiếng nữa",
    "giờ nữa",
    "ngày mai",
    "tối nay",
    "sáng mai",
    "hàng ngày",
    "mỗi ngày",
}


def _matches_any_keyword(text: str, keywords: set[str]) -> bool:
    for kw in keywords:
        if kw in text:
            return True
    return False


def get_active_categories(
    messages: list[dict[str, Any]],
    total_registered_tools: int = 0,
    agent_family: Optional[str] = None,
) -> set[ToolCategory]:
    """Determine the active tool categories for the current context/turn."""
    # 1. Check if disabled via env var
    if os.environ.get("COWORKER_DYNAMIC_TOOL_ROUTING", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return set(ToolCategory)

    # If registry has only a few tools (e.g. unit tests or minimal agent), expose all
    if 0 < total_registered_tools <= 10:
        return set(ToolCategory)

    # 2. CORE and CUSTOM are ALWAYS active
    active: set[ToolCategory] = {ToolCategory.CORE, ToolCategory.CUSTOM}

    # Code-family agents default to having DEV_SHELL active
    if agent_family == "code":
        active.add(ToolCategory.DEV_SHELL)

    # 3. Check recent conversation history (last 8 messages) for continuity
    # If a category was invoked recently, keep it active so multi-turn chains aren't broken
    for msg in reversed(messages[-8:] if len(messages) > 8 else messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                name = None
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name") or tc.get("name")
                elif hasattr(tc, "name"):
                    name = getattr(tc, "name")
                if name:
                    cat = categorize_tool(name)
                    if cat != ToolCategory.CORE:
                        active.add(cat)

    # 4. Extract recent user/steering text to detect user intent
    recent_texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") in ("user", "steering"):
            content = msg.get("content", "")
            if isinstance(content, str):
                recent_texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        recent_texts.append(part.get("text", ""))
        if len(recent_texts) >= 3:
            break

    combined_text = " ".join(recent_texts).lower()

    if _matches_any_keyword(combined_text, _BROWSER_KEYWORDS):
        active.add(ToolCategory.BROWSER)

    if _matches_any_keyword(combined_text, _CRAWL_KEYWORDS):
        active.add(ToolCategory.CRAWL)

    if _matches_any_keyword(combined_text, _DEV_SHELL_KEYWORDS):
        active.add(ToolCategory.DEV_SHELL)

    if _matches_any_keyword(combined_text, _CONNECTORS_KEYWORDS) or "platform:" in combined_text:
        active.add(ToolCategory.CONNECTORS)

    if _matches_any_keyword(combined_text, _AUTOMATION_KEYWORDS) or re.search(
        r"\b\d+\s*(phút|giờ|tiếng|ngày|min|hour|day)", combined_text
    ):
        active.add(ToolCategory.AUTOMATION)

    # If URL is present in prompt, enable browser & crawl if not already matched
    if "http://" in combined_text or "https://" in combined_text or "www." in combined_text:
        active.add(ToolCategory.BROWSER)
        active.add(ToolCategory.CRAWL)

    # Fallback: if only CORE matched and user prompt was general, include DEV_SHELL
    if active == {ToolCategory.CORE}:
        active.add(ToolCategory.DEV_SHELL)

    return active


def route_tools_for_context(
    registry: "ToolRegistry",
    messages: list[dict[str, Any]],
    agent_family: Optional[str] = None,
) -> set[str]:
    """Select the set of tool names to expose to the LLM for this turn."""
    all_names = registry.names()
    active_categories = get_active_categories(
        messages=messages,
        total_registered_tools=len(all_names),
        agent_family=agent_family,
    )

    # If all categories active, return all names
    if active_categories == set(ToolCategory):
        return set(all_names)

    selected: set[str] = set()
    for name in all_names:
        spec = registry.get(name)
        metadata = spec.metadata if spec else None
        cat = categorize_tool(name, metadata=metadata)
        if cat in active_categories:
            selected.add(name)

    return selected
