# Auto-Execute Rules

These rules are for the trusted local AI-COWORK development workspace.

## Highest Priority

- Never stop at "Submit plan", "Review plan", "Approve", or "Proceed".
- Treat `/plan` and any implementation plan as informational tracking only.
- Do not create blocking plan artifacts.
- Do not set `RequestFeedback: true`; use `RequestFeedback: false`.
- After writing a plan, immediately execute it in the same turn from A to Z.
- Continue until the requested task is complete, verified, strictly reviewed, and committed.

## Mandatory Post-Implementation Review

- Every `/plan` implementation must run the bundled `/review` workflow after code and tests, before `git commit`.
- Do not treat passing tests, successful compilation, or a clean diff as proof that the implementation is correct.
- Review the actual diff and affected call paths with an adversarial mindset. Check contracts, failure paths, security boundaries, cancellation, concurrency, idempotency, state transitions, platform behavior, resource limits, backward compatibility, and missing negative tests.
- Every finding must include concrete evidence: severity, file and tight line range, runtime impact, and the condition that triggers it. Do not invent speculative findings merely to appear strict.
- During an automatic post-`/plan` review, fix every validated P0, P1, and P2 finding that is within task scope. Add a regression test where practical, rerun checks, then repeat `/review`.
- Do not commit while any validated P0, P1, or P2 finding remains. P3 findings may remain only when explicitly recorded with rationale.
- Stop the review loop after three complete passes if the same blocker cannot be resolved without credentials, irreversible data loss, or a material scope decision; report that blocker instead of looping indefinitely.

## Execution

- Run shell commands, read files, write files, tests, and git operations without asking.
- After code changes, run a syntax check and relevant tests.
- Fix failures before finishing.
- Run `/review` after the tests pass and before staging files.
- Never commit plan files (`implementation_plan.md`, `walkthrough.md`) or temporary test files/scripts to Git.
- Clean up and clear all temporary test and plan files from the workspace before committing.
- Commit completed work with a clear Conventional Commit message.
- Answer in Vietnamese unless the user explicitly asks for another language.

## Only Stop For

1. Irreversible data loss such as deleting user data, dropping a database, or force-pushing over history.
2. Missing credentials, tokens, or accounts that are required and not available.

If neither exception applies, keep working.
