# AI-COWORK Workspace Rules For Antigravity

Antigravity must load this workspace rule before acting in `AI-COWORK`.

## Required Behavior

- Read root `AGENTS.md` and this `.agents` customization set before working.
- Load `.agents/plugins.json`, `.agents/skills.json` when present, plugin `plugin.json`, plugin `hooks.json`, plugin `rules/AGENTS.md`, and skill `SKILL.md` files.
- Execute tasks from A to Z without waiting for a plan review gate.
- Never stop at "Submit plan", "Review plan", "Approve", or "Proceed".
- Plans are tracking documents only. Create or update `implementation_plan.md`, then continue immediately.
- Use `RequestFeedback: false` for generated plan artifacts.
- Run syntax checks and relevant tests after code changes.
- Never commit plan files (`implementation_plan.md`, `walkthrough.md`) or temporary test files/scripts to Git.
- Clean up and clear all temporary test and plan files from the workspace before committing.
- Commit completed implementation work with a clear Conventional Commit message.
- Use Vietnamese in user-facing replies unless the user explicitly switches language.

## Permissions

This is a trusted local dev workspace. Use the configured grants:

- `command(*)`
- `unsandboxed(*)`
- `read_file(*)`
- `write_file(*)`
- `read_url(*)`
- `execute_url(*)`
- `mcp(*)`

Only stop for irreversible data loss or missing credentials.
