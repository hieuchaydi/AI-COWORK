const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');

async function worker(options = {}) {
  const frames = [], fetches = [], timers = new Map(), timeouts = new Map(), sockets = [];
  const stored = { ...options.stored };
  let nextTimer = 0;
  let messageListener = null;
  const listener = { addListener() {} };
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
    send(raw) {
      const frame = JSON.parse(raw);
      frames.push(frame);
      if (frame.type === 'ingest.rpc' && options.autoReply !== false) queueMicrotask(() => this.onmessage({ data: JSON.stringify({
        type: 'ingest.reply', id: frame.id, ok: true, result: { saved: true },
      }) }));
    }
    close() { this.readyState = 3; }
  }
  const context = vm.createContext({
    console, URL, WebSocket: Socket, crypto: { randomUUID }, AbortController,
    setTimeout: (fn, ms) => { timeouts.set(++nextTimer, { fn, ms }); return nextTimer; },
    clearTimeout: id => timeouts.delete(id),
    setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearInterval: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      fetches.push(url);
      assert.match(url, /\/browser\/pair$/);
      assert.equal(options.headers['X-Bridge-Client'], 'ai-cowork-bridge');
      return { ok: true, json: async () => ({ token: 'new-token', wsUrl: 'ws://127.0.0.1:8766/browser/v1/ws' }) };
    },
    chrome: {
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
      storage: { local: {
        get: (keys, callback) => {
          const result = { ...stored };
          if (typeof callback === 'function') {
            callback(result);
            return;
          }
          return Promise.resolve(result);
        },
        set: async value => { Object.assign(stored, value); },
        remove: async keys => {
          for (const key of Array.isArray(keys) ? keys : [keys]) delete stored[key];
        },
      } },
      runtime: {
        onInstalled: listener,
        onStartup: listener,
        onMessage: { addListener: fn => { messageListener = fn; } },
      },
      alarms: { onAlarm: listener, create() {} },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser-extension/background.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return { context, frames, fetches, timers, timeouts, sockets, stored, getMessageListener: () => messageListener };
}

test('review results finalize from server checkpoint without a large WebSocket payload', async () => {
  const { context, frames, fetches, timers } = await worker();
  await vm.runInContext('reportProgress({id: "job-1"}, {percent: 20})', context);
  await vm.runInContext('uploadIngestResult({job: "job-1", rows: [{text: "x".repeat(180000)}]})', context);
  const operations = frames.map(frame => frame.params.operation);
  assert.deepEqual(operations, ['progress', 'finalize']);
  assert.equal(Object.hasOwn(frames[1].params, 'rows'), false);
  assert.equal(fetches.length, 1); // Only bootstrap pairing uses HTTP.
  assert.equal(timers.size, 0);
});

test('duplicate pushed jobs are acknowledged but execute only once', async () => {
  const { context, frames } = await worker();
  vm.runInContext('globalThis.runs = 0; runJob = async () => { runs++; }; enqueueLegacyJob({id: "same"}); enqueueLegacyJob({id: "same"});', context);
  await vm.runInContext('jobChain', context);
  assert.equal(context.runs, 1);
  assert.equal(frames.filter(frame => frame.type === 'accepted').length, 2);
});

test('active job replays still receive an accepted ACK without starting twice', async () => {
  const { context, frames } = await worker();
  vm.runInContext('activeJobs.set("active-job", {id: "active-job"}); enqueueLegacyJob({id: "active-job"});', context);
  assert.equal(frames.filter(frame => frame.type === 'accepted' && frame.id === 'active-job').length, 1);
  assert.equal(vm.runInContext('activeJobs.size', context), 1);
});

test('getStatus reports the live service-worker socket instead of stale storage', async () => {
  const { context, sockets, stored, getMessageListener } = await worker({ stored: {
    extensionState: 'disconnected',
    lastConnectionError: 'stale error',
  } });
  sockets[0].onopen();
  stored.extensionState = 'disconnected';
  stored.lastConnectionError = 'stale error';
  let response;
  const listener = getMessageListener();
  assert.equal(listener({ action: 'getStatus' }, {}, value => { response = value; }), true);
  assert.equal(response.ok, true);
  assert.equal(response.connected, true);
  assert.equal(response.extensionState, 'connected');
  assert.equal(response.lastConnectionError, 'stale error');
  assert.equal(stored.extensionState, 'disconnected');
});

test('completed ingest result is persisted for popup download links', async () => {
  const { context, stored, getMessageListener } = await worker();
  const result = await vm.runInContext(`rememberCompletedResult(
    { id: "job-downloads", url: "https://shopee.vn/product/93922606/1546910319" },
    {
      ok: true,
      count: 6789,
      partial: true,
      crawl_summary: { expected: 6819, collected: 6789, complete: false },
      csv: "outputs/csv/shopee_1546910319_reviews.csv",
      media_dir: "outputs/media/shopee_1546910319_reviews",
      manifest: "outputs/media/shopee_1546910319_reviews/manifest.json",
      zip: "outputs/zips/shopee_1546910319_reviews_media_part01.zip",
      zip_url: "/outputs/zips/shopee_1546910319_reviews_media_part01.zip",
      zip_parts: [
        "outputs/zips/shopee_1546910319_reviews_media_part01.zip",
        "outputs/zips/shopee_1546910319_reviews_media_part02.zip"
      ],
      zip_urls: [
        "/outputs/zips/shopee_1546910319_reviews_media_part01.zip",
        "/outputs/zips/shopee_1546910319_reviews_media_part02.zip"
      ],
      report_url: "/outputs/text/shopee_1546910319_reviews_report.md",
      rows: [{ should_not_be_persisted: true }]
    }
  )`, context);

  assert.equal(result.count, 6789);
  assert.equal(result.partial, true);
  assert.equal(result.crawl_summary.expected, 6819);
  assert.equal(result.zip_urls.length, 2);
  assert.equal(stored.lastCompletedResult.jobId, 'job-downloads');
  assert.equal(stored.lastCompletedResult.rows, undefined);

  let response;
  assert.equal(getMessageListener()({ action: 'getStatus' }, {}, value => { response = value; }), true);
  assert.equal(response.lastCompletedResult.csv, 'outputs/csv/shopee_1546910319_reviews.csv');
  assert.equal(response.lastCompletedResult.zip_urls.length, 2);
});

test('starting a new job clears stale Shopee attention from the popup', async () => {
  const { context, stored } = await worker();
  vm.runInContext(`
    updateState("api_blocked", { kind: "api_blocked", job_id: "old-job" });
    updateState("busy");
  `, context);
  assert.equal(stored.extensionState, 'busy');
  assert.equal(stored.verificationInfo, null);
});

test('duplicate owner errors stop reconnect and require an explicit takeover', async () => {
  const { context, sockets, stored } = await worker();
  await vm.runInContext(`dispatchEnvelope({
    type: 'error',
    error: {
      code: 'CLIENT_ALREADY_CONNECTED',
      message: 'Gateway đang được client khác sử dụng',
      details: { owner: { clientId: 'owner-1' }, policy: 'exclusive' },
    },
  })`, context);
  assert.equal(stored.connectionEnabled, false);
  assert.equal(stored.extensionState, 'client_conflict');
  assert.equal(stored.connectionConflict.owner.clientId, 'owner-1');
  assert.equal(sockets[0].readyState, 3);
});

test('unacknowledged requests are replayed with the same correlation id', async () => {
  const { context, frames, timers } = await worker();
  vm.runInContext('bridgeSocket.readyState = 3', context);
  const result = vm.runInContext('reportProgress({id: "job-2"}, {percent: 30})', context);
  assert.equal(frames.length, 0);
  vm.runInContext('bridgeSocket.readyState = 1', context);
  for (const retry of timers.values()) retry();
  await result;
  assert.equal(frames.length, 1);
  assert.equal(timers.size, 0);
});

test('transient socket close preserves pending ingest RPC for reconnect replay', async () => {
  const { context, timers } = await worker({ autoReply: false });
  const pending = vm.runInContext('sendIngestRequest({operation: "checkpoint", job: "job-3"})', context);
  vm.runInContext('closeBridgeSocket({preserveDetails: true})', context);
  assert.equal(vm.runInContext('pendingIngestRequests.size', context), 1);
  assert.equal(timers.size, 1);
  vm.runInContext('clearPendingIngestRequests("test cleanup")', context);
  await assert.rejects(pending, /test cleanup/);
});


test('manual disconnect persists and prevents alarm reconnect', async () => {
  const { context, sockets, stored, timeouts } = await worker();
  await vm.runInContext('configureConnection({action: "disconnect"})', context);
  await vm.runInContext('connectBridge()', context);
  assert.equal(sockets.length, 1);
  assert.equal(stored.connectionEnabled, false);
  assert.equal(timeouts.size, 0);
  const restarted = await worker({ stored });
  assert.equal(restarted.sockets.length, 0);
});

test('connect saves new configuration before pairing and clears stale callbacks', async () => {
  const { context, sockets, fetches, timeouts } = await worker();
  const oldOpen = sockets[0].onopen;
  const oldClose = sockets[0].onclose;
  await vm.runInContext('configureConnection({action: "connect", url: "ws://localhost:9876/browser/v1/ws", token: "old"})', context);
  assert.equal(fetches.at(-1), 'http://localhost:9876/browser/pair');
  const connectedUrl = new URL(sockets[1].url);
  assert.equal(connectedUrl.searchParams.get('token'), 'new-token');
  assert.match(connectedUrl.searchParams.get('client_id'), /^[0-9a-f-]{36}$/);
  assert.equal(connectedUrl.searchParams.get('takeover'), null);
  oldOpen();
  oldClose();
  assert.equal(sockets[1].readyState, 1);
  assert.equal(timeouts.size, 1); // Only the new handshake deadline remains.
});

test('handshake timeout closes socket and schedules reconnect', async () => {
  const { sockets, timeouts, stored } = await worker();
  [...timeouts.values()].find(entry => entry.ms === 10000).fn();
  assert.equal(sockets[0].readyState, 3);
  assert.match(stored.lastConnectionError, /10 giây/);
  assert.equal(timeouts.size, 1);
});

test('heartbeat detects an unresponsive gateway', async () => {
  const { context, sockets, timers, stored } = await worker();
  sockets[0].onopen();
  vm.runInContext('lastBridgeMessageAt = Date.now() - 60000', context);
  for (const heartbeat of timers.values()) heartbeat();
  assert.equal(sockets[0].readyState, 3);
  assert.match(stored.lastConnectionError, /heartbeat/);
});

test('disconnect during pairing invalidates the asynchronous attempt', async () => {
  const { context, sockets } = await worker();
  await vm.runInContext('configureConnection({action: "disconnect"})', context);
  let resolvePair;
  context.fetch = () => new Promise(resolve => { resolvePair = resolve; });
  const connecting = vm.runInContext('configureConnection({action: "connect"})', context);
  await new Promise(resolve => setImmediate(resolve));
  await vm.runInContext('configureConnection({action: "disconnect"})', context);
  resolvePair({ ok: true, json: async () => ({ token: 'late-token' }) });
  await connecting;
  assert.equal(sockets.length, 1);
});

test('classifies Shopee API 403 separately from login and traffic verification', async () => {
  const { context } = await worker();
  assert.equal(vm.runInContext(`classifyShopeeFailure({
    status: 403,
    url: 'https://shopee.vn/api/v2/item/get_ratings',
    pageUrl: 'https://shopee.vn/product/373956695/25018847315',
    textSample: '{"error":"access denied"}'
  })`, context), 'api_blocked');
  assert.equal(vm.runInContext(`classifyShopeeFailure({
    status: 403,
    url: 'https://shopee.vn/verify/traffic',
    textSample: 'challenge'
  })`, context), 'verification');
  assert.equal(vm.runInContext(`classifyShopeeFailure({
    status: 200,
    url: 'https://shopee.vn/api/v2/item/get_ratings',
    pageUrl: 'https://shopee.vn/verify/traffic?anti_bot_tracking_id=redacted',
    json: { is_login: true }
  })`, context), 'verification');
  assert.equal(vm.runInContext(`classifyShopeeFailure({
    status: 403,
    url: 'https://shopee.vn/api/v2/item/get_ratings',
    json: { error: 90309999, is_login: false },
    textSample: '{"error":90309999,"is_login":false}'
  })`, context), 'login');
});

test('buildTraceEntry includes all 13 required fields and sanitizes sensitive data', async () => {
  const { context } = await worker();
  const entry = vm.runInContext(`
    buildTraceEntry(
      { id: "job-abc-123", url: "https://shopee.vn/product/12345/67890?sp_atk=sensitive_token#header" },
      "ratings-response",
      {
        itemid: "67890",
        shopid: "12345",
        tabId: 42,
        tabUrl: "https://shopee.vn/product/12345/67890?auth=secret_auth",
        offset: 20,
        limit: 10,
        requestStart: 1700000000000,
        requestEnd: 1700000000250,
        status: 200,
        responseUrl: "https://shopee.vn/api/v2/item/get_ratings?itemid=67890&shopid=12345&token=sensitive_session_token",
        elapsedMs: 250,
        error: null,
      }
    )
  `, context);

  // 13 required fields check
  const requiredFields = [
    'jobId', 'itemid', 'shopid', 'tabId', 'tabUrl', 'event',
    'offset', 'limit', 'requestStart', 'requestEnd', 'httpStatus',
    'responseUrl', 'elapsedMs', 'error',
  ];
  for (const field of requiredFields) {
    assert.ok(field in entry, `Missing required field: ${field}`);
  }

  assert.equal(entry.jobId, 'job-abc-123');
  assert.equal(entry.itemid, '67890');
  assert.equal(entry.shopid, '12345');
  assert.equal(entry.tabId, 42);
  assert.equal(entry.event, 'ratings-response');
  assert.equal(entry.offset, 20);
  assert.equal(entry.limit, 10);
  assert.equal(entry.httpStatus, 200);
  assert.equal(entry.elapsedMs, 250);
  assert.equal(entry.error, null);

  // Sanitization checks: no raw token / auth / session / secret
  assert.ok(!entry.tabUrl.includes('secret_auth'));
  assert.ok(entry.tabUrl.includes('[REDACTED]'));
  assert.ok(!entry.responseUrl.includes('sensitive_session_token'));
  assert.ok(entry.responseUrl.includes('[REDACTED]'));

  // Ensure no sensitive review comments, reviewer username or media urls are present
  assert.equal(entry.comment, undefined);
  assert.equal(entry.review_text, undefined);
  assert.equal(entry.author, undefined);
  assert.equal(entry.image_url, undefined);
  assert.equal(entry.media_url, undefined);
});

test('traceJob emits progress trace envelope to bridge with sanitized error', async () => {
  const { context, frames } = await worker();
  await vm.runInContext(`
    traceJob(
      { id: "job-test-err", url: "https://shopee.vn/product/111/222" },
      (patch) => reportProgress({ id: "job-test-err" }, patch),
      "job-failed",
      {
        error: "HTTP 403 Forbidden with cookie=secret_cookie_val and <b>HTML</b> body",
      }
    )
  `, context);

  const progressFrames = frames.filter(f => f.params?.operation === 'progress');
  assert.ok(progressFrames.length >= 1);
  const lastProgress = progressFrames.at(-1).params.progress;
  assert.equal(lastProgress.stage, 'trace');
  assert.equal(lastProgress.trace.event, 'job-failed');
  assert.equal(lastProgress.trace.jobId, 'job-test-err');
  assert.ok(!lastProgress.trace.error.includes('secret_cookie_val'));
  assert.ok(lastProgress.trace.error.includes('[REDACTED]'));
  assert.ok(!lastProgress.trace.error.includes('<b>'));
});

test('validates Shopee hostname and rejects foreign origins', async () => {
  const { context } = await worker();
  assert.equal(vm.runInContext('isShopeeHostname("shopee.vn")', context), true);
  assert.equal(vm.runInContext('isShopeeHostname("mall.shopee.vn")', context), true);
  assert.equal(vm.runInContext('isShopeeHostname("shopee.vn.evil.com")', context), false);
  assert.equal(vm.runInContext('isShopeeHostname("google.com")', context), false);

  assert.equal(vm.runInContext('isValidShopeeUrl("https://shopee.vn/product/123/456")', context), true);
  assert.equal(vm.runInContext('isValidShopeeUrl("https://attacker.com/shopee.vn/product/123/456")', context), false);
});

test('evaluates preflight login detection and status reporting', async () => {
  const { context } = await worker();
  // 1. Logged in case
  const okEval = vm.runInContext(`
    evaluatePreflightResult({
      ok: true,
      status: 200,
      url: "https://shopee.vn/api/v2/item/get_ratings",
      json: { data: { ratings: [] } },
    })
  `, context);
  assert.equal(okEval.isLogin, true);
  assert.equal(okEval.status, 200);
  assert.equal(okEval.error, null);

  // 2. Error 90309999 login required case
  const loginErrEval = vm.runInContext(`
    evaluatePreflightResult({
      ok: true,
      status: 200,
      url: "https://shopee.vn/api/v2/item/get_ratings",
      json: { error: 90309999, is_login: false },
    })
  `, context);
  assert.equal(loginErrEval.isLogin, false);
  assert.match(loginErrEval.error, /login required/i);

  // 3. HTTP 401 login required case
  const unauthEval = vm.runInContext(`
    evaluatePreflightResult({
      ok: false,
      status: 401,
      url: "https://shopee.vn/api/v2/item/get_ratings",
      json: null,
    })
  `, context);
  assert.equal(unauthEval.isLogin, false);
  assert.match(unauthEval.error, /login required/i);

  // 4. Shopee may block the ratings endpoint while confirming the session is logged in.
  const blockedEval = vm.runInContext(`
    evaluatePreflightResult({
      ok: false,
      status: 403,
      url: "https://shopee.vn/api/v2/item/get_ratings",
      json: { error: 90309999, is_login: true, redirect_to_error_page: true },
      textSample: '{"error":90309999,"is_login":true}',
    })
  `, context);
  assert.equal(blockedEval.isLogin, true);
  assert.match(blockedEval.error, /api access denied/i);
  assert.equal(vm.runInContext(`
    classifyShopeeFailure({
      ok: false,
      status: 403,
      url: "https://shopee.vn/api/v2/item/get_ratings",
      json: { error: 90309999, is_login: true, redirect_to_error_page: true },
    })
  `, context), 'api_blocked');
});

test('ratings preflight and pagination execute in the Shopee page MAIN world', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.ratingScriptCalls = [];
    chrome.scripting = {
      executeScript: async options => {
        ratingScriptCalls.push(options);
        return [{ result: { ok: true, status: 200, json: { data: { ratings: [] } } } }];
      },
    };
  `, context);

  await vm.runInContext(`
    preflightRatingsInTab(42, "1546910319", "93922606", "https://shopee.vn/product/93922606/1546910319")
  `, context);
  await vm.runInContext(`
    fetchRatingsFromTab(42, "1546910319", "93922606", 0, 20, "https://shopee.vn/product/93922606/1546910319")
  `, context);

  const calls = vm.runInContext('ratingScriptCalls.map(call => ({ world: call.world, tabId: call.target.tabId }))', context);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    { world: 'MAIN', tabId: 42 },
    { world: 'MAIN', tabId: 42 },
  ]);
  assert.equal(vm.runInContext('PAGE_SIZE', context), 6);
  assert.equal(vm.runInContext('PACE_MS', context), 1200);
});

test('usable ratings preflight rejects 200 responses without review payload', async () => {
  const { context } = await worker();

  assert.equal(vm.runInContext(`
    isUsableRatingsPreflight({
      ok: true,
      status: 200,
      json: { data: null },
    })
  `, context), false);

  assert.equal(vm.runInContext(`
    isUsableRatingsPreflight({
      ok: true,
      status: 200,
      json: { data: {} },
    })
  `, context), false);

  assert.equal(vm.runInContext(`
    isUsableRatingsPreflight({
      ok: true,
      status: 200,
      json: { data: { ratings: [] } },
      textSample: "captcha challenge",
    })
  `, context), false);

  assert.equal(vm.runInContext(`
    isUsableRatingsPreflight({
      ok: true,
      status: 200,
      json: { data: { ratings: [] } },
    })
  `, context), true);

  assert.equal(vm.runInContext(`
    isUsableRatingsPreflight({
      ok: true,
      status: 200,
      json: { data: { item_rating_summary: { rating_total: 0 } } },
    })
  `, context), true);
});

test('Shopee job creates a browser window when MV3 worker has no current window', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.createdWindow = null;
    chrome.tabs = {
      query: async (query) => query && query.windowId
        ? [{ id: 77, windowId: query.windowId, status: "complete", url: "https://shopee.vn/product/111/222" }]
        : [],
      create: async () => { throw new Error("No current window"); },
      get: async (id) => ({ id, windowId: 9, status: "complete", url: "https://shopee.vn/product/111/222" }),
      onUpdated: { addListener() {}, removeListener() {} },
    };
    chrome.windows = {
      create: async options => {
        createdWindow = options;
        return {
          id: 9,
          tabs: [{ id: 77, windowId: 9, status: "complete", url: options.url }],
        };
      },
    };
  `, context);

  const tab = await vm.runInContext(`
    findOrOpenShopeeTab("https://shopee.vn/product/111/222", "222")
  `, context);
  assert.equal(tab.id, 77);
  assert.equal(tab.url, 'https://shopee.vn/product/111/222');
  assert.equal(vm.runInContext('createdWindow.focused', context), false);
});

