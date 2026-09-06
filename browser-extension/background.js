// AI Cowork — Manifest V3 Chrome Extension Browser Bridge (v2.0.0)
//
// Realtime WebSocket control bridge & ingest worker.
// Operates in the user's real Chrome profile with ordinary session cookies.
//
// Features:
// 1. Enveloped Protocol v1 (command, accepted, progress, result, error, cancel, ping/pong, verification.required).
// 2. Precompiled Typed Action Registry (18 actions, strictly NO eval / NO new Function).
// 3. Same-origin fetch enforcement in tab context.
// 4. Verification & Captcha detection with graceful workflow pause/resume.
// 5. Exponential backoff reconnect with random jitter and 20s keepalive heartbeats.
// 6. Backward-compatible Shopee reviews extraction and acknowledged WebSocket result uploads.

const HELPER = "http://127.0.0.1:8766";
const PAGE_SIZE = 50;
const MAX_REVIEWS = 20000;
const PACE_MS = 700;

// ── State Variables ──────────────────────────────────────────────────────────
let bridgeSocket = null;
let bridgeConnecting = null;
let bridgeHeartbeat = null;
let bridgeReconnectTimeout = null;
let reconnectAttempts = 0;
let connectionGeneration = 0;
let connectionEnabled = true;
let bridgeHandshakeTimeout = null;
let lastBridgeMessageAt = 0;
let pairingController = null;
let jobChain = Promise.resolve();
let extensionState = "disconnected";
let activeJobs = new Map();
let inFlightCommands = new Map();
let verificationInfo = null;
let pendingVerificationJobs = new Map();
let lastVerificationJob = null;

function setBadge(text, color = "#1a73e8") {
  chrome.action.setBadgeText({ text: String(text || "") });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

function updateState(newState, details = null) {
  extensionState = newState;
  if (newState === "awaiting_user_verification") {
    verificationInfo = details;
    setBadge("PAUS", "#f9ab00");
  } else if (newState === "connected") {
    verificationInfo = null;
    setBadge("OK", "#137333");
  } else if (newState === "busy") {
    setBadge("BUSY", "#1a73e8");
  } else {
    verificationInfo = null;
    setBadge("");
  }
  chrome.storage.local.set({
    extensionState,
    verificationInfo,
    currentJob: activeJobs.size > 0 ? Array.from(activeJobs.values())[0] : null,
  });
}

// ── Ingest & Progress Helpers ────────────────────────────────────────────────
const pendingIngestRequests = new Map();
const knownJobs = new Set();

function sendIngestRequest(params) {
  const id = crypto.randomUUID();
  const message = { v: 1, type: "ingest.rpc", id, params };
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const retry = () => {
      if (Date.now() - started > 300000) {
        clearInterval(timer);
        pendingIngestRequests.delete(id);
        reject(new Error("WebSocket ingest acknowledgement timed out"));
        return;
      }
      bridgeSend(message);
    };
    const timer = setInterval(retry, 5000);
    pendingIngestRequests.set(id, { resolve, reject, timer, message });
    retry();
  });
}

async function reportProgress(job, progress) {
  if (job && job.id) await sendIngestRequest({ operation: "progress", job: job.id, progress });
}

async function uploadIngestResult(body) {
  const uploadId = crypto.randomUUID();
  const payload = JSON.stringify(body);
  // Every frame stays below the gateway's 8 MiB limit, even after JSON escaping.
  for (let offset = 0, index = 0; offset < payload.length; index++) {
    let end = Math.min(offset + 65536, payload.length);
    const lastCode = payload.charCodeAt(end - 1);
    if (end < payload.length && lastCode >= 0xD800 && lastCode <= 0xDBFF) end--;
    await sendIngestRequest({ operation: "chunk", uploadId, index, chunk: payload.slice(offset, end) });
    offset = end;
  }
  return sendIngestRequest({ operation: "complete", uploadId });
}

