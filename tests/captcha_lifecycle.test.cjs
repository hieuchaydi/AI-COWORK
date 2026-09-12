const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Ledger } = require('../browser-extension/captcha-lifecycle.js');

function clock() {
  let now = 10000, seq = 0;
  const timers = new Map();
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  return {
    now: () => now,
    setTimer: (fn, ms) => { timers.set(++seq, { fn, at: now + ms }); return seq; },
    clearTimer: id => timers.delete(id),
    async advance(ms) {
      await flush();
      const end = now + ms;
      for (;;) {
        const entry = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!entry) break;
        now = entry[1].at; timers.delete(entry[0]); entry[1].fn(); await flush();
      }
      now = end; await flush();
    },
  };
}
function frame(ledger, timer, id = 'image-1') {
  ledger.observe(7, { id, seenAt: timer.now(), knownStart: true, phase: 'present' });
}

test('first attempt fails and second is confirmed while the same 7 s image is still valid', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  frame(ledger, timer);
  const first = ledger.run(7, 'job', async ctx => {
    ctx.pressed(); await ctx.sleep(650);
    return { ok: false, solved: false, status: 'rejected' };
  });
  await timer.advance(700);
  assert.equal((await first).status, 'rejected');
  const second = ledger.run(7, 'job', async ctx => {
    ctx.pressed(); await ctx.sleep(800);
    return { ok: true, solved: true, status: 'confirmed' };
  });
  await timer.advance(850);
  assert.equal((await second).solved, true);
  assert.equal(ledger.job('job').attempts, 2);
  assert.equal(ledger.frames.get(7).expiresAt, 17000);
});

test('an old image cannot gain lifetime from duplicate observations or late entry', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  frame(ledger, timer);
  await timer.advance(4000);
  frame(ledger, timer); // Same id with a newer timestamp must not renew its lease.
  let executed = false;
  const result = await ledger.run(7, 'job', async () => { executed = true; });
  assert.equal(result.reason, 'insufficient_time');
  assert.equal(executed, false);
  assert.equal(ledger.frames.get(7).expiresAt, 17000);
});

test('image replacement cancels a slow solver and no stale continuation presses the mouse', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  frame(ledger, timer);
  let finishSolver, pressed = false;
  const pending = ledger.run(7, 'job', async ctx => {
    await ctx.run('solve', () => new Promise(resolve => { finishSolver = resolve; }), 900);
    ctx.pressed(); pressed = true;
  });
  await timer.advance(100);
  frame(ledger, timer, 'image-2');
  await timer.advance(0);
  assert.equal((await pending).reason, 'challenge_changed');
  finishSolver({ travel: 100 }); await timer.advance(0);
  assert.equal(pressed, false);
  assert.equal(ledger.job('job').attempts, 0);
});

test('stage deadline aborts the operation signal instead of leaving the next stage running', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  frame(ledger, timer);
  let signal, advanced = false;
  const pending = ledger.run(7, 'job', async ctx => {
    signal = ctx.signal;
    await ctx.run('capture', () => new Promise(() => {}), 600);
    advanced = true;
  });
  await timer.advance(601);
  assert.equal((await pending).reason, 'capture_timeout');
  assert.equal(signal.aborted, true);
  assert.equal(advanced, false);
});

test('command cancellation propagates into the running attempt', async () => {
  const timer = clock(), ledger = new Ledger(timer), controller = new AbortController();
  frame(ledger, timer);
  const pending = ledger.run(7, 'job', ctx => ctx.run('solve', () => new Promise(() => {}), 900), { signal: controller.signal });
  await timer.advance(10);
  controller.abort(new Error('CANCELLED'));
  await timer.advance(0);
  assert.equal((await pending).reason, 'CANCELLED');
  assert.equal(ledger.active.size, 0);
});

test('heartbeat restore and worker restart preserve consumed attempts and passive handover', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  ledger.reserve('job', 'image-1'); ledger.reserve('job', 'image-1'); ledger.reserve('job', 'image-2');
  ledger.restore({ job: { attempts: 0, frames: {} } });
  assert.equal(ledger.job('job').attempts, 3);
  ledger.pause('job');
  const restored = new Ledger(timer); restored.restore(ledger.snapshot()); frame(restored, timer, 'image-3');
  assert.equal((await restored.run(7, 'job', () => assert.fail('budget renewed'))).reason, 'attempt_budget_exhausted');
});

test('already visible image with unknown age is handed over without guessing a fresh deadline', async () => {
  const timer = clock(), ledger = new Ledger(timer);
  ledger.observe(7, { id: 'existing', seenAt: timer.now(), knownStart: false, phase: 'present' });
  assert.equal((await ledger.run(7, 'job', () => assert.fail('unknown-age image executed'))).reason, 'challenge_age_unknown');
});

test('request deadline includes preparation and queue time, even when image remains valid', async () => {
  const timer = clock(), ledger = new Ledger(timer); frame(ledger, timer);
  const result = await ledger.run(7, 'job', () => assert.fail('insufficient command budget'), { deadlineAt: timer.now() + 2000 });
  assert.equal(result.reason, 'insufficient_time');
});

test('only one attempt can run for a tab at a time', async () => {
  const timer = clock(), ledger = new Ledger(timer); frame(ledger, timer);
  const first = ledger.run(7, 'job', ctx => ctx.sleep(600));
  await timer.advance(10);
  assert.equal((await ledger.run(7, 'job', () => {})).reason, 'attempt_in_progress');
  await timer.advance(600); await first;
});

test('unknown canvas identity and out-of-order old-image notifications cannot replace the current image', () => {
  const timer = clock(), ledger = new Ledger(timer); frame(ledger, timer, 'current');
  ledger.observe(7, { id: 'old', seenAt: 9000, knownStart: true, phase: 'present' });
  ledger.observe(7, { id: 'unreadable', seenAt: 10000, knownStart: true, phase: 'present', identityReliable: false });
  assert.equal(ledger.frames.get(7).id, 'current');
});
