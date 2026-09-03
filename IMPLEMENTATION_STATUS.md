# Implementation Status Audit

## Baseline Test Results & Coverage Caveats
- **Baseline Result**: `58 passed in 14.03s`
- **Coverage Caveats**: The BCP conformance test suite (`test_conformance.py`) passes but only tests a hardcoded `MockTransport`, failing to exercise the actual `SocketTransport` or `bcp-agent`. Furthermore, the `cdp_backend.py` relies heavily on `pass` and `NotImplementedError` stubs. Therefore, many tests are passing against mocks while the real runtime implementations are missing or broken.
- **Media Pipeline Note**: Media tests (`connect-ai/tests/test_media_pipeline.py`, etc.) run and pass. They are tracked separately.

---

## Reviewer-Caught False Positives (Round 1)
In the previous audit, file existence was incorrectly used as proof of completion. Re-evaluating behavior against the Definition of Done revealed:
- **T7 (BrowserFetcher)** calls the underlying transport directly without passing `targetId`, diverging from the schema.
- **T10 (Conformance Suite)** only uses `MockTransport` and is not a true Phase-1 CdpTransport suite.
- **T11 (AgentServer)** uses an unsupported `asyncio.start_server(..., pipe=...)` signature on Windows and lacks TCP fallback.
- **T12 (SessionManager)** relies on a basic `token_hex(16)` mock, missing file ACLs, quotas, and connection isolation required by §10.
- **T14-T19 (CDP Backend)** falsely advertise capabilities and are littered with `pass`/`NotImplementedError` for core features like handles, uploads, and network interception.
- **T20 (SocketTransport)** is incomplete; it fails to start a receive loop automatically and lacks reconnect/backoff logic.
- **C11-C22 (Crawler Integration)** contain structural mocks for databases, auth stores, and metrics registries instead of the durable, persistent implementations required by the specs.

---

## Browser Control Plane (BCP) — Implementation Matrix

Generated protocol stubs (T2, T3) are fully complete and separated from the runtime logic. 

| Task | Status | Evidence / Notes |
|---|---|---|
| **T1** | ✅ Complete | `browser-control-plane/docs/cdp-inventory.md` exists and catalogs CDP call-sites. |
| **T2** | ✅ Complete | `protocol/schema.json` is frozen and defines the bounded method set. |
| **T3** | ✅ Complete | `protocol/gen/types.py` and `protocol/gen/stubs.py` are properly generated. |
| **T4** | ✅ Complete | `IBrowserTransport` and `CdpTransport` implemented in `client/transport/cdp_transport.py`. |
| **T5** | ✅ Complete | `client/api/` translates high-level methods to transport calls. |
| **T6** | ✅ Complete | Actionability engine built and polling in `client/api/locator.py`. |
| **T7** | ✅ Complete | `BrowserFetcher` resolves/creates `targetId` and manages target lifecycle. Verified in `crawler/tests/test_bcp_client.py`. |
| **T8** | ✅ Complete | `tools/ci_check_cdp_symbols.py` runs and correctly enforces the isolation rule. |
| **T9** | ⚠️ Partial | `server.py` implements some endpoints, but misses required fixtures like `infinite-scroll-lazy`, `popup-window`, etc. |
| **T10** | ❌ Missing | Four `MockTransport` tests do not constitute the Phase-1 `CdpTransport` conformance suite. |
| **T11** | ✅ Complete | `AgentServer.start_listening` correctly binds Windows Named Pipes using `loop.start_serving_pipe` with `WindowsPipeServer` and TCP fallback. Verified in `test_pipe_transport.py`. |
| **T12** | ⚠️ Partial | Token logic is minimal; lacks file ACLs, quotas, limits, and isolation per §10. |
| **T13** | ⚠️ Partial | Manager files exist but lack implementation of lifecycle event emissions. |
| **T14** | ⚠️ Partial | `CdpBackend.call_function` raises `NotImplementedError`; handle lifecycle is absent. |
| **T15** | ⚠️ Partial | `query` returns `id(element)` without a registry; `get_attributes` is a stub returning `{}`. |
| **T16** | ⚠️ Partial | `upload_files` is `pass`; handle-based action paths are unimplemented. |
| **T17** | ⚠️ Partial | Network backend methods are `pass`; interception methods omitted from dispatch. |
| **T18** | ⚠️ Partial | `import_state` is a warning/no-op; full storage semantics are unverified. |
| **T19** | ⚠️ Partial | Backend advertises capabilities it does not actually implement. |
| **T20** | ✅ Complete | `SocketTransport` implements `IBrowserTransport`, connects via `create_pipe_connection`, auto-manages `receive_loop`, and shuts down cleanly. |
| **T21** | ✅ Complete | Windows Named Pipe and loopback TCP transport suites pass (7/7 tests in `test_pipe_transport.py` & `test_tcp_transport.py`). |
| **T22** | ❌ Missing | Fault-injection suite §6.3 missing. |
| **T23** | ❌ Missing | Benchmark harness §8 missing. |
| **T24** | ❌ Missing | Shadow mode missing. |
| **T25** | ❌ Missing | Pilot crawler migration missing. |
| **T26** | ❌ Missing | Flag rollout missing. |
| **T27** | ❌ Missing | `CdpTransport` remains in the client. |
| **T28** | ⏳ Gated | Extension backend (B1) gated by earlier phases. |

