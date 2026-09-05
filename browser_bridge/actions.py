"""Typed Action Registry and Schemas for Browser Bridge.

Defines all supported precompiled actions executable by the Chrome Extension.
Arbitrary JavaScript execution is strictly prohibited.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field


class ActionName(str, Enum):
    BROWSER_HEALTH = "browser.health"
    TAB_LIST = "tab.list"
    TAB_GET_ACTIVE = "tab.getActive"
    TAB_OPEN = "tab.open"
    TAB_FOCUS = "tab.focus"
    PAGE_NAVIGATE = "page.navigate"
    PAGE_GET_URL = "page.getUrl"
    PAGE_GET_TITLE = "page.getTitle"
    PAGE_WAIT_FOR = "page.waitFor"
    DOM_QUERY = "dom.query"
    DOM_QUERY_ALL = "dom.queryAll"
    DOM_GET_TEXT = "dom.getText"
    DOM_GET_ATTRIBUTE = "dom.getAttribute"
    DOM_CLICK = "dom.click"
    INPUT_TYPE = "input.type"
    INPUT_SELECT = "input.select"
    PAGE_SNAPSHOT = "page.snapshot"
    FETCH_SAME_ORIGIN = "fetch.sameOrigin"
    JOB_CANCEL = "job.cancel"


ALL_ACTIONS: Set[str] = {a.value for a in ActionName}

# Allowed HTTP methods for fetch.sameOrigin
ALLOWED_FETCH_METHODS: Set[str] = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# Headers forbidden from being overridden arbitrarily by client
DISALLOWED_FETCH_HEADERS: Set[str] = {
    "host",
    "connection",
    "keep-alive",
    "cookie",
    "cookie2",
    "sec-",
    "proxy-",
    "user-agent",
}


class TabOpenParams(BaseModel):
    url: str = Field(..., description="Destination URL to open")
    active: bool = Field(default=True, description="Whether to make the tab active")


class TabFocusParams(BaseModel):
    tabId: int = Field(..., description="ID of the tab to bring to foreground")


class PageNavigateParams(BaseModel):
    url: str = Field(..., description="Target URL")
    tabId: Optional[int] = Field(default=None, description="Target tab ID; active tab if omitted")
    waitUntil: Optional[str] = Field(default="load", description="load | domcontentloaded | networkidle")


class PageWaitForParams(BaseModel):
    selector: str = Field(..., description="CSS/DOM selector to wait for")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    state: str = Field(default="visible", description="visible | hidden | attached | detached")
    timeoutMs: int = Field(default=15000, ge=100, le=60000)


class DomQueryParams(BaseModel):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class DomQueryAllParams(BaseModel):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    limit: int = Field(default=50, ge=1, le=500)


class DomGetTextParams(BaseModel):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    maxChars: Optional[int] = Field(default=8000, ge=10, le=100000)


class DomGetAttributeParams(BaseModel):
    selector: str = Field(..., description="CSS selector")
    attribute: str = Field(..., description="Attribute name e.g. href, src, value")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class DomClickParams(BaseModel):
    selector: str = Field(..., description="CSS selector to click")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    waitForNavigation: bool = Field(default=False)


class InputTypeParams(BaseModel):
    selector: str = Field(..., description="CSS selector of input/textarea")
    text: str = Field(..., description="Text to type")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    clearFirst: bool = Field(default=True, description="Clear existing value before typing")
    submit: bool = Field(default=False, description="Press Enter after typing")


class InputSelectParams(BaseModel):
    selector: str = Field(..., description="CSS selector of select element")
    value: str = Field(..., description="Option value or label to select")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class PageSnapshotParams(BaseModel):
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    format: str = Field(default="text", description="text | html | accessibility")
    maxChars: int = Field(default=50000, ge=100, le=500000)


class FetchSameOriginParams(BaseModel):
    pathOrUrl: str = Field(..., description="Relative path or absolute same-origin URL")
    tabId: Optional[int] = Field(default=None, description="Target tab ID providing origin and cookies")
    method: str = Field(default="GET", description="HTTP method")
    headers: Dict[str, str] = Field(default_factory=dict, description="Allowed request headers")
    body: Optional[Any] = Field(default=None, description="Optional request payload")
    maxResponseBytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)


class JobCancelParams(BaseModel):
    jobId: str = Field(..., description="ID of the job or command to cancel")


class EmptyParams(BaseModel):
    pass


def validate_action_params(action: str, params: Dict[str, Any]) -> BaseModel:
    """Validates action parameters against its typed schema."""
    if action not in ALL_ACTIONS:
        raise ValueError(f"Unknown or unsupported action: '{action}'")

    if action == ActionName.TAB_OPEN:
        return TabOpenParams(**params)
    elif action == ActionName.TAB_FOCUS:
        return TabFocusParams(**params)
    elif action == ActionName.PAGE_NAVIGATE:
        return PageNavigateParams(**params)
    elif action == ActionName.PAGE_WAIT_FOR:
        return PageWaitForParams(**params)
    elif action == ActionName.DOM_QUERY:
        return DomQueryParams(**params)
    elif action == ActionName.DOM_QUERY_ALL:
        return DomQueryAllParams(**params)
    elif action == ActionName.DOM_GET_TEXT:
        return DomGetTextParams(**params)
    elif action == ActionName.DOM_GET_ATTRIBUTE:
        return DomGetAttributeParams(**params)
    elif action == ActionName.DOM_CLICK:
        return DomClickParams(**params)
    elif action == ActionName.INPUT_TYPE:
        return InputTypeParams(**params)
    elif action == ActionName.INPUT_SELECT:
        return InputSelectParams(**params)
    elif action == ActionName.PAGE_SNAPSHOT:
        return PageSnapshotParams(**params)
    elif action == ActionName.FETCH_SAME_ORIGIN:
        return FetchSameOriginParams(**params)
    elif action == ActionName.JOB_CANCEL:
        return JobCancelParams(**params)

    # Actions without mandatory parameters (e.g. browser.health, tab.list, tab.getActive, page.getUrl, page.getTitle)
    return EmptyParams()