function mediaUrl(value, kind = "image") {
  if (!value) return "";
  if (typeof value === "object") {
    value = value.url || value.video_url || value.play_url || value.cover || value.image_id || value.id || "";
  }
  value = String(value || "").trim();
  if (!value) return "";
  if (value.startsWith("//")) return "https:" + value;
  if (/^https?:\/\//i.test(value)) return value;
  const base = "https://down-vn.img.susercontent.com/file/";
  return base + value;
}

function asList(value) {
  if (Array.isArray(value)) return value;
  return value ? [value] : [];
}

function normaliseRating(x) {
  const imageUrls = asList(x.images).map((v) => mediaUrl(v, "image")).filter(Boolean);
  const videoUrls = asList(x.videos || x.video).map((v) => mediaUrl(v, "video")).filter(Boolean);
  const mediaUrls = [...imageUrls, ...videoUrls];
  return {
    user: x.author_username || "",
    sao: x.rating_star,
    noi_dung: (x.comment || "").replace(/\s+/g, " ").trim(),
    thoi_gian: new Date((x.ctime || 0) * 1000).toISOString().slice(0, 19).replace("T", " "),
    phan_loai: (x.product_items || []).map((p) => p.model_name).filter(Boolean).join("|"),
    anh: imageUrls.length ? 1 : 0,
    so_anh: imageUrls.length,
    anh_urls: imageUrls.join("|"),
    video: videoUrls.length ? 1 : 0,
    so_video: videoUrls.length,
    video_urls: videoUrls.join("|"),
    media_urls: mediaUrls.join("|"),
    huu_ich: x.like_count || 0,
  };
}

function ratingTotal(j) {
  const data = j && j.data;
  const summary = data && (data.item_rating_summary || data.product_rating_summary || data.rating_summary);
  const total = data && (data.total || data.count || (summary && (summary.rating_total || summary.total_count || summary.count)));
  const n = Number(total);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function crawlPercent(rows, total, base, span) {
  if (total) return Math.min(95, base + Math.floor((Math.min(rows, total) / total) * span));
  return Math.min(95, base + Math.floor((rows / MAX_REVIEWS) * span));
}

function extractShopName(url) {
  try {
    const u = new URL(url, "https://shopee.vn");
    const reserved = new Set([
      "product", "cart", "user", "buyer", "search", "flash_sale",
      "daily_discover", "m", "portal", "verify", "api", "order",
      "checkout", "help", "seller", "affiliate"
    ]);
    const parts = u.pathname.split("/").filter(Boolean);
    if (parts.length >= 2 && !reserved.has(parts[0].toLowerCase())) {
      return parts[0];
    }
  } catch {}
  return null;
}

function idsFrom(url) {
  try {
    const u = new URL(url, "https://shopee.vn");
    const q = u.searchParams;
    const qItem = q.get("itemid") || q.get("item_id");
    if (qItem) return { shopid: q.get("shopid") || q.get("shop_id") || null, itemid: qItem, shopname: extractShopName(url) };
    let m = u.pathname.match(/^\/product\/(\d+)\/(\d+)/) || u.href.match(/i\.(\d+)\.(\d+)/);
    if (m) return { shopid: m[1], itemid: m[2], shopname: null };
    const tail = u.pathname.match(/\/(\d{6,})\/?$/);
    if (tail) return { shopid: null, itemid: tail[1], shopname: extractShopName(url) };
    const any = u.href.match(/[.\-/](\d{8,})(?:[?&#/]|$)/);
    return any ? { shopid: null, itemid: any[1], shopname: extractShopName(url) } : {};
  } catch {
    return {};
  }
}

async function findOrOpenShopeeTab(targetUrl, itemid) {
  const tabs = await chrome.tabs.query({});
  // 1. Prefer a tab already displaying this item
  let tab = tabs.find((t) => t.url && itemid && t.url.includes(itemid));
  // 2. Or create a new tab if none exists with this item
  if (!tab) {
    tab = await chrome.tabs.create({ url: targetUrl, active: false });
    await new Promise((resolve) => {
      let timer = null;
      const listener = (tid, changeInfo) => {
        if (tid === tab.id && changeInfo.status === "complete") {
          cleanup();
          resolve();
        }
      };
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
      };
      chrome.tabs.onUpdated.addListener(listener);
      timer = setTimeout(() => {
        cleanup();
        resolve();
      }, 15000);
    });
    try {
      const updated = await chrome.tabs.get(tab.id);
      if (updated) tab = updated;
    } catch {}
  }
  return tab;
}

async function resolveShopId(itemid, originalUrl, tab, progress) {
  // Support both (itemid, originalUrl, tab, progress) and (itemid, originalUrl, progress)
  if (typeof tab === "function" && progress === undefined) {
    progress = tab;
    tab = null;
  }
  if (progress) await progress({ stage: "resolve-shop", message: "Đang tìm shopid", itemid, percent: 10 });

  const shopname = extractShopName(originalUrl);

  function scrape(text) {
    if (!text || typeof text !== "string") return null;
    const m = text.match(/"shopid"\s*:\s*"?(\d+)/i) ||
              text.match(/"shop_id"\s*:\s*"?(\d+)/i) ||
              text.match(/"shopId"\s*:\s*"?(\d+)/);
    return m ? m[1] : null;
  }

  function extractShopIdFromUrl(urlStr) {
    if (!urlStr || typeof urlStr !== "string") return null;
    try {
      const u = new URL(urlStr, "https://shopee.vn");
      const q = u.searchParams.get("shopid") || u.searchParams.get("shop_id");
      if (q) return q;
      const m = u.pathname.match(/^\/product\/(\d+)\//) || u.href.match(/i\.(\d+)\./);
      if (m) return m[1];
    } catch {}
    return null;
  }

  // 1. Try URL regex from current tab URL (might have redirected to /product/<shopid>/<itemid>)
  if (tab && tab.url) {
    const sid = extractShopIdFromUrl(tab.url);
    if (sid) return sid;
  }

  // 2. Scrape from DOM / canonical / same-origin in tab context (highest fidelity with user session)
  if (tab && tab.id) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: async (sname, iid) => {
          // 2a. Check current location URL
          const fromLoc = location.pathname.match(/^\/product\/(\d+)\//) || location.href.match(/i\.(\d+)\./);
          if (fromLoc) return fromLoc[1];

          // 2b. Check canonical or og:url meta tags
          const canonical = document.querySelector('link[rel="canonical"]')?.href || "";
          const ogUrl = document.querySelector('meta[property="og:url"]')?.content || "";
          for (const u of [canonical, ogUrl]) {
            const m = u.match(/\/product\/(\d+)\//) || u.match(/i\.(\d+)\./);
            if (m) return m[1];
          }

          // 2c. Scrape from DOM in tab context
          const html = document.documentElement.innerHTML;
          const s = html.match(/"shopid"\s*:\s*"?(\d+)/i) ||
                    html.match(/"shop_id"\s*:\s*"?(\d+)/i) ||
                    html.match(/"shopId"\s*:\s*"?(\d+)/);
          if (s) return s[1];

          // 2d. For vanity URL /<shopname>/<itemid>, resolve shopid via same-origin tab fetch
          if (sname) {
            try {
              const resp = await fetch(`/api/v4/shop/get_shop_detail?username=${encodeURIComponent(sname)}`, {
                credentials: "include",
                headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
              });
              if (resp.ok) {
                const j = await resp.json();
                const sid = j?.data?.shopid || j?.data?.shop_id;
                if (sid) return String(sid);
              }
            } catch {}
          }

          // 2e. Try PDP API from tab
          if (iid) {
            try {
              const resp = await fetch(`/api/v4/pdp/get_pc?item_id=${iid}`, {
                credentials: "include",
                headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
              });
              if (resp.ok) {
                const j = await resp.json();
                const sid = j?.data?.item?.shopid || j?.data?.item?.shop_id || j?.data?.shop_id;
                if (sid) return String(sid);
              }
            } catch {}
          }

          return null;
        },
        args: [shopname, itemid],
      });
      const sid = res[0]?.result;
      if (sid) return sid;
    } catch {}
  }

  // 3. Fallback: Try URL regex from original URL
  const fromOrig = extractShopIdFromUrl(originalUrl);
  if (fromOrig) return fromOrig;

  // 4. Fallback: Background fetch original URL
  try {
    const r = await fetch(originalUrl, {
      credentials: "include",
      headers: { "User-Agent": navigator.userAgent },
    });
    if (r.ok) {
      const s = scrape(await r.text());
      if (s) return s;
      const fromRedir = extractShopIdFromUrl(r.url);
      if (fromRedir) return fromRedir;
    }
  } catch {}

  // 5. Fallback: Background fetch shop detail by vanity shopname
  if (shopname) {
    try {
      const r = await fetch(`https://shopee.vn/api/v4/shop/get_shop_detail?username=${encodeURIComponent(shopname)}`, {
        credentials: "include",
        headers: { "User-Agent": navigator.userAgent, "Accept": "application/json" },
      });
      if (r.ok) {
        const j = await r.json();
        const sid = j?.data?.shopid || j?.data?.shop_id;
        if (sid) return String(sid);
      }
    } catch {}
  }

  return null;
}

async function fetchRatingsFromTab(tabId, itemid, shopid, offset, limit, referer) {
  const res = await chrome.scripting.executeScript({
    target: { tabId },
    func: async (iid, sid, off, lim, ref) => {
      const path = `/api/v2/item/get_ratings?filter=0&flag=1&itemid=${iid}&limit=${lim}&offset=${off}&shopid=${sid}&type=0`;
      try {
        const resp = await fetch(path, {
          credentials: "include",
          headers: {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "X-Api-Source": "pc",
            "X-Shopee-Language": "vi",
          },
          referrer: ref,
          referrerPolicy: "strict-origin-when-cross-origin",
        });
        const text = await resp.text();
        let json = null;
        try { json = JSON.parse(text); } catch {}
        return {
          ok: resp.ok,
          status: resp.status,
          url: resp.url,
          pageUrl: location.href,
          json,
          textSample: text.slice(0, 300),
        };
      } catch (err) {
        return { ok: false, error: err.message };
      }
    },
    args: [itemid, shopid, offset, limit, referer],
  });
  return res[0]?.result || { ok: false, error: "executeScript returned no result" };
}

function classifyShopeeFailure(fetchRes) {
  const json = fetchRes?.json || {};
  const data = json?.data || {};
  const url = String(fetchRes?.url || "").toLowerCase();
  const sample = String(fetchRes?.textSample || "").toLowerCase();
  const isLogin = json?.error === 90309999 || json?.is_login === false || data?.is_login === false ||
                  url.includes("/login") || sample.includes("is_login") || sample.includes("90309999");
  if (isLogin) return "login";

  const isChallenge = url.includes("/verify/traffic") || sample.includes("captcha") ||
                      sample.includes("challenge") || sample.includes("verify/traffic");
  if (isChallenge) return "verification";
  return "api_blocked";
}

function formatShopeeFailure(fetchRes) {
  const kind = classifyShopeeFailure(fetchRes);
  const status = fetchRes?.status ?? "unknown";
  const endpoint = fetchRes?.url || "unknown endpoint";
  const page = fetchRes?.pageUrl || "unknown tab";
  const sample = String(fetchRes?.textSample || "").replace(/\s+/g, " ").slice(0, 240);
  if (kind === "login") {
    return `Shopee login required (HTTP ${status}, error=${fetchRes?.json?.error ?? "unknown"}, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome`;
  }
  if (kind === "verification") {
    return `Shopee verification required (HTTP ${status}) — response=${endpoint}`;
  }
  return `Shopee reviews API access denied (HTTP ${status}) — không thấy CAPTCHA trên tab; endpoint=${endpoint}; tab=${page}; response=${sample || "empty"}`;
}

async function extractShopeeReviews(job, progress) {
  const parsed = idsFrom(job.url);
  let itemid = parsed.itemid;
  let shopid = parsed.shopid;
  if (!itemid) throw new Error(`Không đọc được itemid từ URL: ${job.url}`);

  // Find or attach to a Shopee tab in the user's browser FIRST to leverage the real user session
  const tab = await findOrOpenShopeeTab(job.url, itemid);
  if (!tab || !tab.id) throw new Error("Không tìm thấy hoặc không mở được tab Shopee trên trình duyệt");
  job._targetTabId = tab.id;

  if (!shopid) shopid = await resolveShopId(itemid, job.url, tab, progress);
  if (!shopid) throw new Error(`Không tìm thấy shopid cho item ${itemid}`);

  let all = [];
  let offset = 0;
  let total = null;

  while (all.length < MAX_REVIEWS) {
    const fetchRes = await fetchRatingsFromTab(tab.id, itemid, shopid, offset, PAGE_SIZE, tab.url || job.url);
    if (!fetchRes.ok) {
      throw new Error(fetchRes.error ? `Shopee API request failed: ${fetchRes.error}` : formatShopeeFailure(fetchRes));
    }

    const json = fetchRes.json;
    if (json && (json.error === 90309999 || json.is_login === false || (json.data && json.data.is_login === false))) {
      throw new Error("Shopee login required (error 90309999, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome");
    }

    const ratings = (json && json.data && json.data.ratings) || [];
    if (!ratings.length) break;
    if (total === null) total = ratingTotal(json);

    for (const r of ratings) all.push(normaliseRating(r));
    if (progress) {
      await progress({
        stage: "fetch-tab",
        message: `Đã lấy ${all.length} đánh giá qua tab Shopee`,
        rows: all.length,
        percent: crawlPercent(all.length, total, 20, 70),
      });
    }
    if (ratings.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
    await new Promise((s) => setTimeout(s, PACE_MS));
  }
  return all;
}

async function runJob(job) {
  console.log("[bridge] ▶ start job", job.id, job.kind, job.url);
  activeJobs.set(job.id, job);
  updateState("busy");

  const progress = (patch) => reportProgress(job, patch);
  try {
    await progress({ status: "running", stage: "init", message: "Bắt đầu cào", percent: 5 });
    let rows = [];
    if (job.kind === "shopee-reviews" || job.url.includes("shopee.vn")) {
      rows = await extractShopeeReviews(job, progress);
    }
    const parsed = idsFrom(job.url);
    const itemid = parsed.itemid || job.itemid;
    const outputName = itemid ? "shopee_" + itemid + "_reviews" : (job.name || "shopee_" + job.id);
    await progress({ status: "saving", stage: "upload", message: `Đang lưu ${rows.length} dòng`, rows: rows.length, percent: 95 });
    await uploadIngestResult({ job: job.id, name: outputName, source: job.url, rows });
    console.log("[bridge] ✔ job", job.id, "finished:", rows.length, "rows");
  } catch (e) {
    console.error("[bridge] ✘ job", job.id, "failed:", e.message);
    const message = String(e.message || e);
    const isVerification = message.includes("verification required") ||
                           message.includes("/verify/traffic");
    const isLoginRequired = message.includes("login required") ||
                            message.includes("is_login=false") ||
                            message.includes("90309999");
    if (isVerification || isLoginRequired) {
      lastVerificationJob = job;
      pendingVerificationJobs.set(job.id, job);
      triggerVerificationRequired({
        job_id: job.id,
        url: job.url,
        tab_id: job._targetTabId || null,
        reason: message,
        kind: isLoginRequired ? "login" : "verification",
      });
    }
    await progress({
      status: (isVerification || isLoginRequired) ? "awaiting_user_verification" : "error",
      stage: isLoginRequired ? "login_required" : (isVerification ? "verification_required" : "failed"),
      message,
      percent: 100
    });
    await uploadIngestResult({
      job: job.id,
      error: message,
      verification_required: isVerification,
      login_required: isLoginRequired,
    });
  } finally {
    activeJobs.delete(job.id);
    updateState(verificationInfo ? "awaiting_user_verification" : (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN ? "connected" : "disconnected"));
  }
}

// ── Verification Challenge Trigger ──────────────────────────────────────────
const notifiedVerificationJobIds = new Set();

function triggerVerificationRequired(details) {
  console.warn("[bridge] ⚠️ Verification required:", details);
  updateState("awaiting_user_verification", details);
  const jid = details?.job_id;
  if (jid && notifiedVerificationJobIds.has(jid)) {
    return; // Đảm bảo gửi một thông báo duy nhất cho người dùng
  }
  if (jid) notifiedVerificationJobIds.add(jid);

  if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
    bridgeSend({
      v: 1,
      type: "verification.required",
      id: "verif-" + Date.now(),
      params: details,
    });
  }
}

function resumeVerification(jobId) {
  console.log("[bridge] Resuming verification, jobId:", jobId);
  if (jobId) notifiedVerificationJobIds.delete(jobId);
  updateState("connected");
  if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
    bridgeSend({
      v: 1,
      type: "verification.resolved",
      id: "resumed-" + Date.now(),
      params: { status: "resumed", message: "User confirmed verification in tab", jobId },
    });
  }
  const jobToResume = (jobId && pendingVerificationJobs.get(jobId)) || lastVerificationJob;
  if (jobToResume) {
    console.log("[bridge] Retrying job after verification:", jobToResume.id);
    pendingVerificationJobs.delete(jobToResume.id);
    if (lastVerificationJob && lastVerificationJob.id === jobToResume.id) {
      lastVerificationJob = null;
    }
    knownJobs.delete(jobToResume.id);
    enqueueLegacyJob(jobToResume);
    if (!activeJobs.has(jobToResume.id)) {
      enqueueLegacyJob(jobToResume);
    }
  }
}

// ── Action Registry Handlers (18 Precompiled Actions) ───────────────────────
async function handleAction(action, params) {
  params = params || {};
  switch (action) {
    case "browser.health":
      return {
        status: "ok",
        version: "2.0.0",
        connected: bridgeSocket ? bridgeSocket.readyState === WebSocket.OPEN : false,
        activeJobsCount: activeJobs.size,
        extensionState,
      };

    case "tab.list": {
      const tabs = await chrome.tabs.query({});
      return {
        tabs: tabs.map((t) => ({
          tabId: t.id,
          url: t.url,
          title: t.title,
          active: t.active,
          status: t.status,
        })),
      };
    }

    case "tab.getActive": {
      const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!activeTab) throw new Error("No active tab found");
      return {
        tabId: activeTab.id,
        url: activeTab.url,
        title: activeTab.title,
        status: activeTab.status,
      };
    }

    case "tab.open": {
      const tab = await chrome.tabs.create({ url: params.url, active: params.active !== false });
      return { tabId: tab.id, url: tab.url };
    }

    case "tab.focus": {
      const tab = await chrome.tabs.update(params.tabId, { active: true });
      if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
      return { tabId: tab.id, focused: true };
    }

    case "page.navigate": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const waitUntil = params.waitUntil || "load"; // load, domcontentloaded, networkidle

      const navigationPromise = new Promise((resolve) => {
        let timer = null;
        const onUpdated = (tabId, changeInfo, tab) => {
          if (tabId === targetTabId) {
            if (waitUntil === "domcontentloaded") {
              if (changeInfo.status === "loading" || changeInfo.status === "complete") {
                cleanup();
                resolve({ tabId: targetTabId, url: tab.url, loaded: true });
              }
            } else {
              if (changeInfo.status === "complete") {
                cleanup();
                resolve({ tabId: targetTabId, url: tab.url, loaded: true });
              }
            }
          }
        };

        const cleanup = () => {
          if (timer) clearTimeout(timer);
          chrome.tabs.onUpdated.removeListener(onUpdated);
        };

        chrome.tabs.onUpdated.addListener(onUpdated);
        timer = setTimeout(() => {
          cleanup();
          resolve({ tabId: targetTabId, url: params.url, loaded: false, timeout: true });
        }, Math.max(3000, Number(params.timeoutMs) || 30000));
      });

      await chrome.tabs.update(targetTabId, { url: params.url });
      return await navigationPromise;
    }

    case "page.getUrl": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const tab = await chrome.tabs.get(targetTabId);
      return { tabId: tab.id, url: tab.url, title: tab.title };
    }

    case "page.getTitle": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const tab = await chrome.tabs.get(targetTabId);
      return { tabId: tab.id, title: tab.title };
    }

    case "page.waitFor": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const state = params.state || "visible"; // visible, hidden, attached, detached
      const timeoutMs = Math.max(100, Number(params.timeoutMs) || 5000);

      const pollScript = async (selector, expectedState, maxWaitMs) => {
        const start = Date.now();
        const checkState = (el, st) => {
          const isAttached = !!el;
          if (st === "attached") return isAttached;
          if (st === "detached") return !isAttached;
          if (!isAttached) return st === "hidden";
          const rects = el.getClientRects();
          const isVisible =
            rects.length > 0 &&
            (el.offsetWidth > 0 || el.offsetHeight > 0) &&
            window.getComputedStyle(el).visibility !== "hidden";
          if (st === "visible") return isVisible;
          if (st === "hidden") return !isVisible;
          return false;
        };

        while (Date.now() - start < maxWaitMs) {
          const el = document.querySelector(selector);
          if (checkState(el, expectedState)) {
            return { found: true, elapsedMs: Date.now() - start };
          }
          await new Promise((r) => setTimeout(r, 100));
        }
        return { found: false, elapsedMs: Date.now() - start };
      };

      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: pollScript,
        args: [params.selector, state, timeoutMs],
      });

      const outcome = res[0]?.result;
      if (!outcome || !outcome.found) {
        throw new Error(`Timeout waiting for selector '${params.selector}' to be ${state}`);
      }
      return { found: true, elapsedMs: outcome.elapsedMs };
    }

    case "dom.query": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector) => {
          const el = document.querySelector(selector);
          return el ? { found: true, tag: el.tagName.toLowerCase() } : { found: false };
        },
        args: [params.selector],
      });
      return res[0]?.result || { found: false };
    }

    case "dom.queryAll": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector, limit) => {
          const nodes = Array.from(document.querySelectorAll(selector)).slice(0, limit || 50);
          return nodes.map((n) => ({
            tag: n.tagName.toLowerCase(),
            text: (n.innerText || "").slice(0, 100),
          }));
        },
        args: [params.selector, params.limit || 50],
      });
      return { count: res[0]?.result?.length || 0, items: res[0]?.result || [] };
    }

    case "dom.getText": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector, maxChars) => {
          const el = document.querySelector(selector);
          return el ? (el.innerText || "").slice(0, maxChars || 8000) : null;
        },
        args: [params.selector, params.maxChars || 8000],
      });
      return { text: res[0]?.result };
    }

    case "dom.getAttribute": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector, attr) => {
          const el = document.querySelector(selector);
          return el ? el.getAttribute(attr) : null;
        },
        args: [params.selector, params.attribute],
      });
      return { attribute: params.attribute, value: res[0]?.result };
    }

    case "dom.click": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector) => {
          const el = document.querySelector(selector);
          if (!el) return { ok: false, error: "ELEMENT_NOT_FOUND" };
          el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
          el.click();
          return { ok: true, clicked: true };
        },
        args: [params.selector],
      });
      const outcome = res[0]?.result;
      if (!outcome || !outcome.ok) {
        throw new Error(`ELEMENT_NOT_FOUND: Element not found for selector '${params.selector}'`);
      }
      return outcome;
    }

    case "input.type": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector, text, clearFirst, submit) => {
          const el = document.querySelector(selector);
          if (!el) return { ok: false, error: "ELEMENT_NOT_FOUND" };

          const nativeSetter =
            Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set ||
            Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;

          if (clearFirst) {
            if (nativeSetter) {
              nativeSetter.call(el, "");
            } else {
              el.value = "";
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
          }

          const newVal = (clearFirst ? "" : el.value) + text;
          if (nativeSetter) {
            nativeSetter.call(el, newVal);
          } else {
            el.value = newVal;
          }
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));

          if (submit && el.form) {
            el.form.dispatchEvent(new Event("submit", { bubbles: true }));
          }
          return { ok: true, typedLength: text.length };
        },
        args: [params.selector, params.text, params.clearFirst !== false, !!params.submit],
      });
      const outcome = res[0]?.result;
      if (!outcome || !outcome.ok) {
        throw new Error(`ELEMENT_NOT_FOUND: Element not found for selector '${params.selector}'`);
      }
      return outcome;
    }

    case "input.select": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (selector, val) => {
          const el = document.querySelector(selector);
          if (!el) return { ok: false, error: "ELEMENT_NOT_FOUND" };
          el.value = val;
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: true, selected: val };
        },
        args: [params.selector, params.value],
      });
      const outcome = res[0]?.result;
      if (!outcome || !outcome.ok) {
        throw new Error(`ELEMENT_NOT_FOUND: Element not found for selector '${params.selector}'`);
      }
      return outcome;
    }

    case "page.snapshot": {
      const format = (params.format || "text").toLowerCase();
      if (format !== "text" && format !== "html") {
        throw new Error(`UNSUPPORTED_ACTION_PARAM: format '${params.format}' is not supported (only 'text' and 'html')`);
      }
      const targetTabId = params.tabId || (await getActiveTabId());
      const maxChars = Math.min(500000, Math.max(100, Number(params.maxChars) || 50000));
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (fmt, maxLen) => {
          if (fmt === "html") {
            return (document.documentElement.outerHTML || "").slice(0, maxLen);
          }
          return (document.body ? document.body.innerText : "").slice(0, maxLen);
        },
        args: [format, maxChars],
      });
      return { format, snapshot: res[0]?.result || "" };
    }

    case "fetch.sameOrigin": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const tab = await chrome.tabs.get(targetTabId);
      if (!tab.url) throw new Error("Tab has no URL for origin verification");

      let tabUrlObj;
      try {
        tabUrlObj = new URL(tab.url);
      } catch {
        throw new Error("Invalid tab URL");
      }
      if (tabUrlObj.protocol !== "http:" && tabUrlObj.protocol !== "https:") {
        throw new Error(`Tab protocol '${tabUrlObj.protocol}' is not allowed for sameOrigin fetch`);
      }
      if (tabUrlObj.username || tabUrlObj.password) {
        throw new Error("Tab URL containing userinfo (@) is not allowed");
      }

      let targetUrlObj;
      try {
        targetUrlObj = new URL(params.pathOrUrl, tab.url);
      } catch {
        throw new Error("Invalid target path or URL");
      }

      if (targetUrlObj.protocol !== "http:" && targetUrlObj.protocol !== "https:") {
        throw new Error(`Target protocol '${targetUrlObj.protocol}' is not allowed`);
      }
      if (targetUrlObj.username || targetUrlObj.password) {
        throw new Error("Target URL containing userinfo (@) is not allowed");
      }

      if (targetUrlObj.origin !== tabUrlObj.origin) {
        throw new Error(`Cross-origin fetch rejected. Tab origin: ${tabUrlObj.origin}, Target: ${targetUrlObj.origin}`);
      }

      const forbiddenHeaders = [
        "authorization",
        "cookie",
        "cookie2",
        "host",
        "origin",
        "referer",
        "content-length",
        "connection",
        "keep-alive",
        "upgrade",
        "proxy-authorization",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
        "te",
        "trailer",
        "transfer-encoding",
      ];
      const safeHeaders = {};
      if (params.headers && typeof params.headers === "object") {
        for (const [k, v] of Object.entries(params.headers)) {
          const lower = k.toLowerCase().trim();
          if (forbiddenHeaders.includes(lower)) {
            throw new Error(`Forbidden header in fetch.sameOrigin: '${k}'`);
          }
          safeHeaders[k] = v;
        }
      }

      const maxBytes = Math.min(
        10 * 1024 * 1024,
        Math.max(1024, Number(params.maxResponseBytes) || 200 * 1024)
      );

      const targetUrl = targetUrlObj.href;

      // Execute fetch within tab context so Chrome profile cookies attach naturally
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: async (url, method, headers, body, maxLimit) => {
          const opts = {
            method: method || "GET",
            headers: headers || {},
            credentials: "include",
          };
          if (body && ["POST", "PUT", "PATCH"].includes(opts.method.toUpperCase())) {
            opts.body = typeof body === "object" ? JSON.stringify(body) : String(body);
          }
          const resp = await fetch(url, opts);
          const text = await resp.text();
          const byteLen = new TextEncoder().encode(text).length;
          if (byteLen > maxLimit) {
            return {
              status: resp.status,
              ok: false,
              error: `Response size (${byteLen} bytes) exceeds limit of ${maxLimit} bytes`,
            };
          }
          let parsedJson = null;
          try {
            parsedJson = JSON.parse(text);
          } catch {}
          return {
            status: resp.status,
            ok: resp.ok,
            url: resp.url,
            data: parsedJson !== null ? parsedJson : text,
          };
        },
        args: [targetUrl, params.method || "GET", safeHeaders, params.body || null, maxBytes],
      });

      const fetchResult = res[0]?.result;
      if (fetchResult && fetchResult.error) {
        throw new Error(fetchResult.error);
      }
      if (fetchResult && (fetchResult.status === 403 || (typeof fetchResult.data === "string" && fetchResult.data.includes("/verify/traffic")))) {
        triggerVerificationRequired({ tab_id: targetTabId, url: targetUrl, status: fetchResult.status });
      }
      return fetchResult;
    }

    case "job.cancel": {
      const jobId = params.jobId;
      if (activeJobs.has(jobId)) {
        activeJobs.delete(jobId);
        return { cancelled: true, jobId };
      }
      return { cancelled: false, message: "Job not running" };
    }

    default:
      throw new Error(`Action not implemented: ${action}`);
  }
}

