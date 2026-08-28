# Browser Control Plane (BCP) — Implementation Plan v1

Status: DRAFT — ready for implementation
Owner: <fill>
Target repo: <fill>  (suggested root module: `browser-control-plane/`)

---

## 0. How to use this document (read this first, coding agents included)

Rules that override any assumption you would otherwise make:

1. `protocol/schema.json` (defined in §4) is the **single source of truth**. Client types and
   agent handler stubs are generated from it. Never hand-edit generated files.
2. A method is "done" only when **all four** exist: schema entry + client method + agent handler
   + conformance test in `conformance/`. A method without a conformance test does not count.
3. If a backend cannot implement a method, it MUST declare that in `agent.hello.capabilities`
   and return error kind `UNSUPPORTED`. **Never silently no-op, never fake a success result.**
4. Phases 1–4 must not change the public API that existing crawlers call. Backwards compatibility
   is a hard requirement (the product is in production).
5. Do not add a third-party dependency that is not listed in §12. If you need one, add it to §12
   in the same PR with a one-line justification.
6. Out of scope for this work: anti-bot / bot-detection evasion behaviour. See §1.3. Do not add
   fingerprint patching, stealth patches or detection-bypass code under this plan. If a task looks
   like it requires that, stop and ask.
7. When something is ambiguous, prefer the behaviour of Playwright's *public semantics* (not its
   implementation) as the tie-breaker, and record the decision in §13.

---

## 1. Problem, goal, non-goals

### 1.1 Current state

```
crawler / browser-use feature
        |   (direct calls, CDP types leak into business code)
        v
   CDP client
        |
        v
    Chromium
```

Problems this causes, regardless of anything else:

- CDP types and CDP semantics leak into crawler/business code, so the control mechanism cannot be
  swapped without touching business code.
- No single place to add timeouts, retries, tracing, quotas, crash recovery.
- No test suite defining "what the browser layer must do", so every change is a regression risk.

### 1.2 Goal

**Own the control plane.** The app talks to one stable, versioned, backend-agnostic interface.
CDP (or anything else) becomes a swappable implementation detail behind that interface.

Success = the backend implementation can be replaced without touching a single line of crawler code,
proven by running the same conformance suite against two different backends.

### 1.3 Non-goals (explicit)

- **This is not an anti-detection project.** Do not frame the goal as "CDP is detected, so use a
  socket and the site cannot detect it". Factually: the transport between *your process* and the
  browser is not something a web page reads. What a page can observe comes from the browser build and
  its runtime behaviour, not from whether your IPC is CDP-over-WebSocket or JSON-over-a-named-pipe.
  Changing transport alone changes nothing page-visible. Justify this project on control,
  testability, portability and stability — those reasons are real and sufficient on their own.
- Not a full CDP clone. See §4.3 for the bounded method set.
- Not a Chromium fork (that is at most Phase 5, gated by §8 benchmark results).
- No multi-machine / remote control in v1. Same-host only.

---

## 2. The decision the previous draft left open: the browser-side binding

The earlier draft drew `Browser Agent -> Chromium` as a single arrow. That arrow *is* the project.
An agent process cannot drive Chromium by wishing; it has to bind to it somehow. Make this an
explicit, reversible decision:

| ID | Backend | How it drives Chromium | Cost | Fidelity | Notes |
|----|---------|------------------------|------|----------|-------|
| **B0** | **CDP adapter** | agent speaks CDP internally | Low (days) | Full | **Ships first. Mandatory.** Reference implementation and benchmark baseline. |
| B1 | Extension (MV3) | `chrome.tabs` / `chrome.scripting` / `chrome.webRequest` + content scripts, over native messaging or a local socket | Medium (weeks) | Partial: no true trusted input synthesis, weaker frame/network control, MV3 service-worker lifecycle issues | Viable for navigate / DOM / evaluate / cookies; must return `UNSUPPORTED` for parts of §4.3 |
| B2 | Embedder API | CEF / WebView2 / custom Chromium embedder host process | High (months) | High, and you own the host app | Only worth it if you already ship an embedder |
| B3 | Patched Chromium + in-process agent (Mojo) | build-level integration | Very high (quarters) | Highest | Gate behind §8 results plus a written ADR |

**Rule:** B0 ships in Phase 2 and stays forever as the reference/fallback. B1/B2/B3 are added later
behind the same interface. Everything in Phases 0–4 is fully reusable no matter which backend wins —
which is exactly why it goes first.