---

## Crawler Layer — Implementation Matrix

| Task | Status | Evidence / Notes |
|---|---|---|
| **C1** | ✅ Complete | SQLite-backed task schema implemented in `crawler/storage.py`. |
| **C2** | ⚠️ Partial | Compliance engine file exists, but robots parser, cache, and UA policy matching lack behavioral proof. |
| **C3** | ⚠️ Partial | `fetcher.py` handles conditional GET, but `retry.py` lacks stateful circuit breaker / rate limiter. |
| **C4** | ✅ Complete | `HttpFetcher` implemented and tested. |
| **C5** | ⚠️ Partial | `BrowserFetcher` exists but depends on unverified BCP transport and uses a mismatched navigate signature. |
| **C6** | ⚠️ Partial | DB schema has TTL, but compressed storage logic and re-extract job are missing. |
| **C7** | ✅ Complete | Declarative extractor logic implemented in `extractor.py`. |
| **C8** | ✅ Complete | Record validation implemented in `records.py`. |
| **C9** | ✅ Complete | Canonicalization and dedup keys implemented in `identity.py`. |
| **C10** | ✅ Complete | Idempotent upsert implemented via SQLite ON CONFLICT in `storage.py`. |
| **C11** | ⚠️ Partial | DLQ state exists in SQLite, but `requeue_dead_letters` and checkpoint resume lack behavioral test proof. |
| **C12** | ⚠️ Partial | `metrics.py` implements an in-memory dictionary, not actual persistent alarms/alerting semantics. |
| **C13** | ⚠️ Partial | Conformance tests exist, but fixture recorder and golden runner components are missing. |
| **C14** | ⚠️ Partial | VnExpress discovery logic present, but lacks behavioral testing against the full DoD. |
| **C15** | ⚠️ Partial | Extractor logic present, but lacks full DoD coverage. |
| **C16** | ❌ Missing | 500-article soak test missing. |
| **C17** | ⚠️ Partial | Signing exists, but token refresh single-flight and durable cursor checkpointing are missing. |
| **C18** | ⚠️ Partial | `TokenStore` is an in-memory mock; real auth flow and secret store are missing. |
| **C19** | ⚠️ Partial | Product sync exists in API, but lacks snapshots and full §12.5 DoD. |
| **C20** | ❌ Missing | Nightly live-integration job missing. |
| **C21** | ⚠️ Partial | Shopee reviews helper methods exist, but 30-day rescan/disappearance integration missing. |
| **C22** | ⚠️ Partial | Buyer hashing implemented, but retention job, separate access control, and audit log missing. |

---

## Prioritized Remaining Work

1. [x] **T11**: Fix `AgentServer.start` Windows pipe binding and add TCP fallback. (Completed in Task 2)
2. [x] **T7**: Fix `BrowserFetcher` targetId mismatch in `page.navigate` call. (Completed in Task 2)
3. [x] **T20**: Implement `receive_loop` auto-start and `IBrowserTransport` for `SocketTransport`. (Completed in Task 2)
4. [x] **T21**: Conformance tests on `SocketTransport` (Named Pipe + TCP loopback). (Completed in Task 2)
5. **T12-T19**: Implement the missing `pass` / `NotImplementedError` stubs in `cdp_backend.py` and managers (target, dom, network, storage).
6. **T9 & T10**: Complete the fixture site and convert conformance tests to use `CdpTransport` instead of `MockTransport`.
7. **Crawler Layer**: Convert in-memory mocks (metrics, rate limiters, token stores) to persistent implementations.

---

## Completed Tasks Summary

- **Task 1**: BCP & Crawler Layer Implementation Status Audit + TCP loopback test transport (`test_tcp_transport.py`).
- **Task 2**: Fix T11 AgentServer Windows Named Pipe startup (`WindowsPipeServer`, `loop.start_serving_pipe`) with TCP fallback; fix T20 SocketTransport Windows named pipe client connection and auto receive loop; fix T7 BrowserFetcher targetId lifecycle; add Named Pipe conformance suite (`test_pipe_transport.py`, 7/7 transport tests passing).

---

## Recommended Task 3

**Scope:** Implement T14-T16 CDP backend DOM & Runtime handles (`cdp_backend.py` and `managers/runtime_manager.py`).

**Context:**
`CdpBackend.call_function` currently raises `NotImplementedError` and element queries return mock IDs without an ExecutionContext/RemoteObject handle registry. Implementing DOM and runtime handles is required to unblock evaluation and actionability checks over real CDP.

**Likely Files:**
- `browser-control-plane/agent/backends/cdp/cdp_backend.py`
- `browser-control-plane/agent/managers/runtime_manager.py`
- `browser-control-plane/agent/managers/dom_manager.py`

**Acceptance Criteria:**
- Implement remote object handle tracking per ExecutionContext in `RuntimeManager` and `CdpBackend`.
- Replace `NotImplementedError` in `call_function` with actual CDP `Runtime.callFunctionOn`.
- Add test coverage verifying remote handle evaluation and release.