async function getActiveTabId() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab && tab.id) return tab.id;
  const [anyTab] = await chrome.tabs.query({});
  if (anyTab && anyTab.id) return anyTab.id;
  throw new Error("No available browser tab found");
}

// ── Envelope Dispatch & Command Execution ───────────────────────────────────
async function dispatchEnvelope(envelope) {
  if (!envelope || typeof envelope !== "object") return;

  if (envelope.type === "ingest.reply") {
    const pending = pendingIngestRequests.get(envelope.id);
    if (pending) {
      clearInterval(pending.timer);
      pendingIngestRequests.delete(envelope.id);
      if (envelope.ok) pending.resolve(envelope.result);
      else pending.reject(new Error(envelope.error || "Ingest failed"));
    }
    return;
  }

  // Handle command envelopes
  if (envelope.type === "command") {
    const cmdId = envelope.id;
    const action = envelope.action;
    const params = envelope.params || {};
    const deadlineMs = Math.max(1000, Number(envelope.deadlineMs) || 30000);

    const abortController = new AbortController();
    const timeoutId = setTimeout(() => {
      if (inFlightCommands.has(cmdId)) {
        inFlightCommands.delete(cmdId);
        abortController.abort(new Error("TIMEOUT"));
        bridgeSend({
          v: 1,
          type: "error",
          id: cmdId,
          action,
          error: {
            code: "TIMEOUT",
            message: `Action '${action}' timed out after ${deadlineMs}ms in extension`,
            retryable: true,
            details: {},
          },
        });
      }
    }, deadlineMs);

    inFlightCommands.set(cmdId, { abortController, timeoutId, action, startedAt: Date.now() });

    // 1. Send ACCEPTED acknowledgement
    bridgeSend({
      v: 1,
      type: "accepted",
      id: cmdId,
      params: { action, acceptedAt: Date.now() },
    });

    try {
      const result = await handleAction(action, params);
      if (!inFlightCommands.has(cmdId)) {
        return; // Timed out or cancelled
      }
      clearTimeout(timeoutId);
      inFlightCommands.delete(cmdId);

      bridgeSend({
        v: 1,
        type: "result",
        id: cmdId,
        action,
        result,
      });
    } catch (err) {
      if (!inFlightCommands.has(cmdId)) {
        return; // Timed out or cancelled
      }
      clearTimeout(timeoutId);
      inFlightCommands.delete(cmdId);

      console.error(`[bridge] Action '${action}' error:`, err.message);
      let errCode = "INTERNAL_ERROR";
      if (
        err.message.includes("Cross-origin") ||
        err.message.includes("Tab protocol") ||
        err.message.includes("Target protocol") ||
        err.message.includes("userinfo")
      ) {
        errCode = "SAME_ORIGIN_VIOLATION";
      } else if (err.message.includes("ELEMENT_NOT_FOUND")) {
        errCode = "ELEMENT_NOT_FOUND";
      } else if (err.message.includes("UNSUPPORTED_ACTION_PARAM")) {
        errCode = "UNSUPPORTED_ACTION_PARAM";
      } else if (err.message.includes("Timeout waiting")) {
        errCode = "TIMEOUT";
      }

      bridgeSend({
        v: 1,
        type: "error",
        id: cmdId,
        action,
        error: {
          code: errCode,
          message: err.message,
          retryable: errCode === "TIMEOUT",
          details: {},
        },
      });
    }
    return;
  }

  // Handle cancel
  if (envelope.type === "cancel") {
    const targetId = envelope.params && envelope.params.targetCommandId;
    let cancelled = false;
    if (targetId && inFlightCommands.has(targetId)) {
      const entry = inFlightCommands.get(targetId);
      clearTimeout(entry.timeoutId);
      entry.abortController.abort(new Error("CANCELLED"));
      inFlightCommands.delete(targetId);
      cancelled = true;

      bridgeSend({
        v: 1,
        type: "error",
        id: targetId,
        action: entry.action,
        error: {
          code: "CANCELLED",
          message: "Command was cancelled by gateway",
          retryable: false,
          details: {},
        },
      });
    }
    if (targetId && activeJobs.has(targetId)) {
      activeJobs.delete(targetId);
      cancelled = true;
    }
    bridgeSend({
      v: 1,
      type: "result",
      id: envelope.id || ("cancel-" + Date.now()),
      result: { cancelled, targetCommandId: targetId },
    });
    return;
  }

  // Handle verification resume/resolved
  if (envelope.type === "verification.resolved" || envelope.type === "verification.resume") {
    resumeVerification(envelope.params && (envelope.params.jobId || envelope.params.id));
    return;
  }

  // Handle ping
  if (envelope.type === "ping" || envelope.type === "bridge.ping") {
    bridgeSend({
      v: 1,
      type: "pong",
      id: envelope.id || "pong-" + Date.now(),
      params: { at: Date.now() },
    });
    return;
  }

  // Backward-compatible ingest.job message
  if (envelope.type === "ingest.job") {
    enqueueLegacyJob(envelope.job, "websocket");
    return;
  }
}