**Capability negotiation is mandatory**, because backends genuinely differ (§4.2.6).

---

## 3. Architecture and repo layout

```
   crawler / browser-use feature          <- business code, backend-agnostic
                |
       high-level API (Page, Locator, Network, Storage)      §5 layer 2
                |
        IBrowserTransport                 <- the seam            §5 layer 1
         |-- CdpTransport    (legacy, in-process; deleted after Phase 4)
         +-- SocketTransport (BCP wire protocol, §4.2)
                |  local IPC (unix socket / named pipe)
                v
        bcp-agent  (separate process, one per browser profile)
         |-- SessionManager   (handshake, auth, capabilities, quotas)
         |-- TargetManager    (tabs, popups, workers)
         |-- FrameManager     (frame tree, execution contexts / worlds)
         |-- RuntimeManager   (evaluate, handles, init scripts)
         |-- DomManager       (query, snapshot, actionability support)
         |-- InputManager     (mouse / keyboard / scroll / upload)
         |-- NetworkManager   (events, bodies, optional interception)
         +-- backends/ { cdp/, extension/, embedder/ }        <- §2
                v
             Chromium
```

> Naming: call the process **`bcp-agent`**, not "Browser Agent". "Agent" already means the LLM agent
> in browser-use; that collision will confuse both humans and coding tools reading this repo.

Layout:

```
browser-control-plane/
  protocol/
    schema.json            # SOURCE OF TRUTH: methods, params, results, events, errors
    CHANGELOG.md
    gen/                   # generated types - do not hand-edit
  client/
    transport/ { itransport, socket_transport, cdp_transport }
    api/       { browser, page, frame, locator, network, storage }
  agent/
    main                   # process entry, socket server
    managers/ { session, target, frame, runtime, dom, input, network }
    backends/ { cdp/, extension/, embedder/ }
  conformance/
    fixtures/              # local static + dynamic test site (§6.1)
    specs/                 # backend-agnostic test suite (§6.2)
  bench/                   # §8
```

---

## 4. Contracts (authoritative)

### 4.1 Object model

```
Browser
 +-- Target             targetId   (page | popup | oopif | worker)
      +-- Frame         frameId    (tree; one mainFrame per target)
           +-- ExecutionContext   contextId, world = "main" | "isolated"
                +-- Handle        handleId   (remote object / DOM node reference)
```

Invariants that must be honoured:

- `handleId` is scoped to an ExecutionContext. On `frameNavigated` or context destruction, all handles
  of that context are invalidated; using one afterwards returns `FRAME_DETACHED` or `HANDLE_INVALID`
  — never a crash, never a stale result.
- Handles are leaked browser memory until released. The client API auto-releases on scope exit; the
  agent releases all handles of a target when the target closes.
- Every method that touches page state takes `targetId`, plus `frameId` where a frame is meaningful.
  Omitted `frameId` means the main frame.

### 4.2 Wire protocol

#### 4.2.1 Transport

- Linux/macOS: Unix domain socket at `$RUNTIME_DIR/bcp/<profileId>.sock`, mode `0600`.
- Windows: named pipe `\\.\pipe\bcp-<profileId>`, DACL restricted to the current user SID.
- TCP is **off by default**. If it is ever enabled: bind `127.0.0.1` only, token required,
  never `0.0.0.0`.

#### 4.2.2 Framing

Every frame: `[uint32 BE length][uint8 kind][payload]`

- `kind = 0` — payload is UTF-8 JSON (envelope, §4.2.3)
- `kind = 1` — payload is `[16-byte attachmentId][raw bytes]` (screenshots, response bodies, downloads)

Rationale: never base64 megabytes through JSON. Max frame size 32 MiB (configurable); exceeding it is
error `PAYLOAD_TOO_LARGE`, not a disconnect.

#### 4.2.3 Envelopes

Request:

```json
{ "id": 42, "method": "page.navigate",
  "params": { "targetId": "t1", "url": "https://example.com", "waitUntil": "load" },
  "timeoutMs": 30000 }
```

Response, success:

```json
{ "id": 42, "ok": true, "result": { "frameId": "f1", "httpStatus": 200 } }
```

Response, failure:

```json
{ "id": 42, "ok": false,
  "error": { "kind": "TIMEOUT", "code": 4001,
             "message": "navigate exceeded 30000ms",
             "retryable": true,
             "data": { "lastLifecycle": "domcontentloaded" } } }
```

