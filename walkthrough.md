# Walkthrough: Antigravity Media Scrape Autopilot

## Outcome

Built a workspace Antigravity plugin at `.agents/plugins/media-scrape-autopilot/`.

The plugin adds:

- `plugin.json` manifest with the official Antigravity schema URL.
- `hooks.json` plus `allow.py` to pre-approve trusted local tool use.
- `/media-scrape-bundle` skill for scrape/crawl tasks with images or videos.
- Workspace rule requiring CSV output, media download into `outputs/media/<job_name>/`, ZIP output in `outputs/zips/`, and direct links in the final response.
- README with workspace/global install notes and the Antigravity settings needed to bypass product-level plan/terminal gates.

Also exposed `crawl_and_export_bundle` as a first-class crawl tool so agents can do CSV + media ZIP as one tool call.

## Verification

- `python -m py_compile connect-ai/coworker/tools/crawl.py connect-ai/coworker/tools/media_pipeline.py`
- `.venv\Scripts\python.exe -m py_compile connect-ai/coworker/tools/crawl.py connect-ai/coworker/tools/media_pipeline.py .agents/plugins/media-scrape-autopilot/allow.py`
- `python allow.py` from the plugin directory
- `.venv\Scripts\python.exe -m pytest connect-ai/tests/test_media_pipeline.py connect-ai/tests/test_media_zip_tool.py tests/test_antigravity_media_plugin.py`

Result: 8 tests passed.

## Notes

Antigravity plugins and rules can steer the agent and hook tool calls, but the visible "Submit" / "Proceed" artifact gate is also controlled by Antigravity's Artifact Review setting. For this trusted workspace, set Artifact Review to `Always Proceed` if the UI still pauses on plan artifacts.
