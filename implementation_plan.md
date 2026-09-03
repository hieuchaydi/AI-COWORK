---
RequestFeedback: false
---

# Implementation Plan: Antigravity Media Scrape Autopilot

## Goal

Create an Antigravity workspace plugin that makes `/plan` scrape tasks run end-to-end without stopping at a plan review gate, and ensures any crawl that discovers images or videos saves CSV data, downloads media into a folder, zips that folder, and reports both download links.

## Milestones & Tasks

1. [x] [MODIFY] `connect-ai/coworker/tools/crawl.py` and router metadata so `crawl_and_export_bundle` is available as a first-class crawl tool.
2. [x] [MODIFY] Media tests to verify the bundled CSV + media ZIP workflow is registered and callable.
3. [x] [NEW] `.agents/plugins/media-scrape-autopilot/` Antigravity plugin with manifest, auto-allow hook, media scrape skill, rules, and README.
4. [x] [NEW] Plugin smoke test that validates manifest JSON, hook wiring, and required skill/rule content.
5. [x] Run syntax checks and targeted tests.
6. [x] [NEW] `walkthrough.md` with outcome and verification notes.
7. [x] Commit all completed changes with a clear Conventional Commit message.

## Verification Plan

- `python -m py_compile connect-ai/coworker/tools/crawl.py connect-ai/coworker/tools/media_pipeline.py`
- `pytest connect-ai/tests/test_media_pipeline.py connect-ai/tests/test_media_zip_tool.py tests/test_antigravity_media_plugin.py`

This plan is informational only. It does not request review, approval, or a submit step.