Event:

```json
{ "event": "page.lifecycle", "seq": 1187,
  "params": { "targetId": "t1", "frameId": "f1", "phase": "load" } }
```

Cancellation:

```json
{ "id": 43, "method": "$cancel", "params": { "cancelId": 42 } }
```

The agent must genuinely abort the in-flight operation and answer request 42 with kind `CANCELLED`.

#### 4.2.4 Rules

- `id` is client-generated and monotonic per connection. Responses may arrive out of order.
- Multiple in-flight requests are required; **no head-of-line blocking**. Serialize per target only,
  and only for input and navigation (one target must never process two navigations concurrently).
- `seq` is a monotonic per-connection counter over events. A gap means the client lost events and
  must resync by re-reading state, never continue silently.
- `timeoutMs` is mandatory on every request. On expiry the agent replies `TIMEOUT`; it must never drop
  a request without a response.
- Backpressure: bounded event queue per subscription. On overflow the agent drops events and emits
  `agent.eventsDropped { count, sinceSeq }`. **Silent dropping is a bug.**

#### 4.2.5 Handshake

First message must be `agent.hello`:

```json
{ "id": 1, "method": "agent.hello",
  "params": { "protocolVersion": "1.0", "client": "crawler/2.3.0", "token": "<from token file>" } }
```

Reply:

```json
{ "id": 1, "ok": true,
  "result": { "protocolVersion": "1.0", "backend": "cdp",
              "capabilities": ["network.interception", "input.trusted", "page.download"],
              "methods": ["page.navigate", "..."] } }
```

Any other method before a successful hello returns `UNAUTHENTICATED`, then the connection closes.

#### 4.2.6 Versioning and capabilities

- SemVer on `protocolVersion`. Minor versions are additive only. The client refuses a major mismatch.
- The client must **query capabilities, not sniff the backend name**. Business code branching on
  `backend == "cdp"` is forbidden; branch on capability strings.

### 4.3 Method catalog v1

About 45 primitives. The earlier "30–50 primitives" estimate was right; the list below is what real
sites actually require. Frames, dialogs, popups and actionability are what break crawlers — not the
raw count.

**agent** — `agent.hello`, `agent.ping`, `agent.stats`, `agent.shutdown`

**target** — `target.list`, `target.create{url,incognito?}`, `target.activate`, `target.close`

**page** — `page.navigate{targetId,url,waitUntil,referrer?}`, `page.reload`, `page.goBack`,
`page.goForward`, `page.waitForLoadState{state}`, `page.content{frameId?}`,
`page.screenshot{format,quality?,fullPage?,clip?}` (returns an attachment),
`page.setViewport{width,height,dpr,mobile}`, `page.frames` (frame tree),
`page.handleDialog{action,promptText?}`, `page.setDownloadBehavior{mode,path}`

**runtime** — `runtime.evaluate{frameId?,world,expression,awaitPromise,returnByValue}`,
`runtime.callFunction{handleId,functionDeclaration,args[]}`, `runtime.releaseHandle`,
`runtime.addInitScript{source,world}` (needed for instrumentation, e.g. capturing fetch/XHR on
backends that have no native network layer)

**dom** — `dom.query{frameId,selector,engine:css|xpath|text}`, `dom.queryAll`,
`dom.waitForSelector{selector,state:attached|visible|hidden|detached}`, `dom.attributes{handleId}`,
`dom.text`, `dom.html`, `dom.boundingBox`, `dom.scrollIntoView`,
`dom.snapshot{frameId,mode:flat|accessibility,maxNodes}`

> `dom.snapshot` is the serialization browser-use feeds to an LLM. Treat it as a first-class method
> with a stable, versioned schema — not an ad-hoc `evaluate` blob. Its output shape is part of the
> protocol and changing it is a protocol change.

**input** — `input.click{handleId|point,button,clickCount,modifiers}`, `input.hover`,
`input.type{text,delayMs}`, `input.press{key,code,modifiers}`, `input.scroll{dx,dy,point?}`,
`input.dragAndDrop{from,to}`, `input.uploadFiles{handleId,paths[]}`

**network** — `network.enable{captureBodies:none|text|all,maxBodyBytes}`, `network.disable`,
`network.getBody{requestId}` (attachment);
capability-gated: `network.setInterception{patterns}`, `network.continue`, `network.fulfill`,
`network.abort`