// ── WebSocket Bridge Connection ─────────────────────────────────────────────
function bridgeSend(message) {
  if (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN) return false;
  bridgeSocket.send(JSON.stringify(message));
  return true;
}

function clearReconnect() {
  if (bridgeReconnectTimeout) clearTimeout(bridgeReconnectTimeout);
  bridgeReconnectTimeout = null;
}

function closeBridgeSocket() {
  if (bridgeHandshakeTimeout) clearTimeout(bridgeHandshakeTimeout);
  bridgeHandshakeTimeout = null;
  if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
  bridgeHeartbeat = null;
  if (bridgeSocket) {
    bridgeSocket.onopen = bridgeSocket.onmessage = bridgeSocket.onerror = bridgeSocket.onclose = null;
    try { bridgeSocket.close(); } catch {}
  }
  bridgeSocket = null;
  updateState("disconnected");
}

function connectionError(message) {
  chrome.storage.local.set({ lastConnectionError: message });
}

function scheduleBridgeReconnect() {
  if (!connectionEnabled || bridgeReconnectTimeout) return;
  reconnectAttempts++;
  const delay = Math.min(30000, 1000 * Math.pow(1.5, Math.min(reconnectAttempts, 8))) + Math.floor(Math.random() * 1000);
  bridgeReconnectTimeout = setTimeout(() => {
    bridgeReconnectTimeout = null;
    connectBridge();
  }, delay);
}