test('ratings script retries when Chrome transiently returns no result', async () => {
  const { context, timeouts } = await worker();
  vm.runInContext(`
    globalThis.ratingAttempts = 0;
    chrome.scripting = {
      executeScript: async () => {
        ratingAttempts++;
        return ratingAttempts === 1
          ? []
          : [{ result: { ok: true, status: 200, json: { data: { ratings: [] } } } }];
      },
    };
  `, context);

  const pending = vm.runInContext(`
    fetchRatingsFromTab(42, "1546910319", "93922606", 0, 6, "https://shopee.vn/product/93922606/1546910319")
  `, context);
  await new Promise(resolve => setImmediate(resolve));
  const retryTimer = [...timeouts.values()].find(entry => entry.ms === 250);
  assert.ok(retryTimer, 'expected the first executeScript retry delay');
  retryTimer.fn();

  const result = await pending;
  assert.equal(result.ok, true);
  assert.equal(vm.runInContext('ratingAttempts', context), 2);
});

test('extractShopeeReviews halts with login_required before crawl when preflight fails login', async () => {
  const { context } = await worker();
  vm.runInContext(`
    chrome.tabs = {
      query: async () => [{ id: 10, url: "https://shopee.vn/product/111/222" }],
      create: async () => ({ id: 10, url: "https://shopee.vn/product/111/222" }),
      get: async () => ({ id: 10, url: "https://shopee.vn/product/111/222" }),
      onUpdated: { addListener() {}, removeListener() {} },
    };
    chrome.scripting = {
      executeScript: async () => {
        return [{
          result: {
            ok: true,
            status: 200,
            url: "https://shopee.vn/api/v2/item/get_ratings",
            json: { error: 90309999, is_login: false },
            textSample: '{"error":90309999,"is_login":false}',
          }
        }];
      }
    };
  `, context);

  await assert.rejects(async () => {
    await vm.runInContext(`
      extractShopeeReviews({
        id: "job-preflight-fail",
        url: "https://shopee.vn/product/111/222",
        itemid: "222",
      }, () => {})
    `, context);
  }, (err) => {
    assert.equal(err.failureKind, 'login');
    assert.match(err.message, /login required/i);
    return true;
  });
});

