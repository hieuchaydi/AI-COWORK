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
      if (frame.type === 'ingest.rpc') queueMicrotask(() => this.onmessage({ data: JSON.stringify({
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

test('progress and large results use acknowledged WebSocket chunks exclusively', async () => {
  const { context, frames, fetches, timers } = await worker();
  await vm.runInContext('reportProgress({id: "job-1"}, {percent: 20})', context);
  await vm.runInContext('uploadIngestResult({job: "job-1", rows: [{text: "x".repeat(180000)}]})', context);
  const operations = frames.map(frame => frame.params.operation);
  assert.deepEqual(operations, ['progress', 'chunk', 'chunk', 'chunk', 'complete']);
  const body = JSON.parse(frames.filter(frame => frame.params.operation === 'chunk').map(frame => frame.params.chunk).join(''));
  assert.equal(body.rows[0].text.length, 180000);
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


