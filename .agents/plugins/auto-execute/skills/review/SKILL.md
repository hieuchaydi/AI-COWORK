---
name: review
description: Performs a strict, evidence-based review of local code changes or a requested commit range. Use when the user invokes /review, asks whether code is truly ready, or automatically after implementing a /plan before committing.
---

# Strict Code Review

Review like a skeptical senior maintainer. The goal is to discover real defects before users do, not to approve code quickly and not to manufacture criticism.

## Establish Scope

1. Read repository instructions and determine the requested change.
2. Inspect `git status`, the current branch, its merge base, and the complete relevant diff. Compare with the tracked remote when available, but do not fetch unless network access is authorized.
3. Trace every modified entry point through its callers, dependencies, state changes, and externally visible contracts. Read surrounding code; never review isolated diff hunks only.
4. Preserve unrelated user changes and keep the review scoped to the requested implementation.

## Adversarial Review Checklist

Check all applicable categories, including interactions between them:

- Functional correctness: wrong branches, boundary values, stale state, invalid assumptions, incomplete feature wiring, and parameters accepted but ignored.
- Failure behavior: partial success, cleanup, rollback, timeout, cancellation, retry storms, duplicate side effects, reconnects, and process restarts.
- Security and privacy: authentication, authorization, fail-open defaults, origin and input validation, confused-deputy paths, secrets, unsafe logging, injection, path traversal, and unbounded input or output.
- Concurrency and lifecycle: races, deadlocks, ordering, idempotency, listener/timer leaks, background work continuing after cancellation, and ownership of shared state.
- Compatibility: public APIs, schemas, persisted data, older clients, configuration defaults, Windows/Linux differences, and upgrade or migration paths.
- Performance and resources: buffering before limits are checked, unbounded collections, expensive loops, unnecessary network calls, and leaks.
- Observability: errors that are swallowed, misleading success states, missing actionable diagnostics, and sensitive data in logs.
- Tests: assert behavior rather than implementation text; include negative, boundary, failure, cancellation, concurrency, and regression cases. Treat green tests as evidence, never as proof.

## Validate Findings

- Reproduce or prove each candidate from code and control flow. Run the smallest useful test or check when possible.
- Search for an existing guard before reporting a missing one.
- Reject style-only comments unless they create a concrete maintenance or correctness risk.
- Use severities consistently:
  - `P0`: immediate catastrophic impact or data/security compromise.
  - `P1`: high-impact defect likely to affect real users or a security boundary.
  - `P2`: meaningful correctness, reliability, or compatibility defect.
  - `P3`: low-risk improvement that can safely be deferred.

## Output Contract

Lead with findings ordered by severity. Each finding must contain:

- `[P0-P3]` and a concise title.
- Exact file path and the tightest useful line range.
- Trigger or reproduction condition.
- Concrete runtime/user impact.
- Why existing tests or guards do not prevent it.

Then report checks run, assumptions or coverage gaps, and a short readiness verdict. If there are no findings, explicitly state that no actionable findings were found while still listing residual risks and what was not tested.

## Automatic Repair Loop After `/plan`

When this skill is invoked automatically after implementation:

1. Review the implementation before staging or committing.
2. Fix all validated in-scope P0, P1, and P2 findings immediately.
3. Add or strengthen regression tests for each fixed behavior where practical.
4. Run syntax checks and the relevant test suite.
5. Review the resulting diff again from scratch, including the fixes themselves.
6. Repeat until no validated P0, P1, or P2 finding remains, with a maximum of three full review passes.
7. Clean temporary artifacts, verify `git status`, and only then create the Conventional Commit requested by the auto-execute rules.

An explicit standalone `/review` is read-only unless the user also asks to fix the findings. The automatic post-`/plan` invocation is part of implementation and therefore includes the repair loop above.
