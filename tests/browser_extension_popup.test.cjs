const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(id = '') {
  const classes = new Set();
  const listeners = {};
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
    addEventListener(event, handler) {
      listeners[event] = listeners[event] || [];
      listeners[event].push(handler);
    },
    async click() {
      if (listeners['click']) {
        for (const h of listeners['click']) {
          await h();
        }
      }
    },
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
    'openManifest', 'openReport', 'zipParts', 'btnRecheckApi', 'recheckStatusMsg',
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

test('popup displays btnRecheckApi for api_blocked and btnResumeVerification for verification', async () => {
  const { context, elements } = await popup(null);

  // 1. When verification is api_blocked:
  vm.runInContext(`
    renderStatus("api_blocked", {
      verification: {
        kind: "api_blocked",
        job_id: "job-blocked-1",
        reason: "HTTP 403 Forbidden",
        url: "https://shopee.vn/product/1/2",
      }
    });
  `, context);

  assert.equal(elements.btnRecheckApi.style.display, 'block');
  assert.equal(elements.btnResumeVerification.style.display, 'none');
  assert.match(elements.verificationTitle.textContent, /Shopee chặn API/);

  // 2. When verification is verification (CAPTCHA):
  vm.runInContext(`
    renderStatus("awaiting_user_verification", {
      verification: {
        kind: "verification",
        job_id: "job-captcha-1",
        reason: "Slider CAPTCHA",
        url: "https://shopee.vn/product/1/2",
      }
    });
  `, context);

  assert.equal(elements.btnResumeVerification.style.display, 'block');
  assert.equal(elements.btnRecheckApi.style.display, 'none');
  assert.match(elements.verificationTitle.textContent, /Yêu cầu giải CAPTCHA/);
});

test('popup displays correct verify/traffic URL and btnOpenVerificationTab navigates to it', async () => {
  const { context, elements } = await popup(null);
  const verifyUrl = "https://shopee.vn/verify/traffic?anti_bot_tracking_id=challenge123";

  vm.runInContext(`
    let updatedTab = null;
    let updatedWindow = null;
    chrome.tabs.update = async (tabId, opts) => {
      updatedTab = { tabId, ...opts };
      return { id: tabId, windowId: 42 };
    };
    chrome.windows.update = async (winId, opts) => {
      updatedWindow = { winId, ...opts };
      return {};
    };

    renderStatus("awaiting_user_verification", {
      verification: {
        kind: "verification",
        job_id: "job-traffic-1",
        reason: "Shopee verification required (HTTP 200) — tab=${verifyUrl}; response=unknown",
        tab_id: 10,
        url: "https://shopee.vn/product/1/2",
      }
    });
  `, context);

  assert.equal(elements.verificationTargetUrl.textContent, `URL cần mở: ${verifyUrl}`);
  assert.equal(elements.btnOpenVerificationTab.style.display, 'block');

  // Trigger click on btnOpenVerificationTab
  await vm.runInContext(`
    document.getElementById("btnOpenVerificationTab").click();
  `, context);

  const updatedTab = vm.runInContext('updatedTab', context);
  const updatedWindow = vm.runInContext('updatedWindow', context);
  assert.equal(updatedTab.tabId, 10);
  assert.equal(updatedTab.url, verifyUrl);
  assert.equal(updatedTab.active, true);
  assert.equal(updatedWindow.winId, 42);
  assert.equal(updatedWindow.focused, true);
});

test('popup displays verification evidence with dynamic label and job evidence preview', async () => {
  const { context, elements } = await popup(null);

  // 1. Verification with evidence
  vm.runInContext(`
    renderStatus("awaiting_user_verification", {
      verification: {
        kind: "verification",
        job_id: "job-ev-1",
        reason: "Slide puzzle",
        evidence_screenshot: "http://127.0.0.1:8766/outputs/evidence/job1_attempt_1.png",
        evidence_label: "Lần kéo #1",
      },
      currentJob: null,
    });
  `, context);

  assert.equal(elements.verificationEvidence.style.display, 'block');
  assert.equal(elements.evidenceImage.src, 'http://127.0.0.1:8766/outputs/evidence/job1_attempt_1.png');
  assert.equal(elements.evidenceTitle.textContent, '📸 Bằng chứng CAPTCHA (Lần kéo #1):');

  // 2. Running job with evidence preview while verification is cleared
  vm.runInContext(`
    renderStatus("connected", {
      verification: null,
      currentJob: {
        id: "job-ev-1",
        stage: "crawling",
        _progress: {
          percent: 50,
          evidence_url: "http://127.0.0.1:8766/outputs/evidence/job1_resolved.png",
          evidence_label: "Đã giải xong ✔",
        }
      }
    });
  `, context);

  assert.equal(elements.verificationBox.style.display, 'none');
  assert.equal(elements.jobEvidencePreview.style.display, 'block');
  assert.equal(elements.jobEvidenceImg.src, 'http://127.0.0.1:8766/outputs/evidence/job1_resolved.png');
  assert.equal(elements.jobEvidenceTitle.textContent, '📸 Ảnh kiểm chứng (Đã giải xong ✔):');
});

