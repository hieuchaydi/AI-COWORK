# AUTO-GENERATED — DO NOT HAND-EDIT. Regenerate with tools/codegen_bcp.py
# fmt: off
"""Agent handler stubs — implement each raise NotImplementedError."""
from typing import Any, Dict


class GeneratedHandlerStubs:
    """One method per BCP method. Override in the concrete backend."""

    async def handle_agent_hello(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("agent.hello not implemented")

    async def handle_agent_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("agent.ping not implemented")

    async def handle_agent_stats(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("agent.stats not implemented")

    async def handle_agent_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("agent.shutdown not implemented")

    async def handle_target_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("target.list not implemented")

    async def handle_target_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("target.create not implemented")

    async def handle_target_activate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("target.activate not implemented")

    async def handle_target_close(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("target.close not implemented")

    async def handle_page_navigate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.navigate not implemented")

    async def handle_page_reload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.reload not implemented")

    async def handle_page_go_back(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.goBack not implemented")

    async def handle_page_go_forward(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.goForward not implemented")

    async def handle_page_wait_for_load_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.waitForLoadState not implemented")

    async def handle_page_content(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.content not implemented")

    async def handle_page_screenshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.screenshot not implemented")

    async def handle_page_set_viewport(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.setViewport not implemented")

    async def handle_page_frames(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.frames not implemented")

    async def handle_page_handle_dialog(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.handleDialog not implemented")

    async def handle_page_set_download_behavior(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("page.setDownloadBehavior not implemented")

    async def handle_runtime_evaluate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("runtime.evaluate not implemented")

    async def handle_runtime_call_function(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("runtime.callFunction not implemented")

    async def handle_runtime_release_handle(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("runtime.releaseHandle not implemented")

    async def handle_runtime_add_init_script(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("runtime.addInitScript not implemented")

    async def handle_dom_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.query not implemented")

    async def handle_dom_query_all(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.queryAll not implemented")

    async def handle_dom_wait_for_selector(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.waitForSelector not implemented")

    async def handle_dom_attributes(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.attributes not implemented")

    async def handle_dom_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.text not implemented")

    async def handle_dom_html(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.html not implemented")

    async def handle_dom_bounding_box(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.boundingBox not implemented")

    async def handle_dom_scroll_into_view(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.scrollIntoView not implemented")

    async def handle_dom_snapshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("dom.snapshot not implemented")

    async def handle_input_click(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.click not implemented")

    async def handle_input_hover(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.hover not implemented")

    async def handle_input_type(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.type not implemented")

    async def handle_input_press(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.press not implemented")

    async def handle_input_scroll(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.scroll not implemented")

    async def handle_input_drag_and_drop(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.dragAndDrop not implemented")

    async def handle_input_upload_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("input.uploadFiles not implemented")

    async def handle_network_enable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.enable not implemented")

    async def handle_network_disable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.disable not implemented")

    async def handle_network_get_body(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.getBody not implemented")

    async def handle_network_set_interception(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.setInterception not implemented")

    async def handle_network_continue(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.continue not implemented")

    async def handle_network_fulfill(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.fulfill not implemented")

    async def handle_network_abort(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("network.abort not implemented")

    async def handle_storage_get_cookies(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.getCookies not implemented")

    async def handle_storage_set_cookies(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.setCookies not implemented")

    async def handle_storage_clear_cookies(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.clearCookies not implemented")

    async def handle_storage_get_local(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.getLocal not implemented")

    async def handle_storage_set_local(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.setLocal not implemented")

    async def handle_storage_get_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.getSession not implemented")

    async def handle_storage_export_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.exportState not implemented")

    async def handle_storage_import_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("storage.importState not implemented")