test('extractShopeeReviews reports api_blocked when Shopee confirms login on a 403 response', async () => {
  const { context } = await worker();
  vm.runInContext(`
    chrome.tabs = {
      query: async () => [{ id: 11, url: "https://shopee.vn/product/111/222" }],
      create: async () => ({ id: 11, url: "https://shopee.vn/product/111/222" }),
      get: async () => ({ id: 11, url: "https://shopee.vn/product/111/222" }),
      onUpdated: { addListener() {}, removeListener() {} },
    };
    chrome.scripting = {
      executeScript: async () => [{
        result: {
          ok: false,
          status: 403,
          url: "https://shopee.vn/api/v2/item/get_ratings",
          json: { error: 90309999, is_login: true, redirect_to_error_page: true },
          textSample: '{"error":90309999,"is_login":true}',
        }
      }],
    };
  `, context);

  await assert.rejects(async () => {
    await vm.runInContext(`
      extractShopeeReviews({
        id: "job-preflight-api-blocked",
        url: "https://shopee.vn/product/111/222",
        itemid: "222",
      }, () => {})
    `, context);
  }, (err) => {
    assert.equal(err.failureKind, 'api_blocked');
    assert.match(err.message, /api access denied/i);
    return true;
  });
});

test('extractShopeeReviews detects verification required on HTTP 200 with verify/traffic redirect', async () => {
  const { context } = await worker();
  const verifyUrl = "https://shopee.vn/verify/traffic?anti_bot_tracking_id=gqRjZGVrxHSFomtptTE0MjUx";
  vm.runInContext(`
    chrome.tabs = {
      query: async () => [{ id: 12, url: "https://shopee.vn/product/111/222" }],
      create: async () => ({ id: 12, url: "https://shopee.vn/product/111/222" }),
      get: async () => ({ id: 12, url: "https://shopee.vn/product/111/222" }),
      update: async (id, updateInfo) => ({ id, url: updateInfo.url || "https://shopee.vn/product/111/222", windowId: 1 }),
      onUpdated: { addListener() {}, removeListener() {} },
    };
    chrome.windows = {
      update: async () => ({}),
    };
    chrome.scripting = {
      executeScript: async () => [{
        result: {
          ok: true,
          status: 200,
          url: "${verifyUrl}",
          pageUrl: "${verifyUrl}",
          json: null,
          textSample: "<!DOCTYPE html><html><body>Shopee verify traffic</body></html>",
        }
      }],
    };
  `, context);

  await assert.rejects(async () => {
    await vm.runInContext(`
      extractShopeeReviews({
        id: "job-preflight-verify-200",
        url: "https://shopee.vn/product/111/222",
        itemid: "222",
      }, () => {})
    `, context);
  }, (err) => {
    assert.equal(err.failureKind, 'verification');
    assert.match(err.message, /verification required/i);
    assert.match(err.message, /verify\/traffic/i);
    return true;
  });

  const extracted = vm.runInContext(`
    extractVerificationUrl("Shopee verification required (HTTP 200) — tab=${verifyUrl}; response=${verifyUrl}")
  `, context);
  assert.equal(extracted, verifyUrl);
});

