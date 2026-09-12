/* Per-image deadlines. Shared by the MV3 worker and deterministic Node tests. */
(function (scope) {
  "use strict";
  const LIFETIME_MS = 7000;
  const INPUT_RESERVE_MS = 1000;
  const MIN_ATTEMPT_MS = 2800;
  const MAX_JOB_ATTEMPTS = 3;
  const MAX_FRAME_ATTEMPTS = 2;

  class CaptchaError extends Error {
    constructor(code) { super(code); this.name = "CaptchaError"; this.code = code; }
  }

  class Ledger {
    constructor({ now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
      this.now = now;
      this.setTimer = setTimer;
      this.clearTimer = clearTimer;
      this.frames = new Map();
      this.jobs = new Map();
      this.active = new Map();
    }

    restore(snapshot = {}) {
      // Merge, never reset a live budget when the heartbeat reads older storage.
      for (const [key, saved] of Object.entries(snapshot || {})) {
        const job = this.job(key);
        job.attempts = Math.max(job.attempts, Number(saved.attempts) || 0);
        for (const [id, count] of Object.entries(saved.frames || {})) {
          job.frames[id] = Math.max(job.frames[id] || 0, Number(count) || 0);
        }
        job.passive = job.passive || Boolean(saved.passive);
      }
    }

    snapshot() {
      return Object.fromEntries([...this.jobs].map(([key, job]) => [key, {
        attempts: job.attempts, frames: { ...job.frames }, passive: job.passive,
      }]));
    }

    job(key) {
      if (!this.jobs.has(key)) this.jobs.set(key, { attempts: 0, frames: {}, passive: false });
      return this.jobs.get(key);
    }

    observe(tabId, observation) {
      if (!observation || typeof observation.id !== "string" || observation.id.length > 200) return null;
      if (observation.identityReliable === false) return null;
      const seenAt = Number(observation.seenAt);
      const observedAt = Number(observation.observedAt ?? seenAt);
      if (!Number.isFinite(seenAt) || !Number.isFinite(observedAt) || seenAt > this.now() + 250 || observedAt > this.now() + 250) return null;
      const previous = this.frames.get(tabId);
      if (previous && observedAt < previous.observedAt) return previous;
      const next = previous?.id === observation.id ? previous : {
        id: observation.id, seenAt, knownStart: observation.knownStart === true,
        expiresAt: seenAt + LIFETIME_MS, phase: "present",
      };
      next.phase = ["present", "absent", "locked", "resolved"].includes(observation.phase)
        ? observation.phase : "present";
      next.observedAt = observedAt;
      this.frames.set(tabId, next);
      const active = this.active.get(tabId);
      if (active && (active.frame.id !== next.id || next.phase === "locked" || (next.phase === "absent" && !active.confirming))) {
        active.abort(new CaptchaError(next.phase === "locked" ? "challenge_locked" : "challenge_changed"));
      }
      return next;
    }

    pause(key) { this.job(key).passive = true; }

    reserve(key, frameId) {
      const job = this.job(key);
      if (job.passive || job.attempts >= MAX_JOB_ATTEMPTS || (job.frames[frameId] || 0) >= MAX_FRAME_ATTEMPTS) {
        throw new CaptchaError("attempt_budget_exhausted");
      }
      job.attempts++;
      job.frames[frameId] = (job.frames[frameId] || 0) + 1;
    }

    async run(tabId, key, work, request = {}) {
      const frame = this.frames.get(tabId);
      const job = this.job(key);
      const refuse = reason => ({ ok: false, solved: false, attempted: false, reason, challengeId: frame?.id || null });
      if (this.active.has(tabId)) return refuse("attempt_in_progress");
      if (!frame || !frame.knownStart) return refuse("challenge_age_unknown");
      if (frame.phase !== "present") return refuse(`challenge_${frame.phase}`);
      if (job.passive || job.attempts >= MAX_JOB_ATTEMPTS || (job.frames[frame.id] || 0) >= MAX_FRAME_ATTEMPTS) {
        return refuse("attempt_budget_exhausted");
      }
      const deadlineAt = Math.min(frame.expiresAt, Number(request.deadlineAt) || Infinity);
      if (deadlineAt - INPUT_RESERVE_MS - this.now() < MIN_ATTEMPT_MS) return refuse("insufficient_time");

      const controller = new AbortController();
      const abort = reason => { if (!controller.signal.aborted) controller.abort(reason); };
      const fromRequest = () => abort(request.signal.reason || new CaptchaError("cancelled"));
      if (request.signal?.aborted) return refuse("cancelled");
      request.signal?.addEventListener("abort", fromRequest, { once: true });
      const timer = this.setTimer(() => abort(new CaptchaError("challenge_expired")), deadlineAt - this.now());
      const ctx = {
        frame, tabId, key, signal: controller.signal, deadlineAt, abort,
        attempted: false, evidence: null, timing: null,
        remaining: () => deadlineAt - this.now(),
        check: (reserve = 0) => {
          if (controller.signal.aborted) throw controller.signal.reason;
          if (this.frames.get(tabId)?.id !== frame.id) throw new CaptchaError("challenge_changed");
          if (deadlineAt - this.now() <= reserve) throw new CaptchaError("challenge_expired");
        },
        pressed: () => {
          ctx.check(INPUT_RESERVE_MS);
          if (!ctx.attempted) { this.reserve(key, frame.id); ctx.attempted = true; }
        },
        run: async (stage, operation, capMs = 1000, reserve = 0) => {
          ctx.check(reserve);
          let timerId;
          let onAbort;
          try {
            const stopped = new Promise((_, reject) => {
              onAbort = () => reject(controller.signal.reason);
              controller.signal.addEventListener("abort", onAbort, { once: true });
              timerId = this.setTimer(() => abort(new CaptchaError(`${stage}_timeout`)),
                Math.max(1, Math.min(capMs, ctx.remaining() - reserve)));
            });
            const value = await Promise.race([Promise.resolve().then(operation), stopped]);
            ctx.check(reserve);
            return value;
          } finally {
            this.clearTimer(timerId);
            controller.signal.removeEventListener("abort", onAbort);
          }
        },
        sleep: ms => ctx.run("wait", () => new Promise(resolve => this.setTimer(resolve, ms)), ms + 100),
      };
      this.active.set(tabId, ctx);
      try {
        const result = await ctx.run("challenge", () => work(ctx), ctx.remaining());
        return { ...result, attempted: ctx.attempted || Boolean(result?.attempted), challengeId: frame.id };
      } catch (error) {
        return { ...refuse(error?.code || error?.message || "captcha_failed"), attempted: ctx.attempted };
      } finally {
        this.clearTimer(timer);
        request.signal?.removeEventListener("abort", fromRequest);
        abort(new CaptchaError("attempt_finished"));
        if (this.active.get(tabId) === ctx) this.active.delete(tabId);
      }
    }

    cancel(tabId, reason = "cancelled") { this.active.get(tabId)?.abort(new CaptchaError(reason)); }
  }

  const api = { Ledger, CaptchaError, LIFETIME_MS, INPUT_RESERVE_MS, MIN_ATTEMPT_MS };
  scope.CaptchaLifecycle = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
