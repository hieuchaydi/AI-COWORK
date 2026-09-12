const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function observer({ initiallyPresent = false, unreadable = false } = {}) {
  let present = initiallyPresent, now = 10000, mutation, snapshotListener;
  let values = Array.from({ length: 160 }, (_, i) => i % 8);
  const messages = [];
  const image = { src: '', getBoundingClientRect: () => ({ width: 280, height: 150 }) };
  const root = { innerText: '', getBoundingClientRect: () => ({ width: 320, height: 260 }),
    querySelector: () => null, querySelectorAll: () => [image] };
  const context = vm.createContext({
    Date: { now: () => now }, Math, location: { href: 'https://shopee.vn/verify/captcha' },
    setTimeout: fn => { fn(); return 1; }, setInterval: () => 1, clearInterval() {},
    MutationObserver: class { constructor(fn) { mutation = fn; } observe() {} disconnect() {} },
    document: {
      body: null, querySelector: () => present ? root : null,
      createElement: () => ({ getContext: () => ({ drawImage() {}, getImageData() {
        if (unreadable) throw new Error('SecurityError: tainted canvas');
        return { data: values.flatMap(value => [value * 32, 0, 0, 255]) };
      } }) }),
    },
    chrome: { runtime: {
      lastError: null, sendMessage: (message, cb) => { messages.push(message.observation); cb(); },
      onMessage: { addListener: fn => { snapshotListener = fn; } },
    } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser-extension/captcha-observer.js'), 'utf8'), context);
  return {
    get messages() { return messages.filter(message => message.phase === 'present'); },
    allMessages: messages,
    show() { present = true; mutation(); },
    redraw(newValues) { values = newValues; now += 250; context.__coworkCaptchaObserver.scan(); },
    snapshot() { let result; snapshotListener({ action: 'captcha.snapshot' }, {}, value => { result = value; }); return result; },
  };
}

test('document-start observation timestamps the first painted challenge and snapshots never renew it', () => {
  const page = observer(); page.show();
  assert.equal(page.allMessages[0].phase, 'absent', 'a new document immediately invalidates work from the old document');
  assert.equal(page.messages.length, 1);
  assert.equal(page.messages[0].knownStart, true);
  assert.equal(page.messages[0].seenAt, 9750);
  assert.equal(page.snapshot().id, page.messages[0].id);
  assert.equal(page.messages.length, 1);
});

test('late injection into an already visible challenge reports unknown age', () => {
  const page = observer({ initiallyPresent: true });
  assert.equal(page.messages[0].knownStart, false);
});

test('canvas background replacement gets a new id but a small sprite movement does not', () => {
  const page = observer(); page.show();
  const original = page.messages[0].id;
  const small = Array.from({ length: 160 }, (_, i) => i % 8); small[0] = 7;
  page.redraw(small);
  assert.equal(page.messages.length, 1);
  page.redraw(Array.from({ length: 160 }, (_, i) => (i + 3) % 8));
  assert.equal(page.messages.length, 2);
  assert.notEqual(page.messages[1].id, original);
  assert.equal(page.messages[1].knownStart, true);
});

test('a tainted canvas explicitly reports that image identity cannot be tracked', () => {
  const page = observer({ unreadable: true }); page.show();
  assert.equal(page.messages[0].identityReliable, false);
});