test('Job cu bi treo/loi khong khoa job moi trong queue executor', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.executedJobs = [];
    runJob = async (job) => {
      if (job.id === "job-hang") {
        throw new Error("Simulated job failure / timeout");
      }
      executedJobs.push(job.id);
    };
    enqueueLegacyJob({ id: "job-hang" });
    enqueueLegacyJob({ id: "job-next" });
  `, context);

  await vm.runInContext('jobChain', context);
  const executed = Array.from(vm.runInContext('globalThis.executedJobs', context));
  assert.deepEqual(executed, ['job-next']);
  assert.equal(vm.runInContext('activeJobs.size', context), 0);
  assert.equal(vm.runInContext('jobQueue.length', context), 0);
});

test('executeScript tra rong lan dau, lan sau thanh cong qua retry', async () => {
  const { context, timeouts } = await worker();
  vm.runInContext(`
    let attempts = 0;
    chrome.scripting = {
      executeScript: async () => {
        attempts++;
        if (attempts === 1) {
          return []; // empty result from Chrome when tab is navigating
        }
        return [{ result: { ok: true, data: "crawled_data", attempts } }];
      }
    };
  `, context);

  const pending = vm.runInContext(`
    executeScriptResultWithRetry({ target: { tabId: 99 } })
  `, context);

  await new Promise(resolve => setImmediate(resolve));
  const retryTimer = [...timeouts.values()].find(entry => entry.ms === 250);
  assert.ok(retryTimer);
  retryTimer.fn();

  const res = await pending;
  assert.equal(res.ok, true);
  assert.equal(res.data, "crawled_data");
  assert.equal(res.attempts, 2);
});

test('WAF hoac timeout khong lam fetchRatingsFromTab treo vo han', async () => {
  const { context, timeouts } = await worker();
  vm.runInContext(`
    chrome.scripting = {
      executeScript: async (options) => {
        const res = await options.func("123", "456", 0, 6, "https://shopee.vn");
        return [{ result: res }];
      }
    };
    globalThis.fetch = async (url, opts) => {
      return new Promise((resolve, reject) => {
        if (opts && opts.signal) {
          opts.signal.addEventListener('abort', () => {
            const err = new Error("The user aborted a request.");
            err.name = "AbortError";
            reject(err);
          });
        }
      });
    };
  `, context);

  const pending = vm.runInContext(`
    fetchRatingsFromTab(1, "123", "456", 0, 6, "https://shopee.vn")
  `, context);

  await new Promise(resolve => setImmediate(resolve));
  const abortTimer = [...timeouts.values()].find(entry => entry.ms === 15000);
  assert.ok(abortTimer, "AbortController timeout timer expected");
  abortTimer.fn();

  const res = await pending;
  assert.equal(res.ok, false);
  assert.equal(res.isTimeout, true);
});

test('service worker restart resume dung offset tu checkpoint', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.recordedOffsets = [];
    chrome.tabs = {
      query: async () => [{ id: 42, url: "https://shopee.vn/product/111/222" }],
      create: async () => ({ id: 42, url: "https://shopee.vn/product/111/222" }),
      get: async () => ({ id: 42, url: "https://shopee.vn/product/111/222" }),
      onUpdated: { addListener() {}, removeListener() {} },
    };
    chrome.scripting = {
      executeScript: async (opts) => {
        const off = opts.args ? opts.args[2] : null;
        if (typeof off === 'number') recordedOffsets.push(off);
        return [{
          result: {
            ok: true,
            status: 200,
            url: "https://shopee.vn/api/v2/item/get_ratings",
            json: { data: { ratings: [] } },
          }
        }];
      }
    };
  `, context);

  await vm.runInContext(`
    extractShopeeReviews({
      id: "job-chk-resume",
      url: "https://shopee.vn/product/111/222",
      checkpoint: { next_offset: 3000, rows_count: 3000, shopid: "111", total: 5000 }
    }, () => {})
  `, context);

  const offsets = Array.from(vm.runInContext('globalThis.recordedOffsets', context));
  assert.equal(offsets[0], 3000, "Should resume the interrupted segment at offset 3000");
  assert.ok(offsets.slice(1).includes(0), "Should continue with star buckets after the aggregate feed ends");
});