**storage** — `storage.getCookies{urls?}`, `storage.setCookies`, `storage.clearCookies`,
`storage.getLocal{frameId}`, `storage.setLocal`, `storage.getSession`,
`storage.exportState` / `storage.importState` (whole-profile snapshot, for profile persistence)

### 4.4 Event catalog v1

- `target.created` | `target.destroyed` | `target.crashed`
- `page.lifecycle{phase: commit|domcontentloaded|load|networkidle}`
- `page.frameAttached` | `page.frameNavigated` | `page.frameDetached`
- `page.dialog{type,message}` — blocks the page until `page.handleDialog`
- `page.download{suggestedFilename,url,state}`
- `page.console{level,text,args}` | `page.pageError{message,stack}`
- `runtime.contextCreated` | `runtime.contextDestroyed`
- `network.request{requestId,url,method,headers,resourceType}`
- `network.response{requestId,status,headers,fromCache}`
- `network.finished{requestId,encodedLength}` | `network.failed{requestId,errorText}`
- `agent.eventsDropped{count,sinceSeq}`

### 4.5 Error taxonomy (`error.kind`)

`BAD_REQUEST`, `UNAUTHENTICATED`, `UNSUPPORTED`, `PAYLOAD_TOO_LARGE`, `TARGET_NOT_FOUND`,
`TARGET_CRASHED`, `FRAME_DETACHED`, `HANDLE_INVALID`, `NAVIGATION_ABORTED`, `TIMEOUT`, `CANCELLED`,
`JS_EXCEPTION`, `ELEMENT_NOT_FOUND`, `ELEMENT_NOT_ACTIONABLE`, `NETWORK_ERROR`, `TRANSPORT_CLOSED`,
`INTERNAL`

Each error carries `retryable: bool`. Client retry policy keys off `kind` plus `retryable` only —
never off message text.

### 4.6 Waiting semantics (define these precisely, or the port will be flaky)

**`waitUntil` values**

- `commit` — navigation committed, response headers received
- `domcontentloaded`
- `load`
- `networkidle` — deterministic definition: **no more than `maxInflight` (default 0) in-flight HTTP
  requests for a continuous `idleMs` (default 500 ms)**, excluding WebSocket, EventSource, and any
  request older than `longPollMs` (default 30 s). Always bounded by the request `timeoutMs`.

**Actionability** — required before `input.click`, `input.type`, `input.hover`; retried until timeout:

1. attached to the DOM
2. visible — non-empty bounding box, not `display:none` / `visibility:hidden` / `opacity:0`
3. stable — bounding box unchanged across 2 consecutive animation frames
4. hit-testable — `elementFromPoint(center)` is the element or a descendant of it
5. enabled — not `disabled`, not `aria-disabled="true"`, not `pointer-events:none`

On timeout, return `ELEMENT_NOT_ACTIONABLE` with `data.failedCheck` naming which of the five failed.
This single behaviour causes most "works locally, flaky in prod" reports. Implement it once, here —
not scattered through crawler code.

---

## 5. Client-side API

Two layers. Business code must never touch layer 1.

**Layer 1 — `IBrowserTransport`** (thin, 1:1 with the wire):

```
connect(endpoint, token) -> HelloResult
call(method, params, opts{timeoutMs, signal}) -> result | throws BcpError
on(event, handler) -> unsubscribe
capabilities: Set<string>
close()
```

Implementations: `SocketTransport` (§4.2) and `CdpTransport` (in-process, legacy, Phase 1 only).

**Layer 2 — high-level API, what crawlers use**: `Browser`, `Page`, `Frame`, `Locator`, `Network`,
`Storage`. This layer owns retries, actionability waiting, handle lifetime and tracing.

Reference shape in TypeScript. If the stack is Python, mirror it as an ABC with `async def` and
snake_case names — identical names and semantics, only the casing changes:

```ts
interface Page {
  goto(url: string, o?: {waitUntil?: WaitUntil; timeoutMs?: number}): Promise<Response | null>;
  evaluate<T>(fn: string | Function, ...args: unknown[]): Promise<T>;
  content(): Promise<string>;
  locator(selector: string, o?: {engine?: 'css' | 'xpath' | 'text'}): Locator;
  waitForSelector(sel: string, o?: {state?: ElementState; timeoutMs?: number}): Promise<Locator>;
  snapshot(o?: {mode?: 'flat' | 'accessibility'; maxNodes?: number}): Promise<DomSnapshot>;
  screenshot(o?: ScreenshotOptions): Promise<Uint8Array>;
  on(event: PageEvent, cb: Handler): Unsubscribe;
  close(): Promise<void>;
}
```

