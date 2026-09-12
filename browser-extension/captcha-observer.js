/* Observe only the challenge surface. No network, mouse input or page-script hooks. */
(function () {
  "use strict";
  if (globalThis.__coworkCaptchaObserver) return;
  const rootSelector = ".shopee-captcha-slider, .captcha_container, [class*='captcha-modal'], [class*='captcha_wrapper'], [class*='shopee-captcha'], .verify-container, .verify-slider, .geetest_canvas_bg";
  const passedSelector = ".shopee-captcha-slider__btn--success, .captcha-passed, .verify-passed, [class*='slider'][class*='success']";
  const documentKey = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let sequence = 0, current = null, previousNode = null, previousSource = "", previousPixels = null;
  let observedAbsent = false, scanPending = false;
  const sample = document.createElement("canvas");
  sample.width = 16; sample.height = 10;

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function publish() {
    if (!current) return;
    try { chrome.runtime.sendMessage({ action: "captcha.observation", observation: current }, () => void chrome.runtime.lastError); } catch {}
  }
  function scan() {
    scanPending = false;
    const root = document.querySelector(rootSelector);
    const verifyUrl = /\/verify\/|anti_bot_tracking_id|captcha/i.test(location.href);
    const area = root || (verifyUrl ? document.body : null);
    const text = (area?.innerText || "").toLowerCase();
    let phase = /vui lòng thử lại sau|chưa thể hoàn tất xác thực|please try again later|too many attempts|verification failed/i.test(text) ? "locked" : "present";
    if (area && visible(area.querySelector(passedSelector))) phase = "resolved";
    const nodes = area ? [...area.querySelectorAll("canvas, img")] : [];
    const image = nodes.filter(el => {
      const r = el.getBoundingClientRect();
      return visible(el) && r.width >= 150 && r.height >= 80 && r.width / r.height >= 1.2 && r.width / r.height <= 2.5;
    }).sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
    if (!image && phase === "present") phase = "absent";
    if (phase !== "present") {
      if (phase === "absent") observedAbsent = true;
      if (!current) {
        current = { id: `${documentKey}:0`, seenAt: Date.now() - 250, observedAt: Date.now(), knownStart: false, phase };
        publish(); // Navigation must invalidate any in-flight work from the previous document.
      } else if (current.phase !== phase) { current = { ...current, phase, observedAt: Date.now() }; publish(); }
      return;
    }
    const source = image.currentSrc || image.src || "canvas";
    let pixels = null;
    try {
      const ctx = sample.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(image, 0, 0, 16, 10);
      const raw = ctx.getImageData(0, 0, 16, 10).data;
      pixels = Array.from({ length: 160 }, (_, i) => (raw[i * 4] >> 5) * 64 + (raw[i * 4 + 1] >> 5) * 8 + (raw[i * 4 + 2] >> 5));
      // A canvas has been inserted but its photograph has not been painted yet.
      if (new Set(pixels).size < 8) return;
    } catch { /* Cross-origin canvas: DOM/source changes remain observable. */ }
    const changedPixels = pixels && previousPixels
      ? pixels.filter((value, index) => value !== previousPixels[index]).length / pixels.length : 0;
    // Ignore small sprite movement. A new background changes a substantial part of the grid.
    const changed = !current || current.phase === "absent" || image !== previousNode || source !== previousSource || changedPixels > 0.25;
    if (changed) {
      current = {
        id: `${documentKey}:${++sequence}`, seenAt: Date.now() - 250, observedAt: Date.now(),
        knownStart: observedAbsent || Boolean(previousNode), phase: "present",
        identityReliable: Boolean(pixels) || source !== "canvas",
      };
      previousNode = image; previousSource = source; previousPixels = pixels;
      publish();
    }
  }
  function requestScan() {
    if (!scanPending) { scanPending = true; setTimeout(scan, 0); }
  }
  const observer = new MutationObserver(requestScan);
  observer.observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["src", "class", "style", "width", "height"] });
  const interval = setInterval(scan, 250); // Canvas repaint does not emit a DOM mutation.
  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message.action !== "captcha.snapshot") return;
    scan(); respond(current); return false;
  });
  globalThis.__coworkCaptchaObserver = { scan, stop: () => { observer.disconnect(); clearInterval(interval); } };
  scan();
})();
