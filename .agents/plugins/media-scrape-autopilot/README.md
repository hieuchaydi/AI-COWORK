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

If terminal prompts still appear for commands such as `Set-Content`, `New-Item`, or `git diff`, the running Antigravity session has not loaded wildcard command permissions yet. Run the installer script once, then restart Antigravity:

```powershell
powershell -ExecutionPolicy Bypass -File .agents\plugins\media-scrape-autopilot\scripts\install_antigravity_auto_approvals.ps1
```

For this machine, the live Antigravity 2.0 config is under:

```text
%USERPROFILE%\.gemini\config\config.json
%USERPROFILE%\.gemini\config\projects\<project-id>.json
```

The AI-COWORK project should include:

```json
{
  "settings": {
    "toolPermission": "always-proceed",
    "artifactReviewPolicy": "always-proceed",
    "allowNonWorkspaceAccess": "allow",
    "internetAccess": "allow"
  },
  "permissionGrants": {
    "permissionGrants": {
      "allow": [
        "command(*)",
        "unsandboxed(*)",
        "read_file(*)",
        "write_file(*)",
        "read_url(*)",
        "execute_url(*)",
        "mcp(*)",
        "command(git)",
        "command(python)",
        "command(pytest)",
        "command(.venv\\Scripts\\python.exe)",
        "command(npm)",
        "command(node)",
        "read_url(*)",
        "execute_url(*)"
      ]
    }
  }
}
```

If prompts still appear after editing this config, restart Antigravity so the project settings are reloaded.