**Mandatory client behaviours**

- Every call carries a `traceId`; log `traceId, method, targetId, durationMs, error.kind`.
- Auto-reconnect with exponential backoff. After a reconnect, state is re-read, never assumed.
- Handles are garbage-collected on scope exit.
- No CDP type appears above layer 1. Add a CI rule (a grep is enough) that fails the build if `CDP` /
  `Protocol.` symbols appear outside `client/transport/cdp_transport` and `agent/backends/cdp`.

---

## 6. Conformance suite — the actual deliverable

One suite, run unchanged against every backend. This is what turns the abstraction from aspiration
into something enforced.

### 6.1 Fixture site (local, hermetic — no third-party sites in tests)

Static server plus dynamic endpoints covering:

`basic-static`, `spa-pushstate-nav`, `redirect-chain-3x`, `same-origin-iframe`, `cross-origin-iframe`,
`shadow-dom`, `infinite-scroll-lazy`, `dialog-alert-confirm-prompt`, `popup-window`, `download-file`,
`form-with-file-upload`, `cookie-set-httponly`, `slow-endpoint-5s`, `xhr-json`, `fetch-stream`,
`websocket-echo`, `js-error-page`, `crash-page`, `5k-node-table`.

### 6.2 Spec matrix

For each fixture x method group, assert identical observable results across backends:

| Spec | cdp | extension | ... |
|------|-----|-----------|-----|
| navigate / waitUntil=load | PASS | PASS | |
| dom.snapshot / 5k-node | PASS | PASS | |
| input.click / actionability | PASS | UNSUPPORTED (declared) | |

`UNSUPPORTED` is acceptable only when that capability was declared in `agent.hello`.
An undeclared `UNSUPPORTED` is a FAIL.

### 6.3 Fault injection (required, not optional)

Kill `bcp-agent` mid-call; kill the browser mid-navigation; drop the socket mid-response; oversized
response body; 200 concurrent evaluates; target crash during `dom.snapshot`.

Expected in every case: a typed error and automatic recovery. **Zero hangs, zero silent wrong
results.**

---

## 7. Phases and Definition of Done

### Phase 0 — Inventory and seam (1–3 days)

- Grep every CDP call site in the app. Produce `docs/cdp-inventory.md`: call site -> CDP
  domain/method -> which BCP method replaces it -> notes.
- Any CDP usage with no BCP equivalent goes to §13 open questions before the catalog is frozen.
- **DoD:** inventory reviewed; §4.3 catalog frozen for v1; `protocol/schema.json` v1.0.0 committed.

### Phase 1 — Abstraction over the existing CDP (about 1 week)

- Implement layer 1 and layer 2 with **`CdpTransport` only** — still in-process, no socket yet.
- Migrate all crawler code onto layer 2. Turn on the CI rule from §5.
- **DoD:** zero CDP symbols outside the two allowed folders; existing crawlers pass their current
  tests unchanged; the conformance suite is green against `CdpTransport`.

> Doing this before building the agent is what de-risks the whole project: after Phase 1 you can stop
> at any point and still have shipped something valuable.

### Phase 2 — `bcp-agent` process and `SocketTransport` (2–3 weeks)

- Agent process, socket server, handshake/auth, all managers, backend **B0 (cdp)**.
- `SocketTransport` on the client.
- One agent per browser profile; lifecycle tied to the profile (spawn, health check, restart, reap).
- **DoD:** conformance suite green against `SocketTransport + cdp` with results identical to Phase 1;
  fault injection (§6.3) green; security requirements (§10) implemented.

### Phase 3 — Migrate one real crawler and benchmark (about 1 week)

- Pick the smallest production crawler. Run it on both transports in shadow mode (§9).
- Fill in the §8 benchmark table with real numbers.
- **DoD:** shadow-mode output diff = 0 over at least 1000 pages; §8 thresholds met, or a written
  waiver signed off by the owner.

### Phase 4 — Full migration and CDP retirement (1–2 weeks)

- Flip the remaining crawlers; keep the flag for one release; then delete `CdpTransport` from the
  client. The CDP *backend* inside the agent stays — it is the reference implementation.
