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
| **T7** | ⚠️ Partial | `BrowserFetcher` calls `transport.call` directly without `targetId`, violating the schema. |
| **T8** | ✅ Complete | `tools/ci_check_cdp_symbols.py` runs and correctly enforces the isolation rule. |
| **T9** | ⚠️ Partial | `server.py` implements some endpoints, but misses required fixtures like `infinite-scroll-lazy`, `popup-window`, etc. |
| **T10** | ❌ Missing | Four `MockTransport` tests do not constitute the Phase-1 `CdpTransport` conformance suite. |
| **T11** | ⚠️ Partial | `AgentServer.start` has a broken signature for Windows named pipes and lacks TCP fallback. |
| **T12** | ⚠️ Partial | Token logic is minimal; lacks file ACLs, quotas, limits, and isolation per §10. |
| **T13** | ⚠️ Partial | Manager files exist but lack implementation of lifecycle event emissions. |
| **T14** | ⚠️ Partial | `CdpBackend.call_function` raises `NotImplementedError`; handle lifecycle is absent. |
| **T15** | ⚠️ Partial | `query` returns `id(element)` without a registry; `get_attributes` is a stub returning `{}`. |
| **T16** | ⚠️ Partial | `upload_files` is `pass`; handle-based action paths are unimplemented. |
| **T17** | ⚠️ Partial | Network backend methods are `pass`; interception methods omitted from dispatch. |
| **T18** | ⚠️ Partial | `import_state` is a warning/no-op; full storage semantics are unverified. |
| **T19** | ⚠️ Partial | Backend advertises capabilities it does not actually implement. |
| **T20** | ⚠️ Partial | `SocketTransport` lacks auto `receive_loop`, reconnect, backoff, and tracing. |
| **T21** | ❌ Missing | No SocketTransport conformance suite. |
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

1. **T11**: Fix `AgentServer.start` Windows pipe binding and add TCP fallback. (Current blocker for all SocketTransport usage)
2. **T7**: Fix `BrowserFetcher` targetId mismatch in `page.navigate` call.
3. **T20**: Implement `receive_loop` auto-start, reconnect, and backoff for `SocketTransport`.
4. **T12-T19**: Implement the missing `pass` / `NotImplementedError` stubs in `cdp_backend.py` and managers.
5. **T9 & T10**: Complete the fixture site and convert conformance tests to use `CdpTransport` instead of `MockTransport`.
6. **T21**: Run conformance suite on `SocketTransport`.
7. **Crawler Layer**: Convert in-memory mocks (metrics, rate limiters, token stores) to persistent implementations.

---

## Recommended Task 2

**Scope:** Fix T11 AgentServer startup path on Windows (and implement TCP fallback).

**Context:**
`browser-control-plane/agent/main.py` uses `asyncio.start_server(self.handle_client, host=None, port=None, pipe=pipe_name)` on Windows. The `pipe` kwarg is not a standard signature for `asyncio.start_server`. This completely breaks the agent startup on Windows, preventing any SocketTransport testing.

**Likely Files:**
- `browser-control-plane/agent/main.py`

**Acceptance Criteria:**
- Modify `AgentServer.start` to correctly initialize named pipes on Windows (e.g., using `asyncio.start_serving(..., pipe=...)` via `ProactorEventLoop`, or falling back to TCP loopback `127.0.0.1` as permitted by §4.2.1).
- Add a test or verify via a script that the `bcp-agent` successfully binds to the pipe/socket without raising an exception on startup.
- The `bcp-agent` can accept a handshake connection.
