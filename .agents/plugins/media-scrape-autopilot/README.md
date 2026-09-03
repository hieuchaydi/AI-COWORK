# Antigravity Media Scrape Autopilot

Workspace plugin for Antigravity scrape tasks that should run without a plan-review pause and should bundle media automatically.

## What It Does

- Adds the `/media-scrape-bundle` skill.
- Adds workspace rules for `/plan` auto-execution.
- Adds a `PreToolUse` hook that returns `allow` for trusted local tool execution.
- Standardizes scrape outputs:
  - CSV: `outputs/csv/<job_name>.csv`
  - Media folder: `outputs/media/<job_name>/`
  - ZIP: `outputs/zips/<job_name>_media.zip`

## Use

Example prompt:

```text
/plan cao data san pham nay, lay ca anh video review, xuat csv va nen media thanh zip
```

The agent should create/update a plan for tracking, then continue immediately.

## Install

This folder is already in the workspace plugin location:

```text
.agents/plugins/media-scrape-autopilot/
```

For global use in Antigravity IDE, copy this folder to:

```text
%USERPROFILE%\.gemini\config\plugins\media-scrape-autopilot\
```

For Antigravity CLI, install from this workspace:

```text
agy plugin install .agents/plugins/media-scrape-autopilot
```

## Important Antigravity Settings

To remove the UI gate shown as "Submit" or "Proceed", set these in Antigravity Settings for this trusted dev workspace:

- Artifact Review: `Always Proceed`
- Terminal Command Auto Execution: `Always Proceed`
- Browser Javascript Execution: `Always Proceed`, if your scrape needs same-origin API calls from an opened browser

The plugin instructs the agent and pre-approves hookable tool calls, but Antigravity's artifact-review setting is the product-level switch that controls the plan submit gate.