- **DoD:** the flag defaults to `agent` for 100% of profiles; one full release ships with no rollback.

### Phase 5 — Alternative backend, only if justified (open-ended)

- Requires: a written justification tied to a §1.2 goal, a §8 result showing the current backend is
  the actual blocker, and the conformance suite as the acceptance gate.
- Implement B1 first (cheapest). B2/B3 need their own ADR. **Do not start Phase 5 by forking
  Chromium.**

---

## 8. Benchmark spec

Rules: local fixtures only (§6.1); N >= 200 iterations with 20 warm-up runs discarded; report
p50/p95/p99, never averages; same machine, same Chromium build, no other load; each backend measured
with an identical script.

| Metric | Baseline (cdp in-proc) | Candidate (socket + cdp) | Threshold |
|---|---|---|---|
| `evaluate("1+1")` round-trip | measure | measure | p95 <= baseline + 15 ms |
| `page.navigate`, waitUntil=load | measure | measure | p95 <= baseline x 1.10 |
| `dom.snapshot`, 5k nodes | measure | measure | p95 <= baseline x 1.25 |
| `page.content`, 1 MB HTML | measure | measure | p95 <= baseline x 1.25 |
| 20 pages concurrent, throughput | measure | measure | >= baseline x 0.90 |
| RSS per open page | measure | measure | <= baseline + 15 MB |
| Recovery time after browser crash | measure | measure | <= 5 s to usable |
| Sustained 1 h, 10k navigations | leak check | leak check | RSS drift < 10% |

Thresholds are proposals — TUNE them once the baseline exists, but freeze them **before** Phase 3 so
the result cannot be rationalized after the fact.

---

## 9. Migration and rollout

- Flag `BROWSER_TRANSPORT = cdp | agent`, resolvable **per profile** (not global), hot-readable.
- **Shadow mode:** run the crawler on `cdp` and mirror the same step sequence on `agent`, compare
  normalized extracted output, log diffs, and serve only the `cdp` result. Ship only at diff rate 0.
- Kill switch: one config change reverts to `cdp` without a redeploy. Keep it for one full release.
- Telemetry per transport: success rate, p95 per method, error-kind histogram, crash/restart count.
- Write the rollback trigger up front, e.g. "success rate drops more than 2 pp, or p95 navigate
  exceeds 1.5x baseline, for 30 minutes".

---

## 10. Security requirements (non-negotiable)

The control plane can execute arbitrary JS in any logged-in page and read cookies. Treat it that way.

- Socket/pipe restricted to the current user (`0600` / user-only DACL). No TCP by default.
- Handshake token: random, at least 32 bytes, stored in a user-only-readable file, rotated on every
  agent start, compared in constant time. Reject and close on mismatch.
- Max frame size, max in-flight requests per connection, max event queue depth — all enforced with
  typed errors, never by crashing.
- The agent never accepts a filesystem path from an untrusted source for `input.uploadFiles` or
  `page.setDownloadBehavior`; both are restricted to a configured allow-listed directory.
- Agent logs redact cookie values, `Authorization` headers and request bodies by default.
- The agent process runs with the same privileges as the app — never elevated.

---

## 11. Observability

- Structured logs: `traceId, profileId, targetId, method, durationMs, ok, error.kind`.
- Counters: requests by method, errors by kind, events dropped, reconnects, agent restarts.
- `agent.stats` returns: open targets, in-flight requests, handles alive, event queue depth, RSS.
- A `--trace` mode that dumps the full frame stream to a file. It pays for itself during Phase 3.

---

## 12. Dependencies (append here before adding any)

- JSON schema validator / codegen: <fill>
- CDP client library (agent backend B0 only): <fill>
- Test runner: <fill>
- Static file server for fixtures: <fill>

---

## 13. Open questions — answer before Phase 2 starts

1. Language/runtime for `bcp-agent` and for the client? The protocol is neutral; the code is not.
2. Process model: one agent per browser profile (recommended) or one agent serving many profiles?
   Who spawns and supervises it — the app, or the OS service manager?
3. Does any current crawler require `network.setInterception`? If not, ship it capability-gated and
   unimplemented in v1.
4. Does any crawler need cross-origin iframe (OOPIF) content today? This changes B1's viability.
5. Downloads: must files land on disk, or is a byte stream sufficient?
6. Which single crawler is the Phase 3 pilot?
7. From the Phase 0 inventory: which CDP call sites have no BCP equivalent, and what happens to them?