async function connectBridge() {
  if (!connectionEnabled) return;
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) return;
  if (bridgeConnecting) return bridgeConnecting;
  const generation = connectionGeneration;
  const current = () => generation === connectionGeneration && connectionEnabled;
  const attempt = (async () => {
    try {
      const stored = await chrome.storage.local.get(["gatewayUrl", "pairingToken", "connectionEnabled"]);
      if (!current()) return;
      if (stored.connectionEnabled === false) {
        connectionEnabled = false;
        closeBridgeSocket();
        return;
      }
      let token = stored.pairingToken;
      let wsUrl = stored.gatewayUrl || "ws://127.0.0.1:8766/browser/v1/ws";
      const target = new URL(wsUrl);
      if (!['ws:', 'wss:'].includes(target.protocol) ||
          !['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname) || target.username || target.password) {
        throw new Error("Gateway phải là địa chỉ WebSocket localhost hợp lệ");
      }
      // Refresh the process-local token on every reconnect, including custom helper ports.
      const pairUrl = new URL(target.toString());
      pairUrl.protocol = target.protocol === 'wss:' ? 'https:' : 'http:';
      if (pairUrl.port === '8767') pairUrl.port = '8766';
      pairUrl.pathname = '/browser/pair';
      pairUrl.search = pairUrl.hash = '';
      const controller = new AbortController();
      pairingController = controller;
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetch(pairUrl.toString(), {
          cache: "no-store",
          headers: { "X-Bridge-Client": "ai-cowork-bridge" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Ghép nối bị từ chối (HTTP ${response.status})`);
        const pair = await response.json();
        if (typeof pair.token !== 'string' || !pair.token) throw new Error("Gateway không trả pairing token");
        token = pair.token;
      } finally {
        clearTimeout(timeout);
        if (pairingController === controller) pairingController = null;
      }
      if (!current()) return;
      await chrome.storage.local.set({ gatewayUrl: wsUrl, pairingToken: token });
      if (!current()) return;
      target.searchParams.set('token', token);
      const socket = new WebSocket(target.toString());
      bridgeSocket = socket;
      const ownsSocket = () => current() && bridgeSocket === socket;
      const fail = message => {
        if (!ownsSocket()) return;
        connectionError(message);
        closeBridgeSocket();
        scheduleBridgeReconnect();
      };
      bridgeHandshakeTimeout = setTimeout(() => fail("Gateway không hoàn tất kết nối trong 10 giây"), 10000);
      socket.onopen = () => {
        if (!ownsSocket()) return;
        clearTimeout(bridgeHandshakeTimeout);
        bridgeHandshakeTimeout = null;
        clearReconnect();
        reconnectAttempts = 0;
        lastBridgeMessageAt = Date.now();
        connectionError("");
        updateState(verificationInfo ? "awaiting_user_verification" : activeJobs.size ? "busy" : "connected", verificationInfo);
        for (const pending of pendingIngestRequests.values()) bridgeSend(pending.message);
        if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
        bridgeHeartbeat = setInterval(() => {
          if (!ownsSocket()) return;
          if (Date.now() - lastBridgeMessageAt > 45000) {
            fail("Gateway không phản hồi heartbeat; đang kết nối lại");
            return;
          }
          bridgeSend({ v: 1, type: "ping", id: "ping-" + Date.now(), params: { at: Date.now() } });
        }, 20000);
      };
      socket.onmessage = event => {
        if (!ownsSocket()) return;
        lastBridgeMessageAt = Date.now();
        let envelope;
        try { envelope = JSON.parse(event.data); } catch { return; }
        Promise.resolve(dispatchEnvelope(envelope)).catch(error => console.warn("[bridge] dispatch failed:", error.message));
      };
      socket.onerror = () => fail("Không kết nối được WebSocket. Kiểm tra launcher, URL và quyền extension");
      socket.onclose = () => fail("Kết nối gateway đã đóng; đang kết nối lại");
    } catch (error) {
      if (!current()) return;
      connectionError(error.name === 'AbortError' ? "Gateway không phản hồi ghép nối trong 8 giây" : `Không kết nối được gateway: ${error.message}`);
      closeBridgeSocket();
      scheduleBridgeReconnect();
    }
  })();
  bridgeConnecting = attempt;
  try { await attempt; } finally {
    if (bridgeConnecting === attempt) bridgeConnecting = null;
  }
}

async function configureConnection(message) {
  connectionGeneration++;
  connectionEnabled = false;
  clearReconnect();
  if (pairingController) pairingController.abort();
  closeBridgeSocket();
  bridgeConnecting = null;
  const enabled = message.action === "connect";
  const config = { connectionEnabled: enabled };
  if (enabled) {
    if (typeof message.url === "string") config.gatewayUrl = message.url.trim();
    if (typeof message.token === "string") config.pairingToken = message.token.trim();
  }
  const generation = connectionGeneration;
  await chrome.storage.local.set(config);
  if (generation !== connectionGeneration) return;
  connectionEnabled = enabled;
  if (enabled) await connectBridge();
}

// ── WebSocket Job Delivery ─────────────────────────────────────────────────
function enqueueLegacyJob(job) {
  if (!job || !job.id) return;
  if (activeJobs.has(job.id)) {
    console.log("[bridge] Job already active, skipping duplicate enqueue:", job.id);
    return;
  }
  bridgeSend({ v: 1, type: "accepted", id: job.id, jobId: job.id });
  if (job.retry) {
    knownJobs.delete(job.id);
    notifiedVerificationJobIds.delete(job.id);
  }
  if (knownJobs.has(job.id)) return;
  knownJobs.add(job.id);
  jobChain = jobChain.then(() => runJob(job)).catch((err) => console.error("[bridge] Job failed:", err))
    .finally(() => {
      // Bound completed-job deduplication without evicting queued jobs.
      if (knownJobs.size > 1000) knownJobs.delete(job.id);
    });
}

// ── Top-Level Listeners (MV3 Requirement) ───────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("bridge_heartbeat_alarm", { periodInMinutes: 1 });
  connectBridge();
});

chrome.runtime.onStartup.addListener(connectBridge);
chrome.alarms.onAlarm.addListener(connectBridge);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "connect" || msg.action === "disconnect") {
    configureConnection(msg).then(() => sendResponse({ ok: true }), error => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (msg.action === "resumeVerification") {
    resumeVerification(msg.jobId);
    sendResponse({ ok: true });
  }
  return false;
});

// Recreate the watchdog alarm when an MV3 worker starts after browser restart.
chrome.alarms.create("bridge_heartbeat_alarm", { periodInMinutes: 1 });
connectBridge();

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    idsFrom,
    extractShopName,
    resolveShopId,
    normaliseRating,
    ratingTotal,
    crawlPercent,
    classifyShopeeFailure,
    formatShopeeFailure,
    runJob,
    resumeVerification,
    triggerVerificationRequired,
    enqueueLegacyJob,
    knownJobs,
    pendingVerificationJobs,
    notifiedVerificationJobIds,
  };
}
