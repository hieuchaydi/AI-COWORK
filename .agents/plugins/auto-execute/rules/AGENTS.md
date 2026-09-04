# Auto-Execute Rules

These rules are for the trusted local AI-COWORK development workspace.

## Highest Priority

- Never stop at "Submit plan", "Review plan", "Approve", or "Proceed".
- Treat `/plan` and any implementation plan as informational tracking only.
- Do not create blocking plan artifacts.
- Do not set `RequestFeedback: true`; use `RequestFeedback: false`.
- After writing a plan, immediately execute it in the same turn from A to Z.
- Continue until the requested task is complete, verified, and committed.

## Execution

- Run shell commands, read files, write files, tests, and git operations without asking.
- After code changes, run a syntax check and relevant tests.
- Fix failures before finishing.
- Never commit plan files (`implementation_plan.md`, `walkthrough.md`) or temporary test files/scripts to Git.
- Clean up and clear all temporary test and plan files from the workspace before committing.
- Commit completed work with a clear Conventional Commit message.
- Answer in Vietnamese unless the user explicitly asks for another language.

## Only Stop For

1. Irreversible data loss such as deleting user data, dropping a database, or force-pushing over history.
2. Missing credentials, tokens, or accounts that are required and not available.

If neither exception applies, keep working.
