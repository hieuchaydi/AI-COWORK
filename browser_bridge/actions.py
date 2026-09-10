"""Typed Action Registry and Schemas for Browser Bridge.

Defines all supported precompiled actions executable by the Chrome Extension.
Arbitrary JavaScript execution is strictly prohibited.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ActionName(str, Enum):
    BROWSER_HEALTH = "browser.health"
    CAPTCHA_DETECT = "captcha.detect"
    CAPTCHA_AUTO_DRAG = "captcha.autoDrag"
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
    PAGE_SCREENSHOT = "page.screenshot"
    FETCH_SAME_ORIGIN = "fetch.sameOrigin"
    JOB_CANCEL = "job.cancel"
    JOB_CLEAR_STALE = "job.clearStale"
    PAGE_SCROLL = "page.scroll"
    EXTENSION_RELOAD = "extension.reload"


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
    "authorization",
}


class BaseActionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TabOpenParams(BaseActionParams):
    url: str = Field(..., description="Destination URL to open")
    active: bool = Field(default=True, description="Whether to make the tab active")


class TabFocusParams(BaseActionParams):
    tabId: int = Field(..., description="ID of the tab to bring to foreground")


class PageNavigateParams(BaseActionParams):
    url: str = Field(..., description="Target URL")
    tabId: Optional[int] = Field(default=None, description="Target tab ID; active tab if omitted")
    waitUntil: Optional[str] = Field(default="load", description="load | domcontentloaded | networkidle")

    @field_validator("waitUntil")
    @classmethod
    def validate_wait_until(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"load", "domcontentloaded", "networkidle"}:
            raise ValueError(f"Invalid waitUntil '{v}'. Must be 'load', 'domcontentloaded', or 'networkidle'")
        return v


class PageWaitForParams(BaseActionParams):
    selector: str = Field(..., description="CSS/DOM selector to wait for")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    state: str = Field(default="visible", description="visible | hidden | attached | detached")
    timeoutMs: int = Field(default=15000, ge=100, le=60000)

    @field_validator("state")
    @classmethod
    def validate_state(cls, v: str) -> str:
        if v not in {"visible", "hidden", "attached", "detached"}:
            raise ValueError(f"Invalid state '{v}'. Must be 'visible', 'hidden', 'attached', or 'detached'")
        return v


class DomQueryParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class DomQueryAllParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    limit: int = Field(default=50, ge=1, le=500)


class DomGetTextParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    maxChars: Optional[int] = Field(default=8000, ge=10, le=100000)


class DomGetAttributeParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector")
    attribute: str = Field(..., description="Attribute name e.g. href, src, value")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class DomClickParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector to click")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    waitForNavigation: bool = Field(default=False)


class InputTypeParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector of input/textarea")
    text: str = Field(..., description="Text to type")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    clearFirst: bool = Field(default=True, description="Clear existing value before typing")
    submit: bool = Field(default=False, description="Press Enter after typing")


class InputSelectParams(BaseActionParams):
    selector: str = Field(..., description="CSS selector of select element")
    value: str = Field(..., description="Option value or label to select")
    tabId: Optional[int] = Field(default=None, description="Target tab ID")


class PageSnapshotParams(BaseActionParams):
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    format: str = Field(default="text", description="text | html")
    maxChars: int = Field(default=50000, ge=100, le=500000)

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in {"text", "html"}:
            raise ValueError(f"Invalid snapshot format '{v}'. Only 'text' and 'html' are supported")
        return v


class PageScreenshotParams(BaseActionParams):
    tabId: Optional[int] = Field(default=None, description="Target tab ID; active tab if omitted")
    tabId: Optional[int] = Field(default=None, alias="tab_id", description="Target tab ID; active tab if omitted")
    format: str = Field(default="png", description="png | jpeg")
    quality: Optional[int] = Field(default=None, ge=0, le=100, description="Quality 0-100 (for jpeg)")

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in {"png", "jpeg"}:
            raise ValueError(f"Invalid screenshot format '{v}'. Only 'png' and 'jpeg' are supported")
        return v


class FetchSameOriginParams(BaseActionParams):
    pathOrUrl: str = Field(..., description="Relative path or absolute same-origin URL")
    tabId: Optional[int] = Field(default=None, description="Target tab ID providing origin and cookies")
    method: str = Field(default="GET", description="HTTP method")
    headers: Dict[str, str] = Field(default_factory=dict, description="Allowed request headers")
    body: Optional[Any] = Field(default=None, description="Optional request payload")
    maxResponseBytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        upper = v.upper()
        if upper not in ALLOWED_FETCH_METHODS:
            raise ValueError(f"Unsupported HTTP method: '{v}'. Must be one of {sorted(ALLOWED_FETCH_METHODS)}")
        return upper

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: Dict[str, str]) -> Dict[str, str]:
        for k in headers.keys():
            k_lower = k.lower().strip()
            if k_lower in DISALLOWED_FETCH_HEADERS or k_lower.startswith(("sec-", "proxy-")):
                raise ValueError(f"Header '{k}' is forbidden in fetch.sameOrigin")
        return headers


class JobCancelParams(BaseActionParams):
    jobId: str = Field(..., description="ID of the job or command to cancel")


class PageScrollParams(BaseActionParams):
    tabId: Optional[int] = Field(default=None, description="Target tab ID")
    selector: Optional[str] = Field(default=None, description="Optional CSS selector to scroll within")
    top: Optional[int] = Field(default=None, description="Absolute scroll position (scrollTo)")
    left: Optional[int] = Field(default=None, description="Absolute horizontal scroll position")
    deltaY: Optional[int] = Field(default=None, description="Relative vertical scroll amount (scrollBy)")
    deltaX: Optional[int] = Field(default=None, description="Relative horizontal scroll amount")
    behavior: str = Field(default="smooth", description="smooth | instant | auto")


class CaptchaDetectParams(BaseActionParams):
    tabId: Optional[int] = Field(default=None, description="Target tab ID; active tab if omitted")
    scrollIntoView: bool = Field(default=True, description="Whether to scroll captcha controls into view")


class CaptchaAutoDragParams(BaseActionParams):
    tabId: Optional[int] = Field(default=None, description="Target tab ID; active tab if omitted")


class EmptyParams(BaseActionParams):
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
    elif action == ActionName.PAGE_SCREENSHOT:
        return PageScreenshotParams(**params)
    elif action == ActionName.FETCH_SAME_ORIGIN:
        return FetchSameOriginParams(**params)
    elif action == ActionName.JOB_CANCEL:
        return JobCancelParams(**params)
    elif action == ActionName.PAGE_SCROLL:
        return PageScrollParams(**params)
    elif action == ActionName.CAPTCHA_DETECT:
        return CaptchaDetectParams(**params)
    elif action == ActionName.CAPTCHA_AUTO_DRAG:
        return CaptchaAutoDragParams(**params)
    elif action in (
        ActionName.BROWSER_HEALTH,
        ActionName.TAB_LIST,
        ActionName.TAB_GET_ACTIVE,
        ActionName.PAGE_GET_URL,
        ActionName.PAGE_GET_TITLE,
        ActionName.JOB_CLEAR_STALE,
        ActionName.EXTENSION_RELOAD,
    ):
        return EmptyParams(**params)

    raise ValueError(f"Unhandled action schema: '{action}'")
