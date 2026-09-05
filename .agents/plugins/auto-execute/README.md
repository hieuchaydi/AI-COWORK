# Antigravity Auto Execute

Workspace plugin that executes `/plan` tasks end-to-end and performs a strict `/review` before committing.

## Commands

- `/plan <task>`: plan, implement, test, automatically run `/review`, fix validated P0-P2 findings, review again, then commit.
- `/review [scope]`: perform a read-only, evidence-based review with findings ordered by severity.

The automatic review loop is capped at three complete passes. It does not allow a commit while validated P0, P1, or P2 findings remain.
