const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(id = '') {
  const classes = new Set();
  return {
    id,
    style: {},
    className: '',
    textContent: '',
    innerHTML: '',
    value: '',
    children: [],
    classList: {
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
    addEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    removeAttribute(name) { delete this[name]; },
  };
}

async function popup(result) {
  const ids = [
    'statusBadge', 'gatewayUrl', 'pairingToken', 'btnAutoPair', 'btnConnect',
    'btnDisconnect', 'jobStatusText', 'verificationBox', 'verificationReason',
    'verificationMsg', 'verificationTargetUrl', 'btnOpenVerificationTab',
    'btnResumeVerification', 'tabList', 'connectionError', 'verificationTitle',
    'resultSection', 'resultSummary', 'downloadCsv', 'downloadZip',
    'openManifest', 'openReport', 'zipParts',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, element(id)]));
  const stored = {
    gatewayUrl: 'ws://127.0.0.1:8766/browser/v1/ws',
    pairingToken: 'token',
    extensionState: 'connected',
    lastCompletedResult: result,
  };
  const context = vm.createContext({
    console,
    URL,
    setInterval() {},
    fetch: async () => ({ ok: true, json: async () => ({ connected: true }) }),
    document: {
      getElementById: id => elements[id] || (elements[id] = element(id)),
      createElement: () => element(),
    },
    chrome: {
      storage: { local: { get: (keys, callback) => callback({ ...stored }) } },
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          callback({
            ok: true,
            connected: true,
            extensionState: 'connected',
            lastCompletedResult: result,
          });
        },
      },
      tabs: { query: async () => [] },
      windows: {},
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser-extension/popup.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  return { context, elements };
}

test('popup automatically exposes CSV, ZIP, manifest and report after a completed crawl', async () => {
  const { elements } = await popup({
    count: 6789,
    completedAt: 1788739200000,
    csv: 'outputs/csv/shopee_1546910319_reviews.csv',
    manifest: 'outputs/media/shopee_1546910319_reviews/manifest.json',
    zip_urls: [
      '/outputs/zips/shopee_1546910319_reviews_media_part01.zip',
      '/outputs/zips/shopee_1546910319_reviews_media_part02.zip',
    ],
    report_url: '/outputs/text/shopee_1546910319_reviews_report.md',
  });

  assert.equal(elements.resultSection.style.display, 'block');
  assert.match(elements.resultSummary.textContent, /6[.,]789 đánh giá/);
  assert.equal(elements.downloadCsv.href, 'http://127.0.0.1:8766/outputs/csv/shopee_1546910319_reviews.csv');
  assert.equal(elements.downloadZip.href, 'http://127.0.0.1:8766/outputs/zips/shopee_1546910319_reviews_media_part01.zip');
  assert.equal(elements.openManifest.href, 'http://127.0.0.1:8766/outputs/media/shopee_1546910319_reviews/manifest.json');
  assert.equal(elements.openReport.href, 'http://127.0.0.1:8766/outputs/text/shopee_1546910319_reviews_report.md');
  assert.equal(elements.zipParts.children.length, 2);
});

test('popup never turns a non-local result URL into a download link', async () => {
  const { context } = await popup(null);
  assert.equal(vm.runInContext('outputUrl("https://example.com/result.zip", "ws://127.0.0.1:8766/browser/v1/ws")', context), '');
});
