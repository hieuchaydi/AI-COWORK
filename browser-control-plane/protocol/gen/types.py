# AUTO-GENERATED — DO NOT HAND-EDIT. Regenerate with tools/codegen_bcp.py
# fmt: off
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class AgentHelloResult(BaseModel):
    version: Optional[str] = None

class TargetListResult(BaseModel):
    targets: Optional[List[Any]] = None

class TargetCreateParams(BaseModel):
    url: Optional[str] = None
    incognito: Optional[bool] = None

class TargetCreateResult(BaseModel):
    target_id: Optional[str] = None

class TargetActivateParams(BaseModel):
    target_id: str

class TargetCloseParams(BaseModel):
    target_id: str

class PageNavigateParams(BaseModel):
    target_id: str
    url: str
    wait_until: Optional[str] = None
    referrer: Optional[str] = None

class PageWaitforloadstateParams(BaseModel):
    state: str

class PageContentParams(BaseModel):
    frame_id: Optional[str] = None

class PageContentResult(BaseModel):
    content: Optional[str] = None

class PageScreenshotParams(BaseModel):
    format: str
    quality: Optional[int] = None
    full_page: Optional[bool] = None
    clip: Optional[Dict[str, Any]] = None

class PageScreenshotResult(BaseModel):
    data: Optional[str] = None

class PageSetviewportParams(BaseModel):
    width: int
    height: int
    dpr: Optional[float] = None
    mobile: Optional[bool] = None

class PageFramesResult(BaseModel):
    frames: Optional[List[Any]] = None

class PageHandledialogParams(BaseModel):
    action: str
    prompt_text: Optional[str] = None

class PageSetdownloadbehaviorParams(BaseModel):
    mode: str
    path: Optional[str] = None

class RuntimeEvaluateParams(BaseModel):
    frame_id: Optional[str] = None
    world: Optional[str] = None
    expression: str
    await_promise: Optional[bool] = None
    return_by_value: Optional[bool] = None

class RuntimeEvaluateResult(BaseModel):
    result: Optional[Any] = None
    exception_details: Optional[Any] = None

class RuntimeCallfunctionParams(BaseModel):
    handle_id: str
    function_declaration: str
    args: Optional[List[Any]] = None

class RuntimeReleasehandleParams(BaseModel):
    handle_id: str

class RuntimeAddinitscriptParams(BaseModel):
    source: str
    world: Optional[str] = None

class DomQueryParams(BaseModel):
    frame_id: Optional[str] = None
    selector: str
    engine: Any

class DomQueryResult(BaseModel):
    handle_id: Optional[str] = None

class DomQueryallParams(BaseModel):
    frame_id: Optional[str] = None
    selector: str
    engine: Any

class DomQueryallResult(BaseModel):
    handle_ids: Optional[List[Any]] = None

class DomWaitforselectorParams(BaseModel):
    selector: str
    state: Any

class DomAttributesParams(BaseModel):
    handle_id: str

class DomAttributesResult(BaseModel):
    attributes: Optional[Dict[str, Any]] = None

class DomTextParams(BaseModel):
    handle_id: str

class DomTextResult(BaseModel):
    text: Optional[str] = None

class DomHtmlParams(BaseModel):
    handle_id: str

class DomHtmlResult(BaseModel):
    html: Optional[str] = None

class DomBoundingboxParams(BaseModel):
    handle_id: str

class DomBoundingboxResult(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None

class DomScrollintoviewParams(BaseModel):
    handle_id: str

class DomSnapshotParams(BaseModel):
    frame_id: Optional[str] = None
    mode: Any
    max_nodes: Optional[int] = None

class DomSnapshotResult(BaseModel):
    nodes: Optional[List[Any]] = None

class InputClickParams(BaseModel):
    handle_id: Optional[str] = None
    point: Optional[Dict[str, Any]] = None
    button: Optional[str] = None
    click_count: Optional[int] = None
    modifiers: Optional[List[Any]] = None

class InputHoverParams(BaseModel):
    handle_id: Optional[str] = None
    point: Optional[Dict[str, Any]] = None

class InputTypeParams(BaseModel):
    text: str
    delay_ms: Optional[int] = None

class InputPressParams(BaseModel):
    key: Optional[str] = None
    code: Optional[str] = None
    modifiers: Optional[List[Any]] = None

class InputScrollParams(BaseModel):
    dx: float
    dy: float
    point: Optional[Dict[str, Any]] = None

class InputDraganddropParams(BaseModel):
    from: Dict[str, Any]
    to: Dict[str, Any]

class InputUploadfilesParams(BaseModel):
    handle_id: str
    paths: List[Any]

class NetworkEnableParams(BaseModel):
    capture_bodies: Any
    max_body_bytes: Optional[int] = None

class NetworkGetbodyParams(BaseModel):
    request_id: str

class NetworkGetbodyResult(BaseModel):
    body: Optional[str] = None
    base64_encoded: Optional[bool] = None

class NetworkSetinterceptionParams(BaseModel):
    patterns: List[Any]

class NetworkContinueParams(BaseModel):
    request_id: str

class NetworkFulfillParams(BaseModel):
    request_id: str

class NetworkAbortParams(BaseModel):
    request_id: str

class StorageGetcookiesParams(BaseModel):
    urls: Optional[List[Any]] = None

class StorageGetcookiesResult(BaseModel):
    cookies: Optional[List[Any]] = None

class StorageSetcookiesParams(BaseModel):
    cookies: List[Any]

class StorageGetlocalParams(BaseModel):
    frame_id: Optional[str] = None

class StorageGetlocalResult(BaseModel):
    data: Optional[Dict[str, Any]] = None

class StorageSetlocalParams(BaseModel):
    data: Dict[str, Any]

class StorageGetsessionResult(BaseModel):
    data: Optional[Dict[str, Any]] = None

class StorageExportstateResult(BaseModel):
    state: Optional[Dict[str, Any]] = None

class StorageImportstateParams(BaseModel):
    state: Dict[str, Any]