test('fetchRatingsFromTab forwards the selected star bucket to Shopee', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.capturedRatingType = null;
    chrome.scripting = {
      executeScript: async (opts) => {
        capturedRatingType = opts.args[5];
        return [{ result: { ok: true, status: 200, json: { data: { ratings: [] } } } }];
      }
    };
  `, context);

  const result = await vm.runInContext(`
    fetchRatingsFromTab(1, "123", "456", 0, 6, "https://shopee.vn/product/456/123", 5)
  `, context);

  assert.equal(result.ok, true);
  assert.equal(vm.runInContext('capturedRatingType', context), 5);
});

test('reviewFingerprint deduplicates the same review across rating segments', async () => {
  const { context } = await worker();
  const keys = vm.runInContext(`(() => {
    const review = {
      user: "buyer", thoi_gian: "2026-09-08 10:00:00", sao: 5,
      noi_dung: "tot", phan_loai: "den", anh_urls: "a.jpg", video_urls: ""
    };
    return [reviewFingerprint(review), reviewFingerprint({ ...review })];
  })()`, context);
  assert.equal(keys[0], keys[1]);
  assert.ok(keys[0].length > 0);
});

test('handleAction page.screenshot returns captured image data', async () => {
  const { context } = await worker();
  context.setTimeout = (fn) => { queueMicrotask(fn); return 1; };
  context.chrome.tabs = {
    get: async (id) => ({ id, windowId: 1, active: true, url: 'https://shopee.vn/test' }),
    query: async () => [{ id: 10, windowId: 1, active: true, url: 'https://shopee.vn/test' }],
    captureVisibleTab: async (winId, opts) => 'data:image/png;base64,evidence123',
    update: async () => {},
  };
  context.chrome.windows = { update: async () => {} };
  const res = await vm.runInContext('handleAction("page.screenshot", { format: "png", tabId: 10 })', context);
  assert.equal(res.status, 'ok');
  assert.equal(res.dataUrl, 'data:image/png;base64,evidence123');
  assert.equal(res.tabId, 10);
});

test('detectCaptchaInTab flags verification URLs and captures evidence', async () => {
  const { context } = await worker();
  context.setTimeout = (fn) => { queueMicrotask(fn); return 1; };
  context.chrome.tabs = {
    get: async (id) => ({ id, windowId: 1, url: 'https://shopee.vn/verify/traffic' }),
    captureVisibleTab: async () => 'data:image/png;base64,mocked_traffic_captcha',
    update: async () => {},
  };
  context.chrome.windows = { update: async () => {} };
  context.chrome.scripting = {
    executeScript: async () => [{ result: { detected: true, type: 'url', details: 'https://shopee.vn/verify/traffic' } }],
  };
  const detection = await vm.runInContext('detectCaptchaInTab(101)', context);
  assert.equal(detection.detected, true);
  assert.equal(detection.type, 'url');

  const evidence = await vm.runInContext('captureTabEvidence(101)', context);
  assert.equal(evidence, 'data:image/png;base64,mocked_traffic_captcha');
});

test('detectCaptchaInTab scrolls visible captcha elements into view', async () => {
  const { context } = await worker();
  vm.runInContext(`
    globalThis.__captchaScrollArg = null;
    chrome.scripting = {
      executeScript: async ({ func }) => {
        globalThis.location = { href: "https://shopee.vn/product/111/222" };
        globalThis.document = {
          querySelector: (sel) => sel === ".shopee-captcha-slider" ? {
            offsetWidth: 320,
            offsetHeight: 80,
            scrollIntoView: (arg) => { globalThis.__captchaScrollArg = arg; },
          } : null,
          body: { innerText: "" },
        };
        return [{ result: func() }];
      },
    };
  `, context);

  const detection = await vm.runInContext('detectCaptchaInTab(202)', context);
  const scrollArg = vm.runInContext('__captchaScrollArg', context);

  assert.equal(detection.detected, true);
  assert.equal(detection.type, 'dom_selector');
  assert.equal(detection.selector, '.shopee-captcha-slider');
  assert.equal(scrollArg.behavior, 'smooth');
  assert.equal(scrollArg.block, 'center');
  assert.equal(scrollArg.inline, 'center');
});

test('runJob handles api_blocked: sends websocket event, focuses tab, saves state and does not retry', async () => {
  const { context, frames, sockets, getMessageListener } = await worker();
  sockets[0].onopen();

  const updatedTabs = [];
  const updatedWindows = [];

  context.chrome.storage.local = {
    get: async (keys) => {
      if (Array.isArray(keys) && keys.includes('checkpoint_job-403-blocked')) {
        return {
          'checkpoint_job-403-blocked': {
            next_offset: 40,
            rating_type: 5,
            rows_count: 40,
          },
        };
      }
      return {};
    },
    set: async () => {},
  };
  context.chrome.tabs = {
    update: async (tabId, updateProps) => {
      updatedTabs.push({ tabId, updateProps });
      return { id: tabId, windowId: 99 };
    },
    create: async () => {},
  };
  context.chrome.windows = {
    update: async (windowId, updateProps) => {
      updatedWindows.push({ windowId, updateProps });
      return { id: windowId };
    },
  };
  vm.runInContext(`
    extractShopeeReviews = async () => {
      const err = new Error('Shopee reviews API access denied: HTTP 403 / API Blocked');
      err.failureKind = 'api_blocked';
      throw err;
    };
    sendIngestRequest = async () => ({ ok: true });
  `, context);

  const job = {
    id: 'job-403-blocked',
    url: 'https://shopee.vn/product/111/222',
    _targetTabId: 77,
  };

  await vm.runInContext(`runJob(${JSON.stringify(job)})`, context);

  // Check state updated to api_blocked
  const state = vm.runInContext('extensionState', context);
  assert.equal(state, 'api_blocked');
  const verifInfo = vm.runInContext('verificationInfo', context);
  assert.equal(verifInfo?.kind, 'api_blocked');

  // Check WebSocket frame sent
  const blockedFrame = frames.find((f) => f.type === 'api_blocked');
  assert.ok(blockedFrame, 'api_blocked WebSocket frame should be sent');
  assert.equal(blockedFrame.params.job_id, 'job-403-blocked');
  assert.equal(blockedFrame.params.status, 403);
  assert.equal(blockedFrame.params.checkpoint.next_offset, 40);

  // Check tab focused
  assert.equal(updatedTabs.length, 1);
  assert.equal(updatedTabs[0].tabId, 77);
  assert.equal(updatedTabs[0].updateProps.active, true);
  assert.equal(updatedWindows.length, 1);
  assert.equal(updatedWindows[0].windowId, 99);
  assert.equal(updatedWindows[0].updateProps.focused, true);
});

test('runJob on 403 detects captcha slider, triggers verification.required and allows resume', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();
  context.setTimeout = (fn) => { queueMicrotask(fn); return 1; };

  context.chrome.storage.local = {
    get: async (keys) => {
      if (Array.isArray(keys) && keys.includes('checkpoint_job-403-captcha')) {
        return {
          'checkpoint_job-403-captcha': {
            next_offset: 60,
            rating_type: 5,
            rows_count: 60,
          },
        };
      }
      return {};
    },
    set: async () => {},
  };
  context.chrome.tabs = {
    get: async (tabId) => ({ id: tabId, windowId: 10 }),
    update: async (tabId) => ({ id: tabId, windowId: 10 }),
    captureVisibleTab: async () => 'data:image/png;base64,mocked_403_captcha_screenshot',
    create: async () => {},
  };
  context.chrome.windows = {
    update: async () => ({ id: 10 }),
  };
  vm.runInContext(`
    extractShopeeReviews = async () => {
      const err = new Error('Shopee reviews API access denied: HTTP 403');
      err.failureKind = 'api_blocked';
      throw err;
    };
    detectCaptchaInTab = async () => ({ detected: true, type: 'slider', selector: '.shopee-captcha-slider' });
    sendIngestRequest = async () => ({ ok: true });
  `, context);

  const job = {
    id: 'job-403-captcha',
    url: 'https://shopee.vn/product/111/222',
    _targetTabId: 88,
  };

  await vm.runInContext(`runJob(${JSON.stringify(job)})`, context);

  // State should be awaiting_user_verification because captcha was detected on 403
  const state = vm.runInContext('extensionState', context);
  assert.equal(state, 'awaiting_user_verification');

  // WebSocket frame should be verification.required with screenshot and checkpoint
  const verifFrame = frames.find((f) => f.type === 'verification.required');
  assert.ok(verifFrame, 'verification.required frame should be sent');
  assert.equal(verifFrame.params.job_id, 'job-403-captcha');
  assert.equal(verifFrame.params.evidence_screenshot, 'data:image/png;base64,mocked_403_captcha_screenshot');
  assert.equal(verifFrame.params.checkpoint.next_offset, 60);

  // Test resumeVerification unpauses and enqueues the job
  let enqueued = false;
  context.__testEnqueue = (j) => {
    enqueued = true;
    assert.equal(j.id, 'job-403-captcha');
  };
  vm.runInContext(`
    enqueueLegacyJob = (j) => {
      __testEnqueue(j);
    };
  `, context);
  vm.runInContext('resumeVerification("job-403-captcha")', context);
  assert.equal(enqueued, true);
  const postResumeState = vm.runInContext('extensionState', context);
  assert.equal(postResumeState, 'connected');
  vm.runInContext('stopVerificationWatcher()', context);
});

test('probeFilterCandidates discovers comment and media filters dynamically without hardcoding', async () => {
  const { context } = await worker();
  const traces = [];
  context.traceCapture = (ev, d) => traces.push({ ev, d });

  vm.runInContext(`
    fetchRatingsFromTab = async (tabId, itemid, shopid, offset, limit, referer, ratingType, filter) => {
      if (filter === 1) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'c1', comment: 'Rất ưng ý', images: [], videos: [] },
                { cmid: 'c2', comment: 'Đẹp tuyệt vời', images: [], videos: [] },
              ]
            }
          }
        };
      }
      if (filter === 2) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'm1', comment: 'Có hình thật đây', images: ['https://shopee/img1.jpg'], videos: [] },
                { cmid: 'm2', comment: 'Hàng chuẩn', images: ['https://shopee/img2.jpg'], videos: [{ url: 'v1.mp4' }] },
              ]
            }
          }
        };
      }
      return { ok: true, status: 200, json: { data: { ratings: [] } } };
    };
  `, context);

  const job = { id: 'job-probe-test', url: 'https://shopee.vn/product/111/222' };
  const res = await vm.runInContext(`
    probeFilterCandidates(10, '222', '111', 'https://shopee.vn/product/111/222', ${JSON.stringify(job)}, async (p) => {
      traceCapture(p.stage, p.trace);
    })
  `, context);

  assert.equal(res.mediaFilter, 2, 'Filter 2 should be mapped to media (media_ratio = 1.0)');
  assert.equal(res.commentFilter, 1, 'Filter 1 should be mapped to comment (comment_ratio = 1.0)');
  assert.equal(res.mediaStat.media_ratio, 1);
  assert.equal(res.commentStat.comment_ratio, 1);

  // Check filter-probe traces
  const probeTraces = traces.filter((t) => t.d?.event === 'filter-probe');
  assert.ok(probeTraces.length >= 2, 'filter-probe trace events should be recorded');
  const mediaTrace = probeTraces.find((t) => t.d?.filter === 2);
  assert.equal(mediaTrace.d.mappedScope, 'media');
});

test('normalizeCheckpointV2 maintains V2 structures and migrates legacy V1 checkpoints', async () => {
  const { context } = await worker();

  // Test 1: V1 migration
  const v1 = {
    next_offset: 160,
    rating_type: 4,
    rows_count: 150,
    total: 300,
  };
  const migrated = vm.runInContext(`normalizeCheckpointV2(${JSON.stringify(v1)}, '222', '111')`, context);
  assert.equal(migrated.version, 2);
  assert.equal(migrated.active_scope, 'all');
  assert.equal(migrated.scopes.all.next_offset, 160);
  assert.equal(migrated.scopes.all.rating_type, 4);
  assert.equal(migrated.scopes.all.rows_count, 150);
  assert.equal(migrated.scopes.comment.discovered, false);
  assert.equal(migrated.scopes.media.discovered, false);

  // Test 2: V2 preservation
  const v2 = {
    version: 2,
    active_scope: 'comment',
    scopes: {
      all: { key: 'all', completed: true, next_offset: 200, rows_count: 200 },
      comment: { key: 'comment', completed: false, next_offset: 40, rows_count: 40 },
      media: { key: 'media', completed: false, next_offset: 0, rows_count: 0 },
    },
    ui_reference: { total: 500, rcount_with_context: 250, rcount_with_media: 80 },
  };
  const normalizedV2 = vm.runInContext(`normalizeCheckpointV2(${JSON.stringify(v2)}, '222', '111')`, context);
  assert.equal(normalizedV2.version, 2);
  assert.equal(normalizedV2.active_scope, 'comment');
  assert.equal(normalizedV2.scopes.comment.next_offset, 40);
  assert.equal(normalizedV2.ui_reference.rcount_with_context, 250);
});

test('mergeReview merges media URLs and longer text across scopes without data loss', async () => {
  const { context } = await worker();

  const merged = vm.runInContext(`(() => {
    const existing = {
      id: 'cmid-999',
      cmid: 'cmid-999',
      user: 'user_a',
      noi_dung: 'Tốt',
      sao: 5,
      anh: 0,
      so_anh: 0,
      anh_urls: '',
      video: 0,
      so_video: 0,
      video_urls: '',
      media_urls: '',
    };
    const incoming = {
      id: 'cmid-999',
      cmid: 'cmid-999',
      user: 'user_a',
      noi_dung: 'Tốt, đóng gói cẩn thận giao hàng nhanh',
      sao: 5,
      anh: 1,
      so_anh: 2,
      anh_urls: 'https://shopee/p1.jpg|https://shopee/p2.jpg',
      video: 1,
      so_video: 1,
      video_urls: 'https://shopee/v1.mp4',
      media_urls: 'https://shopee/p1.jpg|https://shopee/p2.jpg|https://shopee/v1.mp4',
      phan_loai: 'Màu đen, Size XL',
    };
    return mergeReview(existing, incoming);
  })()`, context);

  assert.equal(merged.id, 'cmid-999');
  assert.equal(merged.noi_dung, 'Tốt, đóng gói cẩn thận giao hàng nhanh');
  assert.equal(merged.anh, 1);
  assert.equal(merged.so_anh, 2);
  assert.equal(merged.anh_urls, 'https://shopee/p1.jpg|https://shopee/p2.jpg');
  assert.equal(merged.video, 1);
  assert.equal(merged.so_video, 1);
  assert.equal(merged.video_urls, 'https://shopee/v1.mp4');
  assert.equal(merged.phan_loai, 'Màu đen, Size XL');
});

test('extractShopeeReviews traverses multi-scopes independently with deduplication and checkpoints', async () => {
  const { context } = await worker();
  const checkpoints = [];
  context.__testCheckpoint = (chk) => checkpoints.push(chk);

  // Setup tab and DOM / API mocks
  context.chrome.tabs = {
    query: async () => [{ id: 55, url: 'https://shopee.vn/product/111/222', active: true }],
    create: async () => ({ id: 55, url: 'https://shopee.vn/product/111/222' }),
  };

  vm.runInContext(`
    sendCheckpoint = async (chk) => {
      __testCheckpoint(chk);
    };

    preflightRatingsInTab = async () => ({
      ok: true,
      status: 200,
      json: {
        data: {
          total: 3,
          item_rating_summary: {
            rating_total: 3,
            rcount_with_context: 2,
            rcount_with_media: 1,
          }
        }
      }
    });

    fetchRatingsFromTab = async (tabId, itemid, shopid, offset, limit, referer, ratingType, filter) => {
      // Filter 0 (all scope)
      if (filter === 0) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'rev-1', author_username: 'buyer1', rating_star: 5, comment: 'Được', images: [] },
                { cmid: 'rev-2', author_username: 'buyer2', rating_star: 4, comment: '', images: [] },
              ]
            }
          }
        };
      }
      // Probe filter 1 (comment candidate): 2 reviews with comments
      if (filter === 1 && offset === 0 && limit === 6) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'rev-1', author_username: 'buyer1', rating_star: 5, comment: 'Được rất ưng', images: [] },
                { cmid: 'rev-3', author_username: 'buyer3', rating_star: 5, comment: 'Quá đẹp', images: [] },
              ]
            }
          }
        };
      }
      // Probe filter 2 (media candidate): 1 review with photo
      if (filter === 2 && offset === 0 && limit === 6) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'rev-1', author_username: 'buyer1', rating_star: 5, comment: 'Được', images: ['https://shopee/rev1.jpg'] },
              ]
            }
          }
        };
      }
      // Filter 1 (comment scope pagination)
      if (filter === 1) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'rev-3', author_username: 'buyer3', rating_star: 5, comment: 'Quá đẹp', images: [] },
              ]
            }
          }
        };
      }
      // Filter 2 (media scope pagination)
      if (filter === 2) {
        return {
          ok: true,
          status: 200,
          json: {
            data: {
              ratings: [
                { cmid: 'rev-1', author_username: 'buyer1', rating_star: 5, comment: 'Được', images: ['https://shopee/rev1.jpg'] },
              ]
            }
          }
        };
      }
      return { ok: true, status: 200, json: { data: { ratings: [] } } };
    };
  `, context);

  const job = {
    id: 'job-multi-scope-test',
    url: 'https://shopee.vn/product/111/222',
  };
  context.testJob = job;

  const results = await vm.runInContext(`
    extractShopeeReviews(testJob, async () => {})
  `, context);

  // Exactly 3 unique reviews (rev-1, rev-2, rev-3) deduplicated by cmid
  assert.equal(results.length, 3);
  const rev1 = results.find((r) => r.id === 'rev-1');
  const rev2 = results.find((r) => r.id === 'rev-2');
  const rev3 = results.find((r) => r.id === 'rev-3');
  assert.ok(rev1, 'rev-1 must exist');
  assert.ok(rev2, 'rev-2 must exist');
  assert.ok(rev3, 'rev-3 must exist');

  // Media merged into rev-1
  assert.equal(rev1.anh, 1);
  assert.ok(rev1.anh_urls.includes('rev1.jpg'));

  // Crawl summary contains Checkpoint V2 scopes and UI reference
  assert.equal(job._crawlSummary.version, 2);
  assert.equal(job._crawlSummary.collected, 3);
  assert.equal(job._crawlSummary.ui_reference.rcount_with_context, 2);
  assert.equal(job._crawlSummary.ui_reference.rcount_with_media, 1);
  assert.equal(job._crawlSummary.scopes.all.completed, true);
  assert.equal(job._crawlSummary.scopes.comment.completed, true);
  assert.equal(job._crawlSummary.scopes.media.completed, true);

  // Checkpoints sent have version 2 and scopes
  assert.ok(checkpoints.length > 0);
  const lastChk = checkpoints[checkpoints.length - 1];
  assert.equal(lastChk.version, 2);
  assert.ok(lastChk.scopes);
});

test('extractShopeeReviews advances rating buckets for comment scope when aggregate feed caps early', async () => {
  const { context } = await worker();
  const requests = [];
  context.__recordRequest = (entry) => requests.push(entry);
  context.setTimeout = (fn) => { queueMicrotask(fn); return 1; };

  context.chrome.tabs = {
    query: async () => [{ id: 56, url: 'https://shopee.vn/product/111/333', active: true }],
    create: async () => ({ id: 56, url: 'https://shopee.vn/product/111/333' }),
  };

  const firstPage = Array.from({ length: 6 }, (_, idx) => ({
    cmid: `comment-all-${idx}`,
    author_username: `buyer-all-${idx}`,
    rating_star: 5,
    comment: `aggregate comment ${idx}`,
    images: [],
  }));
  const fiveStarPage = Array.from({ length: 6 }, (_, idx) => ({
    cmid: `comment-five-${idx}`,
    author_username: `buyer-five-${idx}`,
    rating_star: 5,
    comment: `five star comment ${idx}`,
    images: [],
  }));
  context.__firstPage = firstPage;
  context.__fiveStarPage = fiveStarPage;

  vm.runInContext(`
    sendCheckpoint = async () => {};
    preflightRatingsInTab = async () => ({
      ok: true,
      status: 200,
      json: {
        data: {
          total: 12,
          item_rating_summary: {
            rating_total: 12,
            rcount_with_context: 12,
            rcount_with_media: 0,
          },
        },
      },
    });
    fetchRatingsFromTab = async (tabId, itemid, shopid, offset, limit, referer, ratingType, filter) => {
      __recordRequest({ offset, limit, ratingType, filter });
      if (filter === 1 && ratingType === 0 && offset === 0) {
        return { ok: true, status: 200, json: { data: { ratings: __firstPage } } };
      }
      if (filter === 1 && ratingType === 5 && offset === 0) {
        return { ok: true, status: 200, json: { data: { ratings: __fiveStarPage } } };
      }
      return { ok: true, status: 200, json: { data: { ratings: [] } } };
    };
  `, context);

  const job = {
    id: 'job-comment-cap-wall',
    url: 'https://shopee.vn/product/111/333',
    checkpoint: {
      version: 2,
      active_scope: 'comment',
      scopes: {
        all: { key: 'all', label: 'Tất cả', filter: 0, completed: true, discovered: true, rows_count: 0 },
        comment: { key: 'comment', label: 'Có bình luận', filter: 1, rating_type: 0, next_offset: 0, completed: false, discovered: true, rows_count: 0 },
        media: { key: 'media', label: 'Có hình ảnh / Video', filter: null, completed: true, discovered: false, rows_count: 0 },
      },
      itemid: '333',
      shopid: '111',
      total: 12,
      ui_reference: { total: 12, rating_total: 12, rcount_with_context: 12, rcount_with_media: 0 },
    },
  };
  context.testJob = job;

  const results = await vm.runInContext('extractShopeeReviews(testJob, async () => {})', context);

  assert.equal(results.length, 12);
  assert.ok(requests.some((r) => r.filter === 1 && r.ratingType === 0 && r.offset === 6), 'should hit the capped aggregate offset');
  assert.ok(requests.some((r) => r.filter === 1 && r.ratingType === 5 && r.offset === 0), 'should continue through star buckets for comment scope');
  assert.equal(job._crawlSummary.scopes.comment.completed, true);
  assert.equal(job._crawlSummary.scopes.comment.rows_count, 12);
});

test('handleRecheckApi runs preflight and resumes job from checkpoint on HTTP 200', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let resumedEnqueuedJob = null;
  context.chrome.tabs = {
    get: async (id) => ({ id, windowId: 1, url: 'https://shopee.vn/product/111/222' }),
    update: async () => ({ id: 1 }),
    create: async () => {},
  };
  context.chrome.storage.local = {
    get: async () => ({
      'checkpoint_job-recheck-200': {
        version: 2,
        active_scope: 'comment',
        next_offset: 20,
        itemid: '222',
        shopid: '111',
      },
    }),
    set: async () => {},
  };

  vm.runInContext(`
    findOrOpenShopeeTab = async () => ({ id: 55, url: "https://shopee.vn/product/111/222" });
    preflightRatingsInTab = async () => ({
      ok: true,
      status: 200,
      json: { data: { ratings: [{ id: "r1", comment: "good" }] } },
    });
    enqueueLegacyJob = (j) => { resumedEnqueuedJob = j; };
  `, context);

  const testJob = {
    id: 'job-recheck-200',
    url: 'https://shopee.vn/product/111/222',
    _targetTabId: 55,
  };

  vm.runInContext(`
    pendingVerificationJobs.set("job-recheck-200", ${JSON.stringify(testJob)});
    updateState("api_blocked", { job_id: "job-recheck-200", kind: "api_blocked" });
  `, context);

  const result = await vm.runInContext('handleRecheckApi("job-recheck-200")', context);
  assert.equal(result.ok, true);

  // Verification resolved frame sent with recheck: true
  const resolvedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === 'job-recheck-200');
  assert.ok(resolvedFrame, 'verification.resolved frame should be sent');
  assert.equal(resolvedFrame.params.recheck, true);
  assert.equal(resolvedFrame.params.checkpoint.active_scope, 'comment');

  // Job was enqueued to resume
  const enqueued = vm.runInContext('resumedEnqueuedJob', context);
  assert.ok(enqueued, 'Job must be re-enqueued on successful recheck');
  assert.equal(enqueued.id, 'job-recheck-200');
  assert.equal(enqueued.retry, true);
});

test('handleRecheckApi rejects and does not resume when preflight returns 403', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  vm.runInContext(`
    findOrOpenShopeeTab = async () => ({ id: 66, url: "https://shopee.vn/product/111/222" });
    preflightRatingsInTab = async () => ({
      ok: false,
      status: 403,
      error: "Shopee reviews API access denied (HTTP 403)",
      textSample: "403 Forbidden",
    });
    enqueueLegacyJob = (j) => { throw new Error("Should not enqueue!"); };
  `, context);

  const testJob = {
    id: 'job-recheck-403',
    url: 'https://shopee.vn/product/111/222',
    _targetTabId: 66,
  };

  vm.runInContext(`
    pendingVerificationJobs.set("job-recheck-403", ${JSON.stringify(testJob)});
    updateState("api_blocked", { job_id: "job-recheck-403", kind: "api_blocked" });
  `, context);

  const result = await vm.runInContext('handleRecheckApi("job-recheck-403")', context);
  assert.equal(result.ok, false);
  assert.equal(result.status, 403);
  assert.ok(result.error.includes("403"));

  // No resolved frame sent
  const resolvedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === 'job-recheck-403');
  assert.equal(resolvedFrame, undefined);
});

test('handleRecheckApi rejects HTTP 200 when ratings payload is unusable', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  vm.runInContext(`
    findOrOpenShopeeTab = async () => ({ id: 77, url: "https://shopee.vn/product/111/222" });
    preflightRatingsInTab = async () => ({
      ok: true,
      status: 200,
      json: { data: null },
      textSample: '{"data":null}',
    });
    enqueueLegacyJob = (j) => { throw new Error("Should not enqueue!"); };
  `, context);

  const testJob = {
    id: 'job-recheck-200-null',
    url: 'https://shopee.vn/product/111/222',
    _targetTabId: 77,
  };

  vm.runInContext(`
    pendingVerificationJobs.set("job-recheck-200-null", ${JSON.stringify(testJob)});
    updateState("api_blocked", { job_id: "job-recheck-200-null", kind: "api_blocked" });
  `, context);

  const result = await vm.runInContext('handleRecheckApi("job-recheck-200-null")', context);
  assert.equal(result.ok, false);
  assert.equal(result.status, 200);

  const resolvedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === 'job-recheck-200-null');
  assert.equal(resolvedFrame, undefined);
});

test('gateway verification.resolved with recheck resumes api_blocked job', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let enqueuedJob = null;
  await vm.runInContext(`
    enqueueLegacyJob = (j) => { enqueuedJob = j; };
    pendingVerificationJobs.set("job-gateway-recheck", {
      id: "job-gateway-recheck",
      url: "https://shopee.vn/product/111/222",
    });
    updateState("api_blocked", { job_id: "job-gateway-recheck", kind: "api_blocked" });
    dispatchEnvelope({
      type: "verification.resolved",
      params: {
        jobId: "job-gateway-recheck",
        recheck: true,
        checkpoint: { version: 2, next_offset: 12 },
      },
    });
  `, context);

  const enqueued = vm.runInContext('enqueuedJob', context);
  assert.ok(enqueued, 'api_blocked job should resume when gateway sends recheck=true');
  assert.equal(enqueued.id, 'job-gateway-recheck');
  assert.equal(enqueued.retry, true);
  assert.equal(enqueued.checkpoint.next_offset, 12);

  const refusedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === 'job-gateway-recheck' && f.params?.recheck === false);
  assert.equal(refusedFrame, undefined);
});

test('verification watcher requires 2 consecutive absent checks before preflight and resolution', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let tabUpdateListener = null;

  context.chrome.tabs = {
    onUpdated: {
      addListener: (fn) => { tabUpdateListener = fn; },
      removeListener: () => { tabUpdateListener = null; },
    },
    get: async () => ({ id: 77 }),
    update: async () => ({ id: 77 }),
  };

  context.detectCallCount = 0;
  context.preflightCalled = false;
  context.enqueued = false;

  vm.runInContext(`
    detectCaptchaInTab = async () => {
      detectCallCount++;
      return { detected: false };
    };
    preflightRatingsInTab = async () => {
      preflightCalled = true;
      return {
        ok: true,
        status: 200,
        json: { data: { ratings: [{ id: "r1" }] } },
      };
    };
    enqueueLegacyJob = () => { enqueued = true; };
  `, context);

  const details = {
    job_id: 'job-watcher-consecutive',
    tab_id: 77,
    kind: 'verification',
    checkpoint: { version: 2, next_offset: 10 },
  };

  vm.runInContext(`
    pendingVerificationJobs.set("job-watcher-consecutive", { id: "job-watcher-consecutive", url: "https://shopee.vn/product/111/222" });
    verificationInfo = { job_id: "job-watcher-consecutive", kind: "verification" };
    startVerificationWatcher(${JSON.stringify(details)});
  `, context);

  // Trigger tabUpdateListener 1st time
  const listener = tabUpdateListener || vm.runInContext('activeTabUpdateListener', context);
  assert.ok(listener, 'tab update listener must be registered');

  await listener(77, { status: 'complete' }, { url: 'https://shopee.vn/product/111/222' });
  // 1st check: consecutiveAbsentCount = 1, preflight should NOT be called yet
  assert.equal(vm.runInContext('preflightCalled', context), false);
  assert.equal(vm.runInContext('enqueued', context), false);

  // Trigger tabUpdateListener 2nd time
  await listener(77, { status: 'complete' }, { url: 'https://shopee.vn/product/111/222' });
  // 2nd check: consecutiveAbsentCount = 2, preflight should be called and resume job
  assert.equal(vm.runInContext('preflightCalled', context), true);
  assert.equal(vm.runInContext('enqueued', context), true);

  vm.runInContext('stopVerificationWatcher()', context);
});

test('verification watcher transitions to api_blocked when challenge absent but preflight is 403', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let tabUpdateListener = null;
  context.chrome.tabs = {
    onUpdated: {
      addListener: (fn) => { tabUpdateListener = fn; },
      removeListener: () => { tabUpdateListener = null; },
    },
    get: async () => ({ id: 88 }),
    update: async () => ({ id: 88 }),
  };

  vm.runInContext(`
    detectCaptchaInTab = async () => ({ detected: false });
    preflightRatingsInTab = async () => ({
      ok: false,
      status: 403,
      error: "Shopee reviews API access denied (HTTP 403)",
      textSample: "403 Forbidden",
    });
    enqueueLegacyJob = () => { throw new Error("Should not enqueue!"); };
  `, context);

  const details = {
    job_id: 'job-watcher-to-blocked',
    tab_id: 88,
    kind: 'verification',
    checkpoint: { version: 2, next_offset: 15 },
  };

  vm.runInContext(`
    verificationInfo = { job_id: "job-watcher-to-blocked", kind: "verification" };
    startVerificationWatcher(${JSON.stringify(details)});
  `, context);

  const listener = tabUpdateListener || vm.runInContext('activeTabUpdateListener', context);
  assert.ok(listener, 'tab update listener must be registered');

  // Trigger 2 consecutive checks
  await listener(88, { status: 'complete' }, { url: 'https://shopee.vn/product/111/222' });
  await listener(88, { status: 'complete' }, { url: 'https://shopee.vn/product/111/222' });

  // Extension state should transition to api_blocked
  const state = vm.runInContext('extensionState', context);
  assert.equal(state, 'api_blocked');

  // api_blocked frame sent
  const blockedFrame = frames.find((f) => f.type === 'api_blocked' && f.params?.job_id === 'job-watcher-to-blocked');
  assert.ok(blockedFrame, 'api_blocked frame must be sent');
  assert.equal(blockedFrame.params.status, 403);

  // Watcher must be stopped (no interval or listener)
  const watcherInterval = vm.runInContext('verificationWatcherInterval', context);
  assert.equal(watcherInterval, null);

  vm.runInContext('stopVerificationWatcher()', context);
});

test('resumeVerification recovers job from chrome.storage.local when in-memory pendingVerificationJobs is empty', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let enqueuedJob = null;
  vm.runInContext(`
    enqueueLegacyJob = (j) => { enqueuedJob = j; };
  `, context);

  // Simulate MV3 Service Worker wake-up: pendingVerificationJobs is empty!
  const targetJobId = "job-sw-restart-auto";
  const storedJob = { id: targetJobId, url: "https://shopee.vn/product/123/456", checkpoint: { version: 2, next_offset: 20 } };

  await context.chrome.storage.local.set({
    [`pending_job_${targetJobId}`]: storedJob,
  });

  await vm.runInContext(`
    resumeVerification("${targetJobId}", { verification_cycle: 2, checkpoint: { next_offset: 20 } });
  `, context);

  const enqueued = vm.runInContext('enqueuedJob', context);
  assert.ok(enqueued, 'Job must be recovered from storage and enqueued');
  assert.equal(enqueued.id, targetJobId);
  assert.equal(enqueued.retry, true);

  // Frame verification.resolved sent
  const resolvedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === targetJobId);
  assert.ok(resolvedFrame, 'verification.resolved frame must be sent');
  assert.equal(resolvedFrame.params.verification_cycle, 2);
});

test('startApiBlockedWatcher auto-resumes job on tab reload when preflight ratings returns 200', async () => {
  const { context, frames, sockets } = await worker();
  sockets[0].onopen();

  let tabListener = null;
  context.chrome.tabs = {
    onUpdated: {
      addListener: (fn) => { tabListener = fn; },
      removeListener: () => { tabListener = null; },
    },
    get: async () => ({ id: 99 }),
    update: async () => ({ id: 99 }),
  };

  let preflightCount = 0;
  let enqueuedJob = null;
  vm.runInContext(`
    var preflightCount = 0;
    var enqueuedJob = null;
    preflightRatingsInTab = async () => {
      preflightCount++;
      return {
        ok: true,
        status: 200,
        json: { data: { ratings: [{ itemid: 456, cmid: 111 }] } },
      };
    };
    enqueueLegacyJob = (j) => { enqueuedJob = j; };
    pendingVerificationJobs.set("job-api-blocked-reload", {
      id: "job-api-blocked-reload",
      url: "https://shopee.vn/product/123/456",
    });
    verificationInfo = { job_id: "job-api-blocked-reload", kind: "api_blocked" };
  `, context);

  const details = {
    job_id: "job-api-blocked-reload",
    tab_id: 99,
    url: "https://shopee.vn/product/123/456",
    checkpoint: { version: 2, next_offset: 10 },
  };

  vm.runInContext(`
    startApiBlockedWatcher(${JSON.stringify(details)});
  `, context);

  const listener = tabListener || vm.runInContext('activeApiBlockedTabListener', context);
  assert.ok(listener, 'activeApiBlockedTabListener must be set');

  // Simulate user reloading the Shopee tab
  await listener(99, { status: 'complete' }, { url: "https://shopee.vn/product/123/456" });

  assert.equal(vm.runInContext('preflightCount', context), 1);
  const enqueued = vm.runInContext('enqueuedJob', context);
  assert.ok(enqueued, 'Job must be auto-resumed and enqueued');
  assert.equal(enqueued.id, "job-api-blocked-reload");

  // verification.resolved frame sent with recheck: true
  const resolvedFrame = frames.find((f) => f.type === 'verification.resolved' && f.params?.job_id === "job-api-blocked-reload");
  assert.ok(resolvedFrame, 'verification.resolved frame must be sent on auto-resume');
  assert.equal(resolvedFrame.params.recheck, true);

  vm.runInContext('stopApiBlockedWatcher()', context);
});

test('job.cancel cleans api_blocked watcher and pending job state', async () => {
  const { context, stored, timeouts } = await worker();

  let tabListener = null;
  context.chrome.tabs = {
    onUpdated: {
      addListener: (fn) => { tabListener = fn; },
      removeListener: () => { tabListener = null; },
    },
  };

  await context.chrome.storage.local.set({
    pending_job_cancelled: { id: "cancelled", url: "https://shopee.vn/product/123/456" },
  });

  const baselineTimeouts = timeouts.size;
  vm.runInContext(`
    pendingVerificationJobs.set("cancelled", { id: "cancelled", url: "https://shopee.vn/product/123/456" });
    verificationInfo = { job_id: "cancelled", kind: "api_blocked" };
    startApiBlockedWatcher({
      job_id: "cancelled",
      tab_id: 99,
      url: "https://shopee.vn/product/123/456",
    });
  `, context);

  assert.ok(tabListener, 'api_blocked watcher should register a tab listener');
  assert.equal(timeouts.size, baselineTimeouts + 1);

  const result = await vm.runInContext('handleAction("job.cancel", { jobId: "cancelled" })', context);
  assert.equal(result.cancelled, false);
  assert.equal(tabListener, null);
  assert.equal(timeouts.size, baselineTimeouts);
  assert.equal(vm.runInContext('pendingVerificationJobs.has("cancelled")', context), false);
  assert.equal(stored.pending_job_cancelled, undefined);
});

test('restorePendingVerificationWatchers restarts api_blocked watcher from storage', async () => {
  const targetJobId = "job-restore-api-blocked";
  const { context, stored, timeouts } = await worker({ stored: {
    verificationInfo: {
      job_id: targetJobId,
      kind: "api_blocked",
      tab_id: 101,
      url: "https://shopee.vn/product/123/456",
      checkpoint: { version: 2, next_offset: 30, itemid: "456", shopid: "123" },
    },
    [`pending_job_${targetJobId}`]: {
      id: targetJobId,
      url: "https://shopee.vn/product/123/456",
      checkpoint: { version: 2, next_offset: 30 },
    },
  } });

  let tabListener = null;
  context.chrome.tabs = {
    onUpdated: {
      addListener: (fn) => { tabListener = fn; },
      removeListener: () => { tabListener = null; },
    },
  };

  const baselineTimeouts = timeouts.size;
  await vm.runInContext('restorePendingVerificationWatchers()', context);

  assert.ok(tabListener, 'restored api_blocked watcher should register a tab listener');
  assert.equal(timeouts.size, baselineTimeouts + 1);
  assert.equal(vm.runInContext(`pendingVerificationJobs.has("${targetJobId}")`, context), true);
  assert.equal(stored.verificationInfo.auto_recheck_active, true);
  assert.equal(stored.verificationInfo.auto_recheck_exhausted, false);

  vm.runInContext('stopApiBlockedWatcher()', context);
});

test('classifyShopeeFailure classifies /verify/captcha and captcha samples as verification', async () => {
  const { context } = await worker();

  const res403Captcha = {
    status: 403,
    url: "https://shopee.vn/verify/captcha?anti_bot_tracking_id=xyz",
    textSample: "xác nhận để tiếp tục, kéo qua để hoàn thiện bức hình",
  };
  assert.equal(vm.runInContext(`classifyShopeeFailure(${JSON.stringify(res403Captcha)})`, context), "verification");

  const res200Captcha = {
    status: 200,
    url: "https://shopee.vn/verify/captcha?anti_bot_tracking_id=abc",
    textSample: "kéo thanh trượt để hoàn thành",
  };
  assert.equal(vm.runInContext(`classifyShopeeFailure(${JSON.stringify(res200Captcha)})`, context), "verification");

  const res403General = {
    status: 403,
    url: "https://shopee.vn/api/v2/item/get_ratings",
    textSample: "error 90309999",
  };
  assert.equal(vm.runInContext(`classifyShopeeFailure(${JSON.stringify(res403General)})`, context), "api_blocked");
});

test('detectCaptchaInTab identifies active challenge from Image 1 phrases and classes', async () => {
  const { context } = await worker();

  vm.runInContext(`
    chrome.scripting = {
      executeScript: async ({ func }) => {
        globalThis.location = { href: "https://shopee.vn/verify/captcha?anti_bot_tracking_id=123" };
        globalThis.document = {
          body: { innerText: "Xác nhận để tiếp tục. Kéo qua để hoàn thiện bức hình." },
          querySelector: (sel) => null,
          querySelectorAll: () => [],
        };
        return [{ result: func() }];
      },
    };
  `, context);

  const detection = await vm.runInContext('detectCaptchaInTab(301)', context);
  assert.equal(detection.detected, true);
  assert.equal(detection.resolved, false);
  assert.ok(detection.type === 'dom_text' || detection.type === 'slider_pending');
});

test('detectCaptchaInTab identifies resolved state from Image 2 (green checkmark / passed slider)', async () => {
  const { context } = await worker();

  vm.runInContext(`
    chrome.scripting = {
      executeScript: async ({ func }) => {
        globalThis.location = { href: "https://shopee.vn/verify/captcha?anti_bot_tracking_id=123" };
        globalThis.document = {
          body: { innerText: "Xác nhận để tiếp tục" },
          querySelector: (sel) => sel === ".shopee-captcha-slider__btn--success" ? { offsetWidth: 50, offsetHeight: 50 } : null,
          querySelectorAll: (sel) => [
            {
              innerText: "✔",
              children: [],
            }
          ],
        };
        globalThis.window = {
          getComputedStyle: (el) => ({ backgroundColor: "rgb(38, 170, 153)", color: "white" }),
        };
        return [{ result: func() }];
      },
    };
  `, context);

  const detection = await vm.runInContext('detectCaptchaInTab(302)', context);
  assert.equal(detection.detected, false);
  assert.equal(detection.resolved, true);
  assert.equal(detection.type, 'slider_passed');
});

test('startVerificationWatcher immediately triggers preflight when slider_passed is detected', async () => {
  const jobId = "job-slider-passed-immediate";
  const { context, stored } = await worker();

  let tabUpdateListener = null;
  let updateTabUrl = null;

  context.chrome.tabs = {
    get: async (id) => ({ id, url: "https://shopee.vn/verify/captcha?anti_bot_tracking_id=test" }),
    update: async (id, opts) => { updateTabUrl = opts.url; },
    onUpdated: {
      addListener: (fn) => { tabUpdateListener = fn; },
      removeListener: () => { tabUpdateListener = null; },
    },
    onRemoved: { addListener: () => {}, removeListener: () => {} },
  };

  let preflightCalled = false;
  let enqueued = false;

  vm.runInContext(`
    var preflightCalled = false;
    var enqueued = false;

    pendingVerificationJobs.set("${jobId}", {
      id: "${jobId}",
      itemid: "123",
      shopid: "456",
      url: "https://shopee.vn/product/456/123",
      checkpoint: { next_offset: 10 }
    });

    preflightRatingsInTab = async () => {
      preflightCalled = true;
      return { ok: true, status: 200, json: { data: { ratings: [{ cmid: 999 }] } } };
    };

    detectCaptchaInTab = async () => {
      return { detected: false, resolved: true, type: "slider_passed" };
    };

    enqueueLegacyJob = (j) => {
      enqueued = true;
    };
  `, context);

  await vm.runInContext(`
    verificationInfo = { job_id: "${jobId}", kind: "verification" };
    startVerificationWatcher({
      job_id: "${jobId}",
      tab_id: 88,
      itemid: "123",
      shopid: "456",
      referer: "https://shopee.vn/product/456/123",
      checkpoint: { next_offset: 10 }
    })
  `, context);

  const listener = tabUpdateListener || vm.runInContext('activeTabUpdateListener', context);
  assert.ok(listener, 'tab update listener must be registered');

  // Trigger once: even with 1st check, resolved: true should immediately trigger preflight
  await listener(88, { status: 'complete' }, { url: 'https://shopee.vn/verify/captcha?anti_bot_tracking_id=test' });

  assert.equal(vm.runInContext('preflightCalled', context), true, "preflight should be called immediately on slider_passed");
  assert.equal(vm.runInContext('enqueued', context), true, "job should be resumed immediately");
  assert.equal(updateTabUrl, "https://shopee.vn/product/456/123", "tab should be redirected back to product url");

  vm.runInContext('stopVerificationWatcher()', context);
});

