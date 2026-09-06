const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');

async function worker() {
  const frames = [], fetches = [], timers = new Map();
  let nextTimer = 0;
  const listener = { addListener() {} };
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor() { this.readyState = 1; }
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
    setTimeout, clearTimeout,
    setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearInterval: id => timers.delete(id),
    fetch: async url => {
      fetches.push(url);
      assert.match(url, /\/browser\/pair$/);
      return { ok: true, json: async () => ({ token: 'new-token', wsUrl: 'ws://127.0.0.1:8766/browser/v1/ws' }) };
    },
    chrome: {
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
      storage: { local: { get: async () => ({}), set: async () => {} } },
      runtime: { onInstalled: listener, onStartup: listener, onMessage: listener },
      alarms: { onAlarm: listener, create() {} },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser-extension/background.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  return { context, frames, fetches, timers };
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