---

## 14. Task checklist

```
T1  Phase0  CDP call-site inventory -> docs/cdp-inventory.md
T2  Phase0  Freeze §4.3 catalog; write protocol/schema.json v1.0.0            (dep T1)
T3  Phase0  Codegen pipeline: schema -> client types + agent handler stubs    (dep T2)
T4  Phase1  IBrowserTransport + CdpTransport                                  (dep T3)
T5  Phase1  High-level API: Browser/Page/Frame/Locator/Network/Storage        (dep T4)
T6  Phase1  Actionability engine §4.6 (client-side, backend-agnostic)         (dep T5)
T7  Phase1  Migrate all crawler call sites onto layer 2                       (dep T5)
T8  Phase1  CI rule: no CDP symbols outside allowed folders                   (dep T7)
T9  Phase1  Fixture site §6.1                                                 (dep T2)
T10 Phase1  Conformance suite §6.2, green on CdpTransport                     (dep T9,T5)
T11 Phase2  Agent skeleton: socket server, framing, envelopes, hello          (dep T3)
T12 Phase2  SessionManager: auth, capabilities, quotas, §10                   (dep T11)
T13 Phase2  TargetManager + FrameManager + lifecycle events                   (dep T11)
T14 Phase2  RuntimeManager: evaluate, handles, worlds, init scripts           (dep T13)
T15 Phase2  DomManager: query, waitForSelector, snapshot                      (dep T14)
T16 Phase2  InputManager                                                      (dep T15)
T17 Phase2  NetworkManager: events + bodies (+ interception if Q3 = yes)      (dep T13)
T18 Phase2  StorageManager                                                    (dep T13)
T19 Phase2  Backend B0 (cdp) wiring every manager                             (dep T13..T18)
T20 Phase2  SocketTransport client + reconnect/backoff/tracing                (dep T11)
T21 Phase2  Conformance green on SocketTransport + cdp                        (dep T19,T20,T10)
T22 Phase2  Fault-injection suite §6.3                                        (dep T21)
T23 Phase3  Benchmark harness §8 + baseline numbers                           (dep T21)
T24 Phase3  Shadow mode + diff reporting                                      (dep T21)
T25 Phase3  Pilot crawler migration, >=1000 pages, diff = 0                   (dep T24)
T26 Phase4  Flag rollout, telemetry, rollback trigger                         (dep T25)
T27 Phase4  Remove CdpTransport from the client                               (dep T26)
T28 Phase5  (gated) Backend B1 extension - same conformance suite             (dep T21)
```

---

## Appendix A — worked message examples

Navigate, then read the title:

```
--> {"id":10,"method":"page.navigate","params":{"targetId":"t1","url":"https://example.com","waitUntil":"load"},"timeoutMs":30000}
<-- {"event":"page.lifecycle","seq":501,"params":{"targetId":"t1","frameId":"f1","phase":"commit"}}
<-- {"event":"page.lifecycle","seq":502,"params":{"targetId":"t1","frameId":"f1","phase":"domcontentloaded"}}
<-- {"event":"page.lifecycle","seq":503,"params":{"targetId":"t1","frameId":"f1","phase":"load"}}
<-- {"id":10,"ok":true,"result":{"frameId":"f1","httpStatus":200}}
--> {"id":11,"method":"runtime.evaluate","params":{"targetId":"t1","world":"main","expression":"document.title","returnByValue":true},"timeoutMs":5000}
<-- {"id":11,"ok":true,"result":{"value":"Example Domain"}}
```

Screenshot with a binary attachment:

```
--> {"id":12,"method":"page.screenshot","params":{"targetId":"t1","format":"png","fullPage":true},"timeoutMs":15000}
<-- kind=1 frame: [attachmentId=9f3c...][PNG bytes]
<-- {"id":12,"ok":true,"result":{"attachmentId":"9f3c...","width":1280,"height":4210}}
```

Click on a non-actionable element:

```
--> {"id":13,"method":"input.click","params":{"targetId":"t1","handleId":"h7"},"timeoutMs":5000}
<-- {"id":13,"ok":false,"error":{"kind":"ELEMENT_NOT_ACTIONABLE","code":4210,
     "message":"element not hit-testable after 5000ms","retryable":true,
     "data":{"failedCheck":"hit-test","occludedBy":"div.cookie-banner"}}}
```
