// AI Cowork — Manifest V3 Chrome Extension Browser Bridge (v2.2.0)
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
// Match the Shopee web UI request size. Large batches (for example 50) make
// an otherwise valid in-page session get redirected to /verify/traffic.
const PAGE_SIZE = 6;
const MAX_REVIEWS = 20000;
const PACE_MS = 1200;
const RATING_TYPES = [0, 5, 4, 3, 2, 1];

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
let connectionConflict = null;
let lastCompletedResult = null;

function setBadge(text, color = "#1a73e8") {
  chrome.action.setBadgeText({ text: String(text || "") });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

function updateState(newState, details = null) {
  extensionState = newState;
  if (newState !== "client_conflict") connectionConflict = null;
  if (newState === "awaiting_user_verification") {
    verificationInfo = details;
    setBadge("PAUS", "#f9ab00");
  } else if (newState === "login_required") {
    verificationInfo = details;
    setBadge("LOGN", "#ea8600");
  } else if (newState === "api_blocked") {
    verificationInfo = details;
    setBadge("BLCK", "#d93025");
  } else if (newState === "connected") {
    if (!verificationInfo) {
      setBadge("OK", "#137333");
    } else {
      setBadge("PAUS", "#f9ab00");
    }
  } else if (newState === "busy") {
    // A newly accepted job supersedes terminal attention from an older job.
    // Keeping this object made the popup show a stale Blocked/Login card while
    // the current crawl was already progressing successfully.
    verificationInfo = null;
    setBadge("BUSY", "#1a73e8");
  } else if (newState === "client_conflict") {
    connectionConflict = details || connectionConflict;
    setBadge("LOCK", "#ea8600");
  } else {
    // disconnected
    if (!verificationInfo) {
      setBadge("");
    }
  }
  chrome.storage.local.set({
    extensionState: verificationInfo ? "awaiting_user_verification" : extensionState,
    verificationInfo,
    connectionConflict,
    currentJob: activeJobs.size > 0 ? Array.from(activeJobs.values())[0] : null,
  });
}

function stateForVerification(details) {
  if (details?.kind === "login") return "login_required";
  if (details?.kind === "api_blocked") return "api_blocked";
  return "awaiting_user_verification";
}

// ── Ingest & Progress Helpers ────────────────────────────────────────────────
const pendingIngestRequests = new Map();
const knownJobs = new Set();

function sendIngestRequest(params, { timeoutMs = 120000 } = {}) {
  const id = crypto.randomUUID();
  const message = { v: 1, type: "ingest.rpc", id, params };
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const retry = () => {
      if (Date.now() - started > timeoutMs) {
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

function clearPendingIngestRequests(reason = "WebSocket disconnected") {
  for (const [id, req] of pendingIngestRequests.entries()) {
    clearInterval(req.timer);
    pendingIngestRequests.delete(id);
    try { req.reject(new Error(reason)); } catch {}
  }
}

async function sendCheckpoint(params) {
  return sendIngestRequest({
    operation: "checkpoint",
    ...params,
  });
}

async function reportProgress(job, progress) {
  if (job && job.id) {
    // Keep the latest lightweight snapshot in the active job so the popup can
    // render live progress without polling the helper or duplicating requests.
    job._progress = { ...progress, updatedAt: Date.now() };
    await sendIngestRequest({ operation: "progress", job: job.id, progress });
  }
}

function sanitizeUrlForTrace(url) {
  if (!url || typeof url !== "string") return null;
  try {
    let clean = url.split("#")[0];
    clean = clean.replace(/([?&][^=]*(?:token|auth|session|cookie|sig|signature|sp_atk|password|key|passkey)[^=]*=)[^&#]+/gi, "$1[REDACTED]");
    return clean;
  } catch {
    return null;
  }
}

function sanitizeErrorForTrace(err) {
  if (!err) return null;
  const raw = typeof err === "object" ? (err.message || String(err)) : String(err);
  return raw
    .replace(/<[^>]*>/g, " ")
    .replace(/([^=&\s]*(?:token|cookie|auth|session)[^=&\s]*)=[^\s,;&]+/gi, "$1=[REDACTED]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 400);
}

function buildTraceEntry(job, event, details = {}) {
  const itemid = details.itemid ?? job?.itemid ?? (job?.url ? idsFrom(job.url).itemid : null) ?? null;
  const shopid = details.shopid ?? job?._targetShopId ?? job?.shopid ?? (job?.url ? idsFrom(job.url).shopid : null) ?? null;
  const tabId = details.tabId ?? job?._targetTabId ?? null;
  const tabUrl = sanitizeUrlForTrace(details.tabUrl ?? job?._targetTabUrl ?? job?.url ?? null);

  const reqStart = details.requestStart
    ? (typeof details.requestStart === "number" ? new Date(details.requestStart).toISOString() : String(details.requestStart))
    : null;
  const reqEnd = details.requestEnd
    ? (typeof details.requestEnd === "number" ? new Date(details.requestEnd).toISOString() : String(details.requestEnd))
    : null;

  const httpStatus = typeof details.httpStatus === "number"
    ? details.httpStatus
    : (typeof details.status === "number" ? details.status : null);

  const isLogin = typeof details.isLogin === "boolean"
    ? details.isLogin
    : (typeof details.is_login === "boolean" ? details.is_login : null);

  const entry = {
    jobId: job?.id || details.jobId || null,
    itemid: itemid !== null && itemid !== undefined ? String(itemid) : null,
    shopid: shopid !== null && shopid !== undefined ? String(shopid) : null,
    tabId: typeof tabId === "number" ? tabId : null,
    tabUrl,
    event: String(event),
    offset: typeof details.offset === "number" ? details.offset : null,
    limit: typeof details.limit === "number" ? details.limit : null,
    requestStart: reqStart,
    requestEnd: reqEnd,
    httpStatus,
    responseUrl: sanitizeUrlForTrace(details.responseUrl ?? null),
    elapsedMs: typeof details.elapsedMs === "number" ? details.elapsedMs : null,
    error: details.error ? sanitizeErrorForTrace(details.error) : null,
    is_login: isLogin,
    at: new Date().toISOString(),
  };

  if (typeof details.rowsCount === "number") entry.rowsCount = details.rowsCount;
  if (typeof details.batchSize === "number") entry.batchSize = details.batchSize;
  if (typeof details.totalTarget === "number") entry.totalTarget = details.totalTarget;
  if (typeof details.ratingType === "number") entry.ratingType = details.ratingType;
  if (details.filter !== undefined && details.filter !== null) entry.filter = details.filter;
  if (typeof details.scope === "string") entry.scope = details.scope;
  if (typeof details.mappedScope === "string" || details.mappedScope === null) entry.mappedScope = details.mappedScope;
  if (typeof details.comment_ratio === "number") entry.comment_ratio = details.comment_ratio;
  if (typeof details.media_ratio === "number") entry.media_ratio = details.media_ratio;
  if (typeof details.reason === "string") entry.reason = details.reason;
  if (typeof details.count === "number") entry.count = details.count;

  return entry;
}

async function traceJob(job, progress, event, details = {}) {
  const entry = buildTraceEntry(job, event, details);
  // Keep this structured and omit review text, cookies, and media URLs.
  console.log("[bridge][job-trace]", JSON.stringify(entry));
  if (progress && entry.jobId) {
    try {
      await progress({ stage: "trace", trace: entry, message: `[${event}]` });
    } catch (error) {
      console.warn("[bridge][job-trace] progress upload failed:", error?.message || error);
    }
  }
  return entry;
}

async function uploadIngestResult(body) {
  // Review batches are already durably checkpointed on the helper. Finalize
  // them server-side so a large result never has to cross WebSocket again.
  if (body && body.job && Array.isArray(body.rows)) {
    return sendIngestRequest({
      operation: "finalize",
      job: body.job,
      name: body.name,
      source: body.source,
      itemid: body.itemid,
      max_zip_mb: body.max_zip_mb,
      crawl_summary: body.crawl_summary,
      partial: body.partial,
    }, { timeoutMs: 30 * 60 * 1000 });
  }

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

async function rememberCompletedResult(job, savedResult) {
  if (!savedResult || savedResult.ok !== true) return null;
  await cleanupPendingJobState(job?.id, { clearVerification: true });
  const result = {
    jobId: job?.id || null,
    source: job?.url || null,
    completedAt: Date.now(),
    count: Number(savedResult.count) || 0,
    partial: savedResult.partial === true,
    crawl_summary: savedResult.crawl_summary || null,
    csv: savedResult.csv || null,
    media_dir: savedResult.media_dir || null,
    manifest: savedResult.manifest || null,
    zip: savedResult.zip || null,
    zip_url: savedResult.zip_url || null,
    zip_parts: Array.isArray(savedResult.zip_parts) ? savedResult.zip_parts : [],
    zip_urls: Array.isArray(savedResult.zip_urls) ? savedResult.zip_urls : [],
    report: savedResult.report || null,
    report_url: savedResult.report_url || null,
  };
  lastCompletedResult = result;
  await chrome.storage.local.set({ lastCompletedResult: result });
  return result;
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
  const rawId = x.cmid != null ? x.cmid : (x.id != null ? x.id : (x.rating_id != null ? x.rating_id : ""));
  const reviewId = rawId !== "" ? String(rawId).trim() : "";
  return {
    id: reviewId,
    cmid: x.cmid != null ? String(x.cmid).trim() : (reviewId || null),
    orderid: x.orderid != null ? String(x.orderid).trim() : null,
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

function extractRatingSummary(j) {
  const data = j && j.data;
  const summary = data && (data.item_rating_summary || data.product_rating_summary || data.rating_summary);
  const total = ratingTotal(j);
  return {
    total: total ?? null,
    rating_total: Number(summary?.rating_total || summary?.total_count || total || 0),
    rcount_with_context: Number(summary?.rcount_with_context || 0),
    rcount_with_media: Number(summary?.rcount_with_media || summary?.rcount_with_image || 0),
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

function scopeTarget(scopeKey, uiReference, total) {
  if (scopeKey === "comment") {
    const n = Number(uiReference?.rcount_with_context);
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  if (scopeKey === "media") {
    const n = Number(uiReference?.rcount_with_media);
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  return total || null;
}

function shouldAdvanceRatingType(scopeKey, scope, uiReference, total, typeIndex) {
  if (typeIndex + 1 >= RATING_TYPES.length) return false;
  const target = scopeTarget(scopeKey, uiReference, total);
  if (!target) return true;
  return (Number(scope?.rows_count) || 0) < target;
}

function normalizeShopeeUrl(url) {
  if (!url) return "";
  return String(url)
    .trim()
    .replace(/^(https?:\/\/)(?:www\.)?shoppe\.vn(\/.*)?$/i, "$1shopee.vn$2");
}

function isShopeeHostname(hostname) {
  if (!hostname || typeof hostname !== "string") return false;
  const h = hostname.toLowerCase();
  return h === "shopee.vn" || h.endsWith(".shopee.vn");
}

function isValidShopeeUrl(urlStr) {
  if (!urlStr || typeof urlStr !== "string") return false;
  try {
    const u = new URL(normalizeShopeeUrl(urlStr), "https://shopee.vn");
    return (u.protocol === "http:" || u.protocol === "https:") && isShopeeHostname(u.hostname);
  } catch {
    return false;
  }
}

function extractShopName(url) {
  try {
    const u = new URL(normalizeShopeeUrl(url), "https://shopee.vn");
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
    const normalizedUrl = normalizeShopeeUrl(url);
    const u = new URL(normalizedUrl, "https://shopee.vn");
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
  if (!isValidShopeeUrl(targetUrl)) {
    throw new Error(`Target URL không thuộc hostname shopee.vn: ${targetUrl}`);
  }
  const tabs = await chrome.tabs.query({});
  // 1. Prefer a tab already displaying this item on shopee.vn
  let tab = tabs.find((t) => t.url && isValidShopeeUrl(t.url) && itemid && t.url.includes(String(itemid)));
  // 2. Or create a new tab if none exists with this item
  if (!tab) {
    try {
      tab = await chrome.tabs.create({ url: targetUrl, active: false });
    } catch (err) {
      // An MV3 service worker has no "current window". This happens after Chrome
      // restores only background processes: the socket is connected, but tabs.create
      // rejects until a real browser window exists. Create one and continue the same job.
      const noWindow = /no current window|window/i.test(String(err?.message || err));
      if (!noWindow || !chrome.windows?.create) throw err;
      const win = await chrome.windows.create({ url: targetUrl, focused: false });
      tab = win?.tabs?.[0] || null;
      if (!tab && win?.id) {
        const windowTabs = await chrome.tabs.query({ windowId: win.id });
        tab = windowTabs[0] || null;
      }
    }
    if (tab && tab.status !== "complete") await new Promise((resolve) => {
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

  if (!tab || !tab.id || !tab.url) {
    throw new Error("Không thể tìm hoặc mở tab Shopee trên trình duyệt");
  }

  try {
    const tabHost = new URL(tab.url).hostname;
    if (!isShopeeHostname(tabHost)) {
      throw new Error(`Tab không thuộc hostname shopee.vn (hostname: ${tabHost})`);
    }
  } catch (err) {
    throw new Error(`Tab URL không hợp lệ: ${tab.url} (${err.message})`);
  }

  const tabIds = idsFrom(tab.url);
  if (tabIds.itemid && String(tabIds.itemid) !== String(itemid)) {
    throw new Error(`Tab URL trỏ đến itemid khác (${tabIds.itemid}) so với itemid cần crawl (${itemid})`);
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

  // 2. Scrape from DOM / canonical / same-origin in tab context (chỉ trong tab, KHÔNG dùng background fetch)
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

  // TUYỆT ĐỐI KHÔNG dùng background fetch thay cho request trong tab (đã gỡ bỏ fallback background fetch)
  return null;
}

const SCRIPT_RETRY_DELAYS_MS = [250, 750, 1500];

async function executeScriptResultWithRetry(options) {
  let lastError = "executeScript returned no result";
  for (let attempt = 0; attempt <= SCRIPT_RETRY_DELAYS_MS.length; attempt++) {
    try {
      let timer = null;
      const timeoutPromise = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("executeScript timeout")), 20000);
      });
      const results = await Promise.race([
        chrome.scripting.executeScript(options),
        timeoutPromise,
      ]).finally(() => {
        if (timer) clearTimeout(timer);
      });
      if (results?.length && results[0] && results[0].result !== undefined && results[0].result !== null) {
        return results[0].result;
      }
      lastError = "executeScript returned no result";
    } catch (error) {
      lastError = error?.message || String(error);
    }
    if (attempt < SCRIPT_RETRY_DELAYS_MS.length) {
      await new Promise(resolve => setTimeout(resolve, SCRIPT_RETRY_DELAYS_MS[attempt]));
    }
  }
  return {
    ok: false,
    error: `${lastError} after ${SCRIPT_RETRY_DELAYS_MS.length + 1} attempts`,
    retryableTabError: true,
  };
}

async function fetchRatingsFromTab(tabId, itemid, shopid, offset, limit, referer, ratingType = 0, filter = 0) {
  return executeScriptResultWithRetry({
    target: { tabId },
    // Run as the Shopee page itself. The default ISOLATED world gives the
    // request an extension/content-script initiator that Shopee's WAF rejects
    // even when the tab has a valid logged-in cookie.
    world: "MAIN",
    func: async (iid, sid, off, lim, ref, type, filt) => {
      const path = `/api/v2/item/get_ratings?filter=${filt ?? 0}&flag=1&itemid=${iid}&limit=${lim}&offset=${off}&shopid=${sid}&type=${type ?? 0}`;
      const controller = new AbortController();
      const tid = setTimeout(() => controller.abort(new Error("TIMEOUT")), 15000);
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
          signal: controller.signal,
        });
        clearTimeout(tid);
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
        clearTimeout(tid);
        return { ok: false, error: err.message, isTimeout: err.name === "AbortError" || err.message === "TIMEOUT" };
      }
    },
    args: [itemid, shopid, offset, limit, referer, ratingType, filter],
  });
}

function reviewFingerprint(review) {
  if (!review || typeof review !== "object") return "";
  if (review.id) {
    return `id:${review.id}`;
  }
  return [
    review.user || "",
    review.thoi_gian || "",
    review.sao || "",
    review.noi_dung || "",
    review.phan_loai || "",
    review.anh_urls || "",
    review.video_urls || "",
  ].join("\u001f");
}

function mergeReview(existing, incoming) {
  if (!existing) return incoming;
  if ((incoming.anh_urls && !existing.anh_urls) || (incoming.so_anh || 0) > (existing.so_anh || 0)) {
    existing.anh = incoming.anh;
    existing.so_anh = incoming.so_anh;
    existing.anh_urls = incoming.anh_urls;
  }
  if ((incoming.video_urls && !existing.video_urls) || (incoming.so_video || 0) > (existing.so_video || 0)) {
    existing.video = incoming.video;
    existing.so_video = incoming.so_video;
    existing.video_urls = incoming.video_urls;
  }
  if (incoming.media_urls && (!existing.media_urls || incoming.media_urls.length > existing.media_urls.length)) {
    existing.media_urls = incoming.media_urls;
  }
  if ((incoming.noi_dung && !existing.noi_dung) || (incoming.noi_dung || "").length > (existing.noi_dung || "").length) {
    existing.noi_dung = incoming.noi_dung;
  }
  if (incoming.phan_loai && !existing.phan_loai) {
    existing.phan_loai = incoming.phan_loai;
  }
  if (incoming.cmid && !existing.cmid) existing.cmid = incoming.cmid;
  if (incoming.orderid && !existing.orderid) existing.orderid = incoming.orderid;
  return existing;
}

function shopeeLoginState(fetchRes) {
  const json = fetchRes?.json || {};
  const data = json?.data || {};
  const url = String(fetchRes?.url || "").toLowerCase();
  const sample = String(fetchRes?.textSample || fetchRes?.error || "").toLowerCase();

  if (json?.is_login === true || data?.is_login === true || sample.includes('"is_login":true') || sample.includes('"is_login": true')) return true;
  if (json?.is_login === false || data?.is_login === false) return false;
  if (json?.is_login === true || data?.is_login === true) return true;
  if (fetchRes?.status === 401 || url.includes("/login")) return false;
  if (/\b(?:is_login|login_required)\b["']?\s*[:=]\s*false\b/.test(sample)) return false;
  return null;
}

function isShopeeChallenge(url = "", pageUrl = "", sample = "") {
  const combined = `${url} ${pageUrl} ${sample}`.toLowerCase();
  return (
    combined.includes("/verify/") ||
    combined.includes("anti_bot_tracking_id") ||
    combined.includes("captcha") ||
    combined.includes("challenge") ||
    combined.includes("xác nhận để tiếp tục") ||
    combined.includes("hoàn thiện bức hình") ||
    combined.includes("kéo thanh trượt") ||
    combined.includes("trượt để hoàn thành")
  );
}

function classifyShopeeFailure(fetchRes) {
  const json = fetchRes?.json || {};
  const url = String(fetchRes?.url || "").toLowerCase();
  const pageUrl = String(fetchRes?.pageUrl || "").toLowerCase();
  const sample = String(fetchRes?.textSample || fetchRes?.error || "").toLowerCase();
  const loginState = shopeeLoginState(fetchRes);
  if (loginState === false) return "login";

  if (json?.error === 90309999 && json?.redirect_to_error_page === true) return "verification";
  if (isShopeeChallenge(url, pageUrl, sample)) return "verification";
  if (loginState === true) return "api_blocked";

  if (fetchRes?.status === 403 || json?.error === 90309999 || sample.includes("90309999") ||
      sample.includes("403") || sample.includes("access denied") || sample.includes("api_blocked")) {
    return "api_blocked";
  }
  return "other";
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
    return `Shopee verification required (HTTP ${status}) — tab=${page}; response=${endpoint}`;
  }
  return `Shopee reviews API access denied (HTTP ${status}) — không thấy CAPTCHA trên tab; endpoint=${endpoint}; tab=${page}; response=${sample || "empty"}`;
}

function extractVerificationUrl(input) {
  if (!input) return "";
  const isTarget = (str) => typeof str === "string" && (str.includes("/verify/") || str.includes("anti_bot_tracking_id"));
  const urlPattern = /https?:\/\/[^\s"'`<>]+(?:verify\/(?:traffic|captcha|slider|[a-zA-Z0-9_-]+)|anti_bot_tracking_id)[^\s"'`<>;,)]*/;
  if (typeof input === "object") {
    if (isTarget(input.verification_url)) return input.verification_url.replace(/[;,.)]+$/, "");
    if (isTarget(input.target_url)) return input.target_url.replace(/[;,.)]+$/, "");
    if (isTarget(input.pageUrl)) return input.pageUrl.replace(/[;,.)]+$/, "");
    if (isTarget(input.url)) return input.url.replace(/[;,.)]+$/, "");
    if (isTarget(input.responseUrl)) return input.responseUrl.replace(/[;,.)]+$/, "");
    const json = input.json || {};
    if (json?.redirect_to_error_page === true && typeof json.tracking_id === "string" && json.tracking_id) {
      let origin = "https://shopee.vn";
      try {
        const base = new URL(input.pageUrl || input.url || origin);
        if (isShopeeHostname(base.hostname)) origin = base.origin;
      } catch {}
      return `${origin}/verify/traffic?anti_bot_tracking_id=${encodeURIComponent(json.tracking_id)}`;
    }
    const str = `${input.reason || ""} ${input.error || ""} ${input.message || ""} ${input.textSample || ""}`;
    const match = str.match(urlPattern);
    if (match) return match[0].replace(/[;,.)]+$/, "");
  } else if (typeof input === "string") {
    const match = input.match(urlPattern);
    if (match) return match[0].replace(/[;,.)]+$/, "");
  }
  return "";
}

async function preflightRatingsInTab(tabId, itemid, shopid, referer) {
  return executeScriptResultWithRetry({
    target: { tabId },
    world: "MAIN",
    func: async (iid, sid, ref) => {
      const path = `/api/v2/item/get_ratings?filter=0&flag=1&itemid=${iid}&limit=1&offset=0&shopid=${sid}&type=0`;
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
        return { ok: false, error: err.message, pageUrl: location.href };
      }
    },
    args: [itemid, shopid, referer],
  });
}

function evaluatePreflightResult(fetchRes) {
  const status = typeof fetchRes?.status === "number" ? fetchRes.status : (fetchRes?.ok ? 200 : null);
  const json = fetchRes?.json || {};
  const explicitLoginState = shopeeLoginState(fetchRes);
  const isLoginRequired = explicitLoginState === false;
  const failureKind = classifyShopeeFailure(fetchRes);
  const isVerificationRequired = failureKind === "verification";
  const isLogin = !isVerificationRequired && (explicitLoginState === true || Boolean(fetchRes?.ok && !isLoginRequired));
  let error = null;
  if (isLoginRequired) {
    error = `Shopee login required (HTTP ${status ?? "unknown"}, error=${json?.error ?? "unknown"}, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome`;
  } else if (isVerificationRequired) {
    error = formatShopeeFailure(fetchRes);
  } else if (!fetchRes?.ok) {
    error = fetchRes?.error ? `Shopee API request failed: ${fetchRes.error}` : formatShopeeFailure(fetchRes);
  }

  return {
    status,
    isLogin,
    error,
    responseUrl: fetchRes?.url || null,
  };
}

function isUsableRatingsPreflight(fetchRes) {
  if (!fetchRes?.ok || fetchRes.status !== 200) return false;
  const sample = String(fetchRes.textSample || fetchRes.error || "").toLowerCase();
  if (sample.includes("access denied") || sample.includes("/verify/traffic") || sample.includes("captcha") || sample.includes("challenge")) {
    return false;
  }
  const json = fetchRes.json;
  if (!json || typeof json !== "object" || json.error) return false;
  const data = json.data;
  const candidateArrays = [
    data?.ratings,
    data?.items,
    json.ratings,
    json.items,
  ];
  if (candidateArrays.some((value) => Array.isArray(value))) return true;
  const total = Number(data?.item_rating_summary?.rating_total ?? data?.total ?? json.total);
  return Number.isFinite(total) && total >= 0 && data !== null && typeof data === "object" && Object.keys(data).length > 0;
}

const CANDIDATE_FILTERS = [1, 2, 3, 4, 5];
const PROBE_LIMIT = 6;

async function probeFilterCandidates(tabId, itemid, shopid, referer, job, progress) {
  const probeStats = [];
  const validCandidates = [];

  for (const candidateFilter of CANDIDATE_FILTERS) {
    const probeStart = Date.now();
    let probeRes;
    try {
      probeRes = await fetchRatingsFromTab(tabId, itemid, shopid, 0, PROBE_LIMIT, referer, 0, candidateFilter);
    } catch (err) {
      probeRes = { ok: false, error: err?.message || String(err), status: 500 };
    }
    const probeElapsed = Date.now() - probeStart;
    const httpStatus = typeof probeRes?.status === "number" ? probeRes.status : (probeRes?.ok ? 200 : 500);

    if (!probeRes?.ok) {
      probeStats.push({
        filter: candidateFilter,
        httpStatus,
        count: 0,
        comment_ratio: 0,
        media_ratio: 0,
        mappedScope: null,
        reason: probeRes?.error || `HTTP ${httpStatus}`,
        elapsedMs: probeElapsed,
      });
      continue;
    }

    const batch = (probeRes.json && (probeRes.json.data?.ratings || probeRes.json.ratings || probeRes.json.items)) || [];
    if (!Array.isArray(batch) || batch.length === 0) {
      probeStats.push({
        filter: candidateFilter,
        httpStatus,
        count: 0,
        comment_ratio: 0,
        media_ratio: 0,
        mappedScope: null,
        reason: "Empty ratings batch returned by API",
        elapsedMs: probeElapsed,
      });
      continue;
    }

    let commentCount = 0;
    let mediaCount = 0;
    for (const r of batch) {
      const hasComment = typeof r.comment === "string" && r.comment.trim().length > 0;
      if (hasComment) commentCount++;
      const hasImages = Array.isArray(r.images) && r.images.length > 0;
      const hasVideos = (Array.isArray(r.videos) && r.videos.length > 0) || (r.video && typeof r.video === "object");
      if (hasImages || hasVideos) mediaCount++;
    }

    const commentRatio = Math.round((commentCount / batch.length) * 100) / 100;
    const mediaRatio = Math.round((mediaCount / batch.length) * 100) / 100;

    validCandidates.push({
      filter: candidateFilter,
      httpStatus,
      count: batch.length,
      comment_ratio: commentRatio,
      media_ratio: mediaRatio,
      elapsedMs: probeElapsed,
    });
  }

  // Media filter: highest media_ratio >= 0.7
  let mediaFilter = null;
  let mediaStat = null;
  const mediaCandidates = validCandidates.filter((c) => c.media_ratio >= 0.7);
  if (mediaCandidates.length > 0) {
    mediaCandidates.sort((a, b) => b.media_ratio - a.media_ratio || b.count - a.count);
    mediaFilter = mediaCandidates[0].filter;
    mediaStat = mediaCandidates[0];
  }

  // Comment filter: highest comment_ratio >= 0.7 among remaining
  let commentFilter = null;
  let commentStat = null;
  const commentCandidates = validCandidates.filter((c) => c.filter !== mediaFilter && c.comment_ratio >= 0.7);
  if (commentCandidates.length > 0) {
    commentCandidates.sort((a, b) => b.comment_ratio - a.comment_ratio || b.count - a.count);
    commentFilter = commentCandidates[0].filter;
    commentStat = commentCandidates[0];
  }

  for (const c of validCandidates) {
    let mappedScope = null;
    let reason = "Evaluated";
    if (c.filter === mediaFilter) {
      mappedScope = "media";
      reason = `Matched scope 'media' (media_ratio=${c.media_ratio})`;
    } else if (c.filter === commentFilter) {
      mappedScope = "comment";
      reason = `Matched scope 'comment' (comment_ratio=${c.comment_ratio})`;
    } else {
      reason = `Not selected: comment_ratio=${c.comment_ratio}, media_ratio=${c.media_ratio}`;
    }
    probeStats.push({
      ...c,
      mappedScope,
      reason,
    });
  }

  for (const stat of probeStats) {
    await traceJob(job, progress, "filter-probe", {
      itemid,
      shopid,
      ...stat,
    });
  }

  return {
    commentFilter,
    mediaFilter,
    commentStat,
    mediaStat,
    probeStats,
  };
}

function normalizeCheckpointV2(chk, defaultItemid, defaultShopid) {
  if (!chk || typeof chk !== "object") return null;
  if (chk.version === 2 && chk.scopes) {
    return {
      version: 2,
      active_scope: chk.active_scope || "all",
      scopes: chk.scopes,
      itemid: chk.itemid || defaultItemid,
      shopid: chk.shopid || defaultShopid,
      total: chk.total ?? null,
      ui_reference: chk.ui_reference || null,
      updated_at: chk.updated_at || Date.now(),
      next_offset: chk.next_offset || 0,
      rows_count: chk.rows_count || 0,
      rating_type: chk.rating_type || 0,
    };
  }

  // V1 migration
  const v1Offset = typeof chk.next_offset === "number" ? chk.next_offset : (chk.offset || 0);
  const v1Rows = typeof chk.rows_count === "number" ? chk.rows_count : v1Offset;
  const v1RatingType = Number.isInteger(chk.rating_type) ? chk.rating_type : 0;
  return {
    version: 2,
    active_scope: "all",
    scopes: {
      all: {
        key: "all",
        label: "Tất cả",
        filter: 0,
        rating_type: v1RatingType,
        next_offset: v1Offset,
        rows_count: v1Rows,
        completed: false,
        discovered: true,
      },
      comment: {
        key: "comment",
        label: "Có bình luận",
        filter: null,
        rating_type: 0,
        next_offset: 0,
        rows_count: 0,
        completed: false,
        discovered: false,
      },
      media: {
        key: "media",
        label: "Có hình ảnh / Video",
        filter: null,
        rating_type: 0,
        next_offset: 0,
        rows_count: 0,
        completed: false,
        discovered: false,
      },
    },
    itemid: chk.itemid || defaultItemid,
    shopid: chk.shopid || defaultShopid,
    total: chk.total ?? null,
    ui_reference: null,
    updated_at: chk.updated_at || Date.now(),
    next_offset: v1Offset,
    rows_count: v1Rows,
    rating_type: v1RatingType,
  };
}

async function extractShopeeReviews(job, progress) {
  job.url = normalizeShopeeUrl(job.url);
  if (!isValidShopeeUrl(job.url)) {
    throw new Error(`Job URL không thuộc hostname shopee.vn: ${job.url}`);
  }

  const parsed = idsFrom(job.url);
  let itemid = parsed.itemid;
  let shopid = parsed.shopid;
  if (!itemid) throw new Error(`Không đọc được itemid từ URL: ${job.url}`);
  job.itemid = itemid;

  // 1. Tìm hoặc mở tab Shopee đúng itemid
  let tab = await findOrOpenShopeeTab(job.url, itemid);
  if (!tab || !tab.id) throw new Error("Không tìm thấy hoặc không mở được tab Shopee trên trình duyệt");
  job._targetTabId = tab.id;
  job._targetTabUrl = tab.url || "";
  await traceJob(job, progress, "tab-ready", {
    itemid,
    tabId: tab.id,
    tabUrl: tab.url || "",
  });

  // 2. Giải quyết shopid qua in-tab execution (tuyệt đối không dùng background fetch)
  if (!shopid) shopid = await resolveShopId(itemid, job.url, tab, progress);
  if (!shopid) throw new Error(`Không tìm thấy shopid cho item ${itemid}`);
  job._targetShopId = shopid;
  await traceJob(job, progress, "shop-resolved", {
    itemid,
    shopid,
    tabId: tab.id,
    tabUrl: tab.url || "",
  });

  // 3. Kiểm tra URL hiện tại có đúng shopid/itemid không
  const currentTabIds = idsFrom(tab.url || "");
  if (currentTabIds.itemid && String(currentTabIds.itemid) !== String(itemid)) {
    throw new Error(`Tab URL không khớp itemid: URL có itemid=${currentTabIds.itemid}, yêu cầu itemid=${itemid}`);
  }
  if (currentTabIds.shopid && String(currentTabIds.shopid) !== String(shopid)) {
    console.warn(`[bridge] Tab URL shopid (${currentTabIds.shopid}) khác resolved shopid (${shopid})`);
  }

  // 4. Chạy preflight ratings API trong chính tab đó với credentials: "include"
  const preflightStart = Date.now();
  const preflightRes = await preflightRatingsInTab(tab.id, itemid, shopid, tab.url || job.url);
  const preflightEnd = Date.now();
  const preflightElapsed = preflightEnd - preflightStart;
  const evalResult = evaluatePreflightResult(preflightRes);

  // 5. Ghi rõ status, error, is_login
  await traceJob(job, progress, "tab-preflight", {
    itemid,
    shopid,
    tabId: tab.id,
    tabUrl: tab.url || "",
    httpStatus: evalResult.status,
    responseUrl: evalResult.responseUrl,
    elapsedMs: preflightElapsed,
    error: evalResult.error,
    isLogin: evalResult.isLogin,
    requestStart: new Date(preflightStart).toISOString(),
    requestEnd: new Date(preflightEnd).toISOString(),
  });

  // Classify the API response before deriving auth state. Shopee can return
  // 403/error=90309999 together with is_login=true; that is API blocking, not logout.
  const preflightFailureKind = classifyShopeeFailure(preflightRes);
  if (!preflightRes.ok || preflightFailureKind === "verification") {
    const failureKind = preflightFailureKind;
    const err = new Error(evalResult.error || formatShopeeFailure(preflightRes));
    err.failureKind = failureKind;
    err.fetchRes = preflightRes;
    throw err;
  }

  // Nếu phiên chưa đăng nhập thì báo login_required và dừng ngay
  if (!evalResult.isLogin) {
    const err = new Error(evalResult.error || "Shopee login required (is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome");
    err.failureKind = "login";
    err.fetchRes = preflightRes;
    throw err;
  }

  // 5. Trích xuất số tham chiếu UI Shopee
  const uiReference = extractRatingSummary(preflightRes.json);
  let total = uiReference.total;

  // 6. Nạp Checkpoint V2 (hỗ trợ migration từ V1)
  let loadedCheckpoint = null;
  if (job.checkpoint) {
    loadedCheckpoint = normalizeCheckpointV2(job.checkpoint, itemid, shopid);
  }
  if (!loadedCheckpoint) {
    try {
      const stored = await chrome.storage.local.get([`checkpoint_${job.id}`]);
      const chk = stored[`checkpoint_${job.id}`];
      if (chk) loadedCheckpoint = normalizeCheckpointV2(chk, itemid, shopid);
    } catch {}
  }
  if (loadedCheckpoint?.total && total === null) {
    total = loadedCheckpoint.total;
  }

  // 7. Khởi tạo Scopes: all, comment, media
  let scopes = loadedCheckpoint?.scopes ? { ...loadedCheckpoint.scopes } : null;
  if (!scopes) {
    scopes = {
      all: {
        key: "all",
        label: "Tất cả",
        filter: 0,
        rating_type: 0,
        next_offset: 0,
        rows_count: 0,
        completed: false,
        discovered: true,
        probe_stats: { count: null, comment_ratio: null, media_ratio: null },
      },
      comment: {
        key: "comment",
        label: "Có bình luận",
        filter: null,
        rating_type: 0,
        next_offset: 0,
        rows_count: 0,
        completed: false,
        discovered: false,
      },
      media: {
        key: "media",
        label: "Có hình ảnh / Video",
        filter: null,
        rating_type: 0,
        next_offset: 0,
        rows_count: 0,
        completed: false,
        discovered: false,
      },
    };
  }

  // 8. Unique reviews storage
  const uniqueReviewsMap = new Map();
  const SCOPE_KEYS = ["all", "comment", "media"];
  let activeScopeKey = loadedCheckpoint?.active_scope || "all";
  const startIdx = Math.max(0, SCOPE_KEYS.indexOf(activeScopeKey));

  // 9. Vòng lặp duyệt từng scope độc lập
  for (let sIdx = startIdx; sIdx < SCOPE_KEYS.length; sIdx++) {
    const scopeKey = SCOPE_KEYS[sIdx];
    const scope = scopes[scopeKey];

    // Chạy probe nếu là scope comment/media và chưa được phát hiện
    if ((scopeKey === "comment" || scopeKey === "media") && !scope.discovered && scope.filter == null && !scope.completed) {
      const probe = await probeFilterCandidates(tab.id, itemid, shopid, tab.url || job.url, job, progress);
      if (scopes.comment.filter == null && !scopes.comment.completed) {
        scopes.comment.filter = probe.commentFilter;
        scopes.comment.discovered = probe.commentFilter != null;
        scopes.comment.probe_stats = probe.commentStat || null;
        if (probe.commentFilter == null) {
          scopes.comment.completed = true;
          scopes.comment.unavailable_reason = "Không tìm thấy filter 'Có bình luận' từ probe";
        }
      }
      if (scopes.media.filter == null && !scopes.media.completed) {
        scopes.media.filter = probe.mediaFilter;
        scopes.media.discovered = probe.mediaFilter != null;
        scopes.media.probe_stats = probe.mediaStat || null;
        if (probe.mediaFilter == null) {
          scopes.media.completed = true;
          scopes.media.unavailable_reason = "Không tìm thấy filter 'Có hình ảnh/video' từ probe";
        }
      }
    }

    if (scope.completed || !scope.discovered || scope.filter == null) {
      continue;
    }

    activeScopeKey = scopeKey;
    scope.target = scopeTarget(scopeKey, uiReference, total);
    if (!Array.isArray(scope.used_rating_buckets)) scope.used_rating_buckets = [];
    let offset = scope.next_offset || 0;
    let ratingType = scope.rating_type || 0;
    let typeIndex = Math.max(0, RATING_TYPES.indexOf(ratingType));

    while (uniqueReviewsMap.size < MAX_REVIEWS) {
      if (!scope.used_rating_buckets.includes(ratingType)) {
        scope.used_rating_buckets.push(ratingType);
      }
      const requestStarted = Date.now();
      const requestStartIso = new Date(requestStarted).toISOString();
      await traceJob(job, progress, "ratings-request", {
        itemid,
        shopid,
        offset,
        ratingType,
        filter: scope.filter,
        scope: scopeKey,
        limit: PAGE_SIZE,
        tabId: tab.id,
        tabUrl: tab.url || "",
        requestStart: requestStartIso,
      });

      let fetchRes = await fetchRatingsFromTab(tab.id, itemid, shopid, offset, PAGE_SIZE, tab.url || job.url, ratingType, scope.filter);
      if (!fetchRes.ok && (fetchRes.retryableTabError || fetchRes.isTimeout)) {
        const recoveredTab = await findOrOpenShopeeTab(job.url, itemid);
        if (recoveredTab?.id) {
          tab = recoveredTab;
          job._targetTabId = tab.id;
          job._targetTabUrl = tab.url || job.url;
          await traceJob(job, progress, "tab-recovered", {
            itemid,
            shopid,
            tabId: tab.id,
            tabUrl: tab.url || job.url,
            offset,
            ratingType,
            filter: scope.filter,
            scope: scopeKey,
            limit: PAGE_SIZE,
            error: fetchRes.error,
          });
          fetchRes = await fetchRatingsFromTab(tab.id, itemid, shopid, offset, PAGE_SIZE, tab.url || job.url, ratingType, scope.filter);
        }
      }

      const requestEnded = Date.now();
      const requestEndIso = new Date(requestEnded).toISOString();
      const elapsedMs = requestEnded - requestStarted;
      const httpStatus = typeof fetchRes.status === "number" ? fetchRes.status : (fetchRes.ok ? 200 : 500);

      await traceJob(job, progress, "ratings-response", {
        itemid,
        shopid,
        offset,
        ratingType,
        filter: scope.filter,
        scope: scopeKey,
        limit: PAGE_SIZE,
        tabId: tab.id,
        tabUrl: tab.url || "",
        requestStart: requestStartIso,
        requestEnd: requestEndIso,
        elapsedMs,
        httpStatus,
        responseUrl: fetchRes.url || null,
        error: fetchRes.ok ? null : (fetchRes.error || "Shopee API request failed"),
      });

      if (!fetchRes.ok) {
        let failureKind = classifyShopeeFailure(fetchRes);
        if (failureKind === "api_blocked" && tab?.id) {
          try {
            const captchaCheck = await detectCaptchaInTab(tab.id);
            if (captchaCheck?.detected) failureKind = "verification";
          } catch {}
        }
        if (failureKind === "verification" || failureKind === "api_blocked") {
          try {
            await chrome.storage.local.set({
              [`checkpoint_${job.id}`]: {
                version: 2,
                active_scope: scopeKey,
                scopes,
                next_offset: offset,
                rating_type: ratingType,
                rows_count: uniqueReviewsMap.size,
                itemid,
                shopid,
                total,
                ui_reference: uiReference,
                updated_at: Date.now(),
              },
            });
          } catch {}
        }
        const err = new Error(fetchRes.error ? `Shopee API request failed: ${fetchRes.error}` : formatShopeeFailure(fetchRes));
        err.failureKind = failureKind;
        err.fetchRes = fetchRes;
        throw err;
      }

      const json = fetchRes.json;
      const failureKindFromRes = classifyShopeeFailure(fetchRes);
      if (failureKindFromRes === "verification" || (json && (json.error || json.is_login === false || (json.data && json.data.is_login === false)))) {
        let failureKind = failureKindFromRes;
        if (failureKind === "api_blocked" && tab?.id) {
          try {
            const captchaCheck = await detectCaptchaInTab(tab.id);
            if (captchaCheck?.detected) failureKind = "verification";
          } catch {}
        }
        if (failureKind === "verification" || failureKind === "api_blocked") {
          try {
            await chrome.storage.local.set({
              [`checkpoint_${job.id}`]: {
                version: 2,
                active_scope: scopeKey,
                scopes,
                next_offset: offset,
                rating_type: ratingType,
                rows_count: uniqueReviewsMap.size,
                itemid,
                shopid,
                total,
                ui_reference: uiReference,
                updated_at: Date.now(),
              },
            });
          } catch {}
        }
        const err = new Error(formatShopeeFailure(fetchRes));
        err.failureKind = failureKind;
        err.fetchRes = fetchRes;
        throw err;
      }

      const batch = (json && (json.data?.ratings || json.ratings || json.items)) || [];
      if (!Array.isArray(batch) || batch.length === 0) {
        if (shouldAdvanceRatingType(scopeKey, scope, uiReference, total, typeIndex)) {
          typeIndex += 1;
          ratingType = RATING_TYPES[typeIndex];
          scope.rating_type = ratingType;
          offset = 0;
          scope.next_offset = 0;
          await sendCheckpoint({
            job: job.id,
            version: 2,
            active_scope: scopeKey,
            scopes,
            offset: 0,
            next_offset: 0,
            rating_type: ratingType,
            itemid,
            shopid,
            rows: [],
            total,
            ui_reference: uiReference,
          });
          await chrome.storage.local.set({
            [`checkpoint_${job.id}`]: {
              version: 2,
              active_scope: scopeKey,
              scopes,
              next_offset: 0,
              rating_type: ratingType,
              rows_count: uniqueReviewsMap.size,
              itemid,
              shopid,
              total,
              ui_reference: uiReference,
              updated_at: Date.now(),
            },
          });
          await traceJob(job, progress, "ratings-segment", {
            itemid,
            shopid,
            scope: scopeKey,
            ratingType,
            rowsCount: uniqueReviewsMap.size,
            totalTarget: total,
            scopeTarget: scopeTarget(scopeKey, uiReference, total),
          });
          await progress({
            status: "running",
            stage: "fetch",
            active_scope: scopeKey,
            scope_label: scope.label,
            scopes,
            message: `Shopee giới hạn luồng ${scope.label}; đang cào tiếp nhóm ${ratingType} sao`,
            rows: uniqueReviewsMap.size,
            percent: crawlPercent(uniqueReviewsMap.size, total, 10, 80),
          });
          continue;
        }
        scope.completed = true;
        break;
      }

      const newBatchRows = [];
      for (const raw of batch) {
        const review = normaliseRating(raw);
        const key = reviewFingerprint(review);
        if (!key) continue;
        if (uniqueReviewsMap.has(key)) {
          mergeReview(uniqueReviewsMap.get(key), review);
        } else {
          uniqueReviewsMap.set(key, review);
          newBatchRows.push(review);
        }
      }

      scope.rows_count = (scope.rows_count || 0) + batch.length;
      const nextOffset = offset + batch.length;
      scope.next_offset = nextOffset;

      if (total === null) total = ratingTotal(json);
      const completedRows = uniqueReviewsMap.size;
      const pct = crawlPercent(completedRows, total, 10, 80);

      await traceJob(job, progress, "ratings-batch", {
        itemid,
        shopid,
        scope: scopeKey,
        offset,
        ratingType,
        filter: scope.filter,
        limit: PAGE_SIZE,
        tabId: tab.id,
        tabUrl: tab.url || "",
        batchSize: batch.length,
        scopeRows: scope.rows_count,
        uniqueRowsCount: completedRows,
        totalTarget: total,
      });

      await progress({
        status: "running",
        stage: "fetch",
        active_scope: scopeKey,
        scope_label: scope.label,
        scopes,
        rows: completedRows,
        total,
        ui_reference: uiReference,
        message: `Đang cào [${scope.label}]: offset ${nextOffset}, unique ${completedRows}${total ? "/" + total : ""}`,
        percent: pct,
      });

      await sendCheckpoint({
        job: job.id,
        version: 2,
        active_scope: scopeKey,
        scopes,
        offset: nextOffset,
        next_offset: nextOffset,
        rating_type: ratingType,
        itemid,
        shopid,
        rows: newBatchRows,
        total,
        ui_reference: uiReference,
      });

      await chrome.storage.local.set({
        [`checkpoint_${job.id}`]: {
          version: 2,
          active_scope: scopeKey,
          scopes,
          next_offset: nextOffset,
          rating_type: ratingType,
          rows_count: completedRows,
          itemid,
          shopid,
          total,
          ui_reference: uiReference,
          updated_at: Date.now(),
        },
      });

      if (batch.length < PAGE_SIZE) {
        if (shouldAdvanceRatingType(scopeKey, scope, uiReference, total, typeIndex)) {
          typeIndex += 1;
          ratingType = RATING_TYPES[typeIndex];
          scope.rating_type = ratingType;
          offset = 0;
          scope.next_offset = 0;
          continue;
        }
        scope.completed = true;
        break;
      }

      offset = nextOffset;
      await new Promise((s) => setTimeout(s, PACE_MS));
    }

    scope.completed = true;
  }

  const allReviews = Array.from(uniqueReviewsMap.values());
  job._crawlSummary = {
    version: 2,
    expected: total,
    collected: allReviews.length,
    ui_reference: uiReference,
    scopes: {
      all: { ...scopes.all },
      comment: { ...scopes.comment },
      media: { ...scopes.media },
    },
    complete: !total || allReviews.length >= total,
  };
  try { await chrome.storage.local.remove([`checkpoint_${job.id}`]); } catch {}
  return allReviews;
}

async function runJob(job) {
  console.log("[bridge] ▶ start job", job.id, job.kind, job.url);
  activeJobs.set(job.id, job);
  updateState("busy");

  const progress = (patch) => reportProgress(job, patch);
  try {
    await traceJob(job, progress, "job-start", {
      kind: job.kind,
      sourceUrl: job.url,
      itemid: job.itemid || (job.url ? idsFrom(job.url).itemid : null),
    });
    await progress({ status: "running", stage: "init", message: "Bắt đầu cào", percent: 5 });
    let rows = [];
    if (job.kind === "shopee-reviews" || job.url.includes("shopee.vn")) {
      rows = await extractShopeeReviews(job, progress);
    }
    const parsed = idsFrom(job.url);
    const itemid = parsed.itemid || job.itemid;
    const crawlSummary = job._crawlSummary || null;
    const outputName = itemid ? "shopee_" + itemid + "_reviews" : (job.name || "shopee_" + job.id);
    await traceJob(job, progress, "upload-start", {
      itemid,
      shopid: job._targetShopId,
      tabId: job._targetTabId,
      tabUrl: job._targetTabUrl,
      rowsCount: rows.length,
      totalTarget: crawlSummary?.expected || null,
      outputName,
    });
    const partial = Boolean(crawlSummary && !crawlSummary.complete);
    await progress({
      status: "saving",
      stage: partial ? "partial-upload" : "upload",
      message: partial
        ? `Đã lấy tối đa ${crawlSummary.collected}/${crawlSummary.expected} đánh giá Shopee cho phép; đang lưu kết quả một phần`
        : `Đang lưu ${rows.length} dòng`,
      rows: crawlSummary?.collected || rows.length,
      total: crawlSummary?.expected || null,
      partial,
      percent: 95,
    });
    const savedResult = await uploadIngestResult({
      job: job.id,
      name: outputName,
      source: job.url,
      rows,
      crawl_summary: crawlSummary,
      partial,
    });
    await rememberCompletedResult(job, savedResult);
    await traceJob(job, progress, "upload-complete", {
      itemid,
      shopid: job._targetShopId,
      tabId: job._targetTabId,
      tabUrl: job._targetTabUrl,
      rowsCount: rows.length,
      outputName,
    });
    console.log("[bridge] ✔ job", job.id, "finished:", rows.length, "rows");
  } catch (e) {
    console.error("[bridge] ✘ job", job.id, "failed:", e.message);
    await traceJob(job, progress, "job-failed", {
      itemid: job.itemid || (job.url ? idsFrom(job.url).itemid : null),
      shopid: job._targetShopId || job.shopid,
      tabId: job._targetTabId,
      tabUrl: job._targetTabUrl,
      error: String(e.message || e),
    });
    const message = String(e.message || e);
    const failureKind = e.failureKind || classifyShopeeFailure({ error: message, textSample: message });

    if (failureKind === "login") {
      // 1. login_required: Dừng đúng lỗi, không bypass, tuyệt đối không gọi resume verification
      updateState("login_required", {
        job_id: job.id,
        url: job.url,
        tab_id: job._targetTabId || null,
        reason: message,
        kind: "login",
      });
      await progress({
        status: "login_required",
        stage: "login_required",
        message: "Shopee yêu cầu đăng nhập tài khoản (is_login=false, error=90309999). Vui lòng đăng nhập trên tab Chrome.",
        error: message,
        login_required: true,
        verification_required: false,
        api_blocked: false,
        percent: 100,
      });
      await uploadIngestResult({
        job: job.id,
        error: message,
        login_required: true,
        verification_required: false,
        api_blocked: false,
        stage: "login_required",
      });
    } else if (failureKind === "verification") {
      // 2. traffic_verification / CAPTCHA: Hỗ trợ pause/resume verification
      lastVerificationJob = job;
      pendingVerificationJobs.set(job.id, job);
      let checkpoint = null;
      try {
        const stored = await chrome.storage.local.get([`checkpoint_${job.id}`]);
        checkpoint = stored[`checkpoint_${job.id}`] || null;
      } catch {}
      try {
        await chrome.storage.local.set({ [`pending_job_${job.id}`]: job });
      } catch {}
      const parsed = idsFrom(job.url || "");
      const itemid = job.itemid || parsed.itemid || checkpoint?.itemid;
      const shopid = job._targetShopId || job.shopid || parsed.shopid || checkpoint?.shopid;
      const evidence = await captureTabEvidence(job._targetTabId || null);
      const verificationUrl = extractVerificationUrl(e?.fetchRes) || extractVerificationUrl(message) || job.url;
      await triggerVerificationRequired({
        job_id: job.id,
        url: job.url,
        verification_url: verificationUrl,
        target_url: verificationUrl,
        itemid,
        shopid,
        tab_id: job._targetTabId || null,
        reason: message,
        kind: "verification",
        evidence_screenshot: evidence,
        checkpoint,
      });
      await progress({
        status: "awaiting_user_verification",
        stage: "verification_required",
        message: "Shopee yêu cầu xác minh danh tính / giải CAPTCHA. Vui lòng thao tác trên tab Chrome.",
        error: message,
        verification_required: true,
        login_required: false,
        api_blocked: false,
        evidence_screenshot: evidence,
        percent: 100,
      });
      await uploadIngestResult({
        job: job.id,
        error: message,
        verification_required: true,
        login_required: false,
        api_blocked: false,
        evidence_screenshot: evidence,
        stage: "verification_required",
      });
    } else if (failureKind === "api_blocked") {
      // 3. api_blocked: Shopee chặn API (HTTP 403)
      let checkpoint = null;
      try {
        const stored = await chrome.storage.local.get([`checkpoint_${job.id}`]);
        checkpoint = stored[`checkpoint_${job.id}`] || null;
      } catch {}

      const targetTabId = job._targetTabId;
      // Quét xem tab Shopee có slider CAPTCHA hoặc trang xác minh không
      const captchaCheck = targetTabId ? await detectCaptchaInTab(targetTabId) : { detected: false };
      const evidence = targetTabId ? await captureTabEvidence(targetTabId) : null;

      // Lưu job vào pending verification để sẵn sàng resume
      lastVerificationJob = job;
      pendingVerificationJobs.set(job.id, job);
      try {
        await chrome.storage.local.set({ [`pending_job_${job.id}`]: job });
      } catch {}

      if (captchaCheck?.detected) {
        // --- 1. VERIFICATION_REQUIRED: Tab hiển thị CAPTCHA challenge rõ ràng ---
        await triggerVerificationRequired({
          job_id: job.id,
          url: job.url,
          tab_id: targetTabId || null,
          reason: `Phát hiện thử thách CAPTCHA khi gọi API Shopee (${captchaCheck.type || "slider"})`,
          kind: "verification",
          evidence_screenshot: evidence,
          checkpoint,
        });
        await progress({
          status: "awaiting_user_verification",
          stage: "verification_required",
          message: "Shopee yêu cầu xác minh danh tính / giải CAPTCHA. Vui lòng thao tác trên tab Chrome.",
          error: message,
          verification_required: true,
          login_required: false,
          api_blocked: false,
          evidence_screenshot: evidence,
          checkpoint,
          percent: 100,
        });
        await uploadIngestResult({
          job: job.id,
          error: message,
          verification_required: true,
          login_required: false,
          api_blocked: false,
          evidence_screenshot: evidence,
          checkpoint,
          stage: "verification_required",
        });
      } else {
        // --- 2. API_BLOCKED: Bị chặn API (403) nhưng tab không có CAPTCHA challenge ---
        let tabFocused = false;
        if (targetTabId && chrome.tabs) {
          try {
            const tab = await chrome.tabs.update(targetTabId, { active: true });
            if (tab && tab.windowId && chrome.windows) {
              await chrome.windows.update(tab.windowId, { focused: true });
            }
            tabFocused = true;
          } catch (tabErr) {
            console.warn("[bridge] Could not focus tab for api_blocked:", tabErr?.message);
          }
        }
        if (!tabFocused && chrome.tabs?.create) {
          try {
            const statusUrl = `http://127.0.0.1:8766/ingest?job=${encodeURIComponent(job.id)}`;
            await chrome.tabs.create({ url: statusUrl });
          } catch {}
        }

        // Gửi WebSocket event api_blocked lên server để lưu checkpoint và log
        if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
          bridgeSend({
            v: 1,
            type: "api_blocked",
            id: "blocked-" + Date.now(),
            params: {
              job_id: job.id,
              url: job.url,
              tab_id: targetTabId || null,
              reason: message,
              status: 403,
              checkpoint,
              evidence_screenshot: evidence,
            },
          });
        }

        // Cập nhật state nội bộ và dừng job, bật Smart Auto-Resume Watcher (lắng nghe reload/active tab và backoff an toàn)
        updateState("api_blocked", {
          job_id: job.id,
          url: job.url,
          tab_id: targetTabId || null,
          reason: message,
          kind: "api_blocked",
          checkpoint,
          evidence_screenshot: evidence,
        });

        if (targetTabId) {
          startApiBlockedWatcher({
            job_id: job.id,
            url: job.url,
            tab_id: targetTabId,
            itemid: job.itemid,
            shopid: job._targetShopId || job.shopid,
            checkpoint,
          });
        }

        await progress({
          status: "error",
          stage: "api_blocked",
          message: "Shopee chặn API đánh giá (HTTP 403 / API Blocked). Job đã dừng an toàn; vui lòng kiểm tra lại.",
          error: message,
          api_blocked: true,
          verification_required: false,
          login_required: false,
          checkpoint,
          evidence_screenshot: evidence,
          percent: 100,
        });
        await uploadIngestResult({
          job: job.id,
          error: message,
          api_blocked: true,
          verification_required: false,
          login_required: false,
          checkpoint,
          evidence_screenshot: evidence,
          stage: "api_blocked",
        });
      }
    } else {
      // 4. Lỗi WebSocket / extension / network khác
      updateState("error", {
        job_id: job.id,
        url: job.url,
        reason: message,
        kind: "extension_error",
      });
      await progress({
        status: "error",
        stage: "failed",
        message,
        error: message,
        percent: 100,
        verification_required: false,
        login_required: false,
        api_blocked: false,
      });
      await uploadIngestResult({
        job: job.id,
        error: message,
        verification_required: false,
        login_required: false,
        api_blocked: false,
      });
    }
  } finally {
    activeJobs.delete(job.id);
    const fallbackState = verificationInfo
      ? (verificationInfo.kind === "login" ? "login_required" : (verificationInfo.kind === "api_blocked" ? "api_blocked" : "awaiting_user_verification"))
      : (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN ? "connected" : "disconnected");
    updateState(fallbackState, verificationInfo);
  }
}

// ── Verification Challenge Detection, Evidence & Handover ────────────────────
const notifiedVerificationJobIds = new Set();
const verificationCycles = new Map();
const resumeInFlightMap = new Map();
let verificationWatcherInterval = null;
let activeTabUpdateListener = null;
let apiBlockedWatcherInterval = null;
let activeApiBlockedTabListener = null;
let lastApiBlockedCheckTime = 0;

async function cleanupPendingJobState(jobId, { clearVerification = false } = {}) {
  if (!jobId) return;
  pendingVerificationJobs.delete(jobId);
  notifiedVerificationJobIds.delete(jobId);
  resumeInFlightMap.delete(jobId);
  if (lastVerificationJob && lastVerificationJob.id === jobId) {
    lastVerificationJob = null;
  }
  if (verificationInfo?.job_id === jobId) {
    if (verificationInfo.kind === "api_blocked") stopApiBlockedWatcher();
    if (verificationInfo.kind === "verification") stopVerificationWatcher();
    if (clearVerification) {
      verificationInfo = null;
      updateState(bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN ? "connected" : "disconnected");
    }
  }
  try {
    await chrome.storage.local.remove([`pending_job_${jobId}`]);
  } catch {}
}

function stopApiBlockedWatcher() {
  if (apiBlockedWatcherInterval) {
    clearTimeout(apiBlockedWatcherInterval);
    apiBlockedWatcherInterval = null;
  }
  if (activeApiBlockedTabListener && chrome.tabs?.onUpdated) {
    try {
      chrome.tabs.onUpdated.removeListener(activeApiBlockedTabListener);
    } catch {}
    activeApiBlockedTabListener = null;
  }
}

async function restorePendingVerificationWatchers() {
  let stored = {};
  try {
    stored = await chrome.storage.local.get(null);
  } catch {
    return;
  }
  const info = stored.verificationInfo;
  if (!info || !info.job_id || info.kind === "login") return;
  const storedJob = stored[`pending_job_${info.job_id}`];
  if (storedJob && !pendingVerificationJobs.has(info.job_id)) {
    pendingVerificationJobs.set(info.job_id, storedJob);
    lastVerificationJob = storedJob;
  }
  if (!info.tab_id) return;
  verificationInfo = info;
  updateState(stateForVerification(info), info);
  if (info.kind === "verification") {
    startVerificationWatcher(info);
  } else if (info.kind === "api_blocked" && !info.auto_recheck_exhausted) {
    startApiBlockedWatcher(info);
  }
}

function startApiBlockedWatcher(details) {
  stopApiBlockedWatcher();
  const jid = details?.job_id;
  const targetTabId = details?.tab_id;
  if (!jid || !targetTabId || !chrome.tabs) return;

  const parsed = idsFrom(details?.url || "");
  const itemid = details?.itemid || details?.checkpoint?.itemid || parsed.itemid;
  const shopid = details?.shopid || details?.checkpoint?.shopid || parsed.shopid;
  const referer = details?.url || "";

  const checkAndAutoResume = async (source) => {
    if (!verificationInfo || verificationInfo.job_id !== jid || verificationInfo.kind !== "api_blocked") {
      stopApiBlockedWatcher();
      return;
    }
    const now = Date.now();
    // Throttle: don't preflight more often than once every 8 seconds
    if (now - lastApiBlockedCheckTime < 8000) {
      return;
    }
    lastApiBlockedCheckTime = now;

    if (resumeInFlightMap.get(jid)) return;

    console.log(`[bridge] Smart auto-recheck for api_blocked (trigger: ${source}) on tab ${targetTabId}...`);
    let preflightRes = null;
    try {
      preflightRes = await preflightRatingsInTab(targetTabId, itemid, shopid, referer);
    } catch (err) {
      updateState("api_blocked", {
        ...verificationInfo,
        auto_recheck_active: true,
        last_recheck_at: new Date().toISOString(),
        last_recheck_status: "network_error",
      });
      console.warn("[bridge] Smart preflight error:", err?.message || err);
      return;
    }

    const evalRes = evaluatePreflightResult(preflightRes);
    const hasValidRatings = isUsableRatingsPreflight(preflightRes);

    if (hasValidRatings) {
      console.log(`[bridge] ✔ Smart auto-recheck succeeded (HTTP 200)! Auto-resuming job: ${jid}`);
      stopApiBlockedWatcher();
      updateState("resuming", { job_id: jid, message: "Shopee đã hết chặn API! Đang tự động tiếp tục cào..." });
      resumeVerification(jid, {
        recheck: true,
        checkpoint: details?.checkpoint,
        message: "Shopee đã hết chặn API (HTTP 200). Hệ thống tự động tiếp tục cào từ checkpoint.",
      });
    } else {
      updateState("api_blocked", {
        ...verificationInfo,
        auto_recheck_active: true,
        last_recheck_at: new Date().toISOString(),
        last_recheck_status: preflightRes?.status || "unknown",
      });
      console.log(`[bridge] Smart preflight still blocked (status: ${preflightRes?.status}):`, evalRes.error || "unusable ratings payload");
    }
  };

  // 1. Listen for tab reload / navigation
  const tabListener = async (tabId, changeInfo, tab) => {
    if (tabId !== targetTabId) return;
    if (changeInfo.status === "complete") {
      await checkAndAutoResume("tab_reload");
    }
  };
  chrome.tabs.onUpdated?.addListener(tabListener);
  activeApiBlockedTabListener = tabListener;

  // 2. Safe Backoff Timer: retry for a few minutes without looping forever.
  let backoffIndex = 0;
  const backoffDelays = [15000, 45000, 90000, 180000, 300000];
  const scheduleNextBackoff = () => {
    if (backoffIndex >= backoffDelays.length) {
      updateState("api_blocked", {
        ...verificationInfo,
        auto_recheck_active: false,
        auto_recheck_exhausted: true,
        auto_recheck_attempts: backoffIndex,
        next_recheck_at: null,
      });
      return;
    }
    const baseDelay = backoffDelays[backoffIndex++];
    const jitter = Math.floor(Math.random() * Math.min(5000, Math.max(1000, baseDelay * 0.1)));
    const delay = baseDelay + jitter;
    updateState("api_blocked", {
      ...verificationInfo,
      auto_recheck_active: true,
      auto_recheck_exhausted: false,
      auto_recheck_attempts: backoffIndex,
      next_recheck_at: new Date(Date.now() + delay).toISOString(),
    });
    apiBlockedWatcherInterval = setTimeout(async () => {
      await checkAndAutoResume("backoff_timer");
      scheduleNextBackoff();
    }, delay);
  };
  scheduleNextBackoff();
}

async function captureTabEvidence(tabId) {
  try {
    if (!chrome.tabs?.captureVisibleTab) return null;
    let targetId = tabId;
    if (!targetId) {
      const active = await getActiveTab();
      targetId = active?.id;
    }
    if (!targetId) return null;
    const tab = await chrome.tabs.get(targetId);
    if (!tab || !tab.windowId) return null;
    try {
      await chrome.tabs.update(targetId, { active: true });
      if (tab.windowId && chrome.windows) await chrome.windows.update(tab.windowId, { focused: true });
      await new Promise((r) => setTimeout(r, 200));
    } catch {}
    return await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  } catch (err) {
    console.warn("[bridge] captureTabEvidence failed:", err?.message || err);
    return null;
  }
}

async function tryAutoDragShopeeCaptcha(tabId, detection) {
  if (!tabId || !detection?.detected) return { attempted: false, reason: "no_captcha_detection" };

  let slider = detection.slider;
  if (!slider && chrome.scripting?.executeScript) {
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => {
          const handleSelectors = [
            ".shopee-captcha-slider__btn",
            ".shopee-captcha-slider__button",
            "div[class*='slider__btn']",
            "div[class*='slider__button']",
            "div[class*='slider-btn']",
            "div[class*='slider-button']",
            ".verify-slider__btn",
          ];
          for (const selector of handleSelectors) {
            const el = document.querySelector(selector);
            if (!el || (!el.offsetWidth && !el.offsetHeight)) continue;
            const rect = el.getBoundingClientRect();
            return {
              selector,
              x: Math.round(rect.left + rect.width / 2),
              y: Math.round(rect.top + rect.height / 2),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
              isOrangeHandle: true,
            };
          }
          const containerSelectors = [
            ".shopee-captcha-slider",
            ".verify-slider",
            "[class*='captcha'][class*='slider']",
          ];
          for (const selector of containerSelectors) {
            const el = document.querySelector(selector);
            if (!el || (!el.offsetWidth && !el.offsetHeight)) continue;
            const rect = el.getBoundingClientRect();
            return {
              selector,
              x: Math.round(rect.left + Math.min(32, rect.width / 2)),
              y: Math.round(rect.top + rect.height / 2),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
              isOrangeHandle: false,
            };
          }
          return null;
        },
      });
      slider = res?.result || null;
    } catch (err) {
      console.warn("[bridge] Slider coordinate lookup failed:", err?.message || err);
    }
  }

  if (!slider || !Number.isFinite(slider.x) || !Number.isFinite(slider.y)) {
    return { attempted: false, reason: "slider_coordinates_missing" };
  }

  const startX = slider.x;
  const startY = slider.y;
  const travel = Math.max(240, Number(slider.width || 300) - 18);
  const endX = startX + travel;
  const points = [];
  for (let i = 0; i <= 18; i++) {
    const t = i / 18;
    const ease = 1 - Math.pow(1 - t, 2);
    const jitter = Math.sin(i * 1.7) * 1.5;
    points.push({ x: Math.round(startX + travel * ease), y: Math.round(startY + jitter) });
  }

  if (chrome.debugger?.attach && chrome.debugger?.sendCommand && chrome.debugger?.detach) {
    const target = { tabId };
    let attached = false;
    try {
      await chrome.debugger.attach(target, "1.3");
      attached = true;
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: startX, y: startY, button: "left" });
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mousePressed", x: startX, y: startY, button: "left", clickCount: 1 });
      for (const point of points) {
        await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, button: "left", buttons: 1 });
        await new Promise((r) => setTimeout(r, 28 + Math.floor(Math.random() * 18)));
      }
      await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: endX, y: startY, button: "left", clickCount: 1 });
      return { attempted: true, method: "debugger", startX, startY, endX };
    } catch (err) {
      console.warn("[bridge] Debugger slider drag failed:", err?.message || err);
    } finally {
      if (attached) {
        try { await chrome.debugger.detach(target); } catch {}
      }
    }
  }

  if (chrome.scripting?.executeScript) {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (x, y, toX) => {
        const target = document.elementFromPoint(x, y);
        if (!target) return { attempted: false, reason: "element_from_point_missing" };
        const fire = (type, px, py) => {
          const opts = { bubbles: true, cancelable: true, clientX: px, clientY: py, screenX: px, screenY: py, buttons: type === "mouseup" ? 0 : 1 };
          target.dispatchEvent(new MouseEvent(type, opts));
          if (typeof PointerEvent !== "undefined") target.dispatchEvent(new PointerEvent(type.replace("mouse", "pointer"), { ...opts, pointerId: 1, pointerType: "mouse", isPrimary: true }));
        };
        fire("mousedown", x, y);
        for (let i = 1; i <= 16; i++) {
          const t = i / 16;
          fire("mousemove", Math.round(x + (toX - x) * t), Math.round(y + Math.sin(i) * 2));
        }
        fire("mouseup", toX, y);
        return { attempted: true, method: "dom_events" };
      },
      args: [startX, startY, endX],
    });
    return res?.result || { attempted: true, method: "dom_events" };
  }

  return { attempted: false, reason: "no_input_backend" };
}

async function detectCaptchaInTab(tabId, options = {}) {
  try {
    if (!tabId || !chrome.scripting?.executeScript) return { detected: false, resolved: false };
    const shouldScroll = options?.scrollIntoView !== false;
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (allowScroll) => {
        const win = typeof window !== "undefined" ? window : globalThis;
        const canScroll = allowScroll !== false;
        const url = location.href.toLowerCase();
        const isVerifyPage = url.includes("/verify/") || url.includes("anti_bot_tracking_id") || url.includes("captcha");
        const bodyText = document.body ? document.body.innerText.toLowerCase() : "";

        // 1. Kiểm tra trạng thái ĐÃ GIẢI XONG (Resolved)
        const checkPassedSelectors = [
          ".shopee-captcha-slider__btn--success",
          ".captcha-passed",
          ".verify-passed",
          ".slider--success",
          ".slider-btn--success",
          "[class*='btn--success']",
          "[class*='success']",
          "[class*='passed']",
        ];

        let isResolved = false;
        for (const s of checkPassedSelectors) {
          try {
            const el = document.querySelector(s);
            if (el && (el.offsetWidth > 0 || el.offsetHeight > 0)) {
              isResolved = true;
              break;
            }
          } catch {}
        }
        if (!isResolved && typeof document.querySelectorAll === "function") {
          try {
            isResolved = Array.from(document.querySelectorAll("svg, i, div, span, button")).some((el) => {
              const text = el.innerText || "";
              const hasCheckChar = text.includes("✔") || text.includes("✓");
              const style = typeof win.getComputedStyle === "function" ? win.getComputedStyle(el) : null;
              const bg = style?.backgroundColor || "";
              const color = style?.color || "";
              const isGreen = bg.includes("38, 170, 153") || bg.includes("32, 178, 170") || color.includes("38, 170, 153") || bg.includes("26aa99") || bg.includes("210, 236, 231");
              return hasCheckChar || (isGreen && (el.offsetWidth > 15 || el.offsetHeight > 15));
            });
          } catch {}
        }

        if (isResolved) {
          return { detected: false, resolved: true, type: "slider_passed", pageUrl: location.href };
        }

        // 2. Tìm Nút trượt màu cam (Slider Handle/Button) - Nút người dùng cần kéo
        const handleSelectors = [
          ".shopee-captcha-slider__btn",
          ".shopee-captcha-slider__button",
          "div[class*='slider__btn']",
          "div[class*='slider__button']",
          "div[class*='slider-btn']",
          "div[class*='slider-button']",
          ".verify-slider__btn",
          ".geetest_slider_btn",
        ];

        let handleEl = null;
        let handleSelector = null;
        for (const sel of handleSelectors) {
          try {
            const el = document.querySelector(sel);
            if (el && (el.offsetWidth > 0 || el.offsetHeight > 0)) {
              handleEl = el;
              handleSelector = sel;
              break;
            }
          } catch {}
        }

        // Nếu chưa tìm thấy class handle, quét tìm phần tử màu cam Shopee (#ee4d2d / rgb(238, 77, 45)) có hình nút
        if (!handleEl && typeof document.querySelectorAll === "function") {
          try {
            const candidates = Array.from(document.querySelectorAll("div, button, span, i"));
            handleEl = candidates.find((el) => {
              if (!el.offsetWidth || !el.offsetHeight) return false;
              if (el.offsetWidth > 120) return false; // Nút trượt vuông nhỏ khoảng 40-60px
              const style = typeof win.getComputedStyle === "function" ? win.getComputedStyle(el) : null;
              const bg = style?.backgroundColor || "";
              return bg.includes("238, 77, 45") || bg.includes("ee4d2d");
            }) || null;
            if (handleEl) handleSelector = "shopee-orange-handle";
          } catch {}
        }

        // 3. Tìm Container hoặc Widget Captcha tổng thể
        const containerSelectors = [
          ".shopee-captcha-slider",
          ".shopee-captcha-slider__bar",
          ".captcha_container",
          ".geetest_radar_btn",
          ".geetest_canvas_bg",
          "iframe[src*='verify']",
          "iframe[src*='captcha']",
          ".challenge-container",
          ".verify-container",
          ".verify-slider",
          "div[class*='captcha-modal']",
          "div[class*='captcha_wrapper']",
          "div[class*='shopee-captcha']",
          "div[class*='traffic-verify']",
          "div[class*='puzzle-verify']",
        ];

        let containerEl = null;
        let containerSelector = null;
        for (const sel of containerSelectors) {
          try {
            const el = document.querySelector(sel);
            if (el && (el.offsetWidth > 0 || el.offsetHeight > 0)) {
              containerEl = el;
              containerSelector = sel;
              break;
            }
          } catch {}
        }

        // Nếu có phần tử Captcha (handle hoặc container)
        const primaryEl = containerEl || handleEl;
        if (primaryEl) {
          // Chỉ scroll một lần duy nhất vào trung tâm màn hình, không scroll giật giật lặp lại
          const alreadyScrolled = Boolean(win.__shopeeCaptchaScrolled);
          if (canScroll && !alreadyScrolled) {
            try {
              if (typeof primaryEl.scrollIntoView === "function") {
                primaryEl.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
                win.__shopeeCaptchaScrolled = true;
              }
            } catch {}
          }

          // Tính toạ độ chuẩn: ƯU TIÊN toạ độ tâm của Nút màu cam (handle)
          let sliderCoordinates = null;
          if (handleEl) {
            const hRect = typeof handleEl.getBoundingClientRect === "function"
              ? handleEl.getBoundingClientRect()
              : { left: 0, top: 0, width: handleEl.offsetWidth || 0, height: handleEl.offsetHeight || 0 };
            sliderCoordinates = {
              x: Math.round(hRect.left + hRect.width / 2),
              y: Math.round(hRect.top + hRect.height / 2),
              width: Math.round(hRect.width),
              height: Math.round(hRect.height),
              isOrangeHandle: true,
              handleSelector: handleSelector || null,
            };
          } else if (containerEl) {
            const cRect = typeof containerEl.getBoundingClientRect === "function"
              ? containerEl.getBoundingClientRect()
              : { left: 0, top: 0, width: containerEl.offsetWidth || 0, height: containerEl.offsetHeight || 0 };
            sliderCoordinates = {
              x: Math.round(cRect.left + Math.min(32, cRect.width / 2)),
              y: Math.round(cRect.top + cRect.height / 2),
              width: Math.round(cRect.width),
              height: Math.round(cRect.height),
              isOrangeHandle: false,
              handleSelector: null,
            };
          }

          return {
            detected: true,
            resolved: false,
            type: "dom_selector",
            selector: containerSelector || handleSelector || ".shopee-captcha-slider",
            handleSelector: handleSelector || null,
            elementFound: true,
            pageUrl: location.href,
            slider: sliderCoordinates,
          };
        }

        // 4. Fallback qua Text match (Tuyệt đối KHÔNG scroll vào thẻ text span chữ để tránh làm lệch màn hình)
        const phrases = [
          "kéo qua để hoàn thiện bức hình",
          "hoàn thiện bức hình",
          "xác nhận để tiếp tục",
          "trượt để hoàn thành",
          "kéo thanh trượt",
          "xác minh bạn không phải là người máy",
          "please slide to complete the puzzle",
          "slide to verify",
          "drag the slider",
        ];

        for (const p of phrases) {
          if (bodyText.includes(p)) {
            return { detected: true, resolved: false, type: "dom_text", phrase: p, elementFound: true, pageUrl: location.href };
          }
        }

        if (isVerifyPage) {
          return { detected: true, resolved: false, type: "slider_pending", pageUrl: location.href };
        }

        return { detected: false, resolved: false, pageUrl: location.href };
      },
      args: [shouldScroll],
    });
    return res?.result || { detected: false, resolved: false };
  } catch (err) {
    return { detected: false, resolved: false, error: err?.message };
  }
}

function stopVerificationWatcher() {
  if (verificationWatcherInterval) {
    clearInterval(verificationWatcherInterval);
    verificationWatcherInterval = null;
  }
  if (activeTabUpdateListener && chrome.tabs?.onUpdated) {
    try {
      chrome.tabs.onUpdated.removeListener(activeTabUpdateListener);
    } catch {}
    activeTabUpdateListener = null;
  }
}

function startVerificationWatcher(details) {
  stopVerificationWatcher();
  const jid = details?.job_id;
  const targetTabId = details?.tab_id;
  if (!jid || !targetTabId || !chrome.tabs) return;

  const parsed = idsFrom(details?.url || "");
  const itemid = details?.itemid || (details?.checkpoint?.itemid) || parsed.itemid;
  const shopid = details?.shopid || (details?.checkpoint?.shopid) || parsed.shopid;
  const referer = details?.referer || details?.url;

  let consecutiveAbsentCount = 0;
  let autoDragAttempts = 0;

  const performCheckAndPreflight = async () => {
    if (!verificationInfo || verificationInfo.job_id !== jid || verificationInfo.kind !== "verification") {
      stopVerificationWatcher();
      return;
    }
    const check = await detectCaptchaInTab(targetTabId, { scrollIntoView: false });
    if (check?.resolved || !check?.detected) {
      consecutiveAbsentCount++;
      console.log(`[bridge] CAPTCHA absent/resolved count: ${consecutiveAbsentCount}/2 (type: ${check?.type || "none"}) for job ${jid}`);
      if (consecutiveAbsentCount >= 2 || check?.resolved) {
        if (resumeInFlightMap.get(jid)) {
          console.log("[bridge] Resume already in-flight for job:", jid);
          return;
        }
        resumeInFlightMap.set(jid, true);

        console.log("[bridge] Challenge resolved/absent! Running preflight API in tab...");
        let preflightRes = null;
        try {
          preflightRes = await preflightRatingsInTab(targetTabId, itemid, shopid, referer);
        } catch (err) {
          console.warn("[bridge] Preflight error:", err?.message || err);
        }

        const evalRes = evaluatePreflightResult(preflightRes);
        const hasValidRatings = isUsableRatingsPreflight(preflightRes);

        if (hasValidRatings) {
          console.log("[bridge] ✔ Preflight succeeded (HTTP 200 & valid ratings)! Resuming job:", jid);
          stopVerificationWatcher();
          try {
            const currentTab = await chrome.tabs.get(targetTabId);
            if (currentTab?.url && (currentTab.url.includes("/verify/") || currentTab.url.includes("captcha"))) {
              await chrome.tabs.update(targetTabId, { url: referer || details?.url });
            }
          } catch {}

          const currentCycle = (verificationCycles.get(jid) || 0) + 1;
          verificationCycles.set(jid, currentCycle);
          resumeVerification(jid, {
            verification_cycle: currentCycle,
            checkpoint: details?.checkpoint,
            message: "Thử thách CAPTCHA đã được giải và preflight API thành công (HTTP 200)",
          });
        } else {
          console.warn("[bridge] ✘ Preflight failed after challenge disappeared:", evalRes.error || preflightRes?.status || "unusable ratings payload");
          resumeInFlightMap.set(jid, false);
          consecutiveAbsentCount = 0;

          if (classifyShopeeFailure(preflightRes) === "verification") {
            const verificationUrl = extractVerificationUrl(preflightRes) || details?.verification_url || details?.target_url || details?.url;
            if (verificationUrl && targetTabId && chrome.tabs?.update) {
              try {
                await chrome.tabs.update(targetTabId, { url: verificationUrl, active: true });
              } catch (err) {
                console.warn("[bridge] Could not navigate to Shopee verification URL:", err?.message || err);
              }
            }
            await triggerVerificationRequired({
              ...details,
              job_id: jid,
              tab_id: targetTabId,
              verification_url: verificationUrl,
              target_url: verificationUrl,
              reason: evalRes.error || formatShopeeFailure(preflightRes),
              kind: "verification",
              checkpoint: details?.checkpoint,
            });
            return;
          }

          if (!check?.resolved && (preflightRes?.status === 403 || evalRes.error?.includes("403"))) {
            const currentTab = await chrome.tabs.get(targetTabId).catch(() => null);
            const isStillVerify = currentTab?.url && (currentTab.url.includes("/verify/") || currentTab.url.includes("captcha"));
            if (!isStillVerify) {
              console.warn("[bridge] Tab has no challenge and is not on verify page, but API is 403 -> transitioning to api_blocked");
              stopVerificationWatcher();
              const reason = evalRes.error || "Shopee chặn API đánh giá (HTTP 403 / API Blocked)";
              updateState("api_blocked", {
                job_id: jid,
                tab_id: targetTabId,
                reason,
                kind: "api_blocked",
                checkpoint: details?.checkpoint,
              });
              if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
                bridgeSend({
                  v: 1,
                  type: "api_blocked",
                  id: "blocked-" + Date.now(),
                  params: {
                    job_id: jid,
                    status: 403,
                    reason,
                    checkpoint: details?.checkpoint,
                  },
                });
              }
            }
          }
        }
      }
    } else {
      consecutiveAbsentCount = 0;
      // Chế độ quan sát thụ động (Passive Handover): không tự ý drag bừa 240px làm hỏng CAPTCHA ghép hình.
      if (check?.slider?.isOrangeHandle) {
        console.log(`[bridge] Passive handover: Shopee orange slider button active at (${check.slider.x}, ${check.slider.y}). Chờ người dùng thao tác kéo...`);
      }
    }
  };

  const tabUpdateListener = async (tabId, changeInfo, tab) => {
    if (tabId !== targetTabId) return;
    const isVerify = tab.url && (tab.url.includes("/verify/") || tab.url.includes("anti_bot_tracking_id") || tab.url.includes("captcha"));
    if (changeInfo.status === "complete" || (tab.url && !isVerify)) {
      await performCheckAndPreflight();
    }
  };
  chrome.tabs.onUpdated?.addListener(tabUpdateListener);
  activeTabUpdateListener = tabUpdateListener;

  verificationWatcherInterval = setInterval(async () => {
    await performCheckAndPreflight();
  }, 2500);
}

async function triggerVerificationRequired(details) {
  console.warn("[bridge] ⚠️ Verification required:", details);
  const verificationUrl = details.verification_url || details.target_url || extractVerificationUrl(details) || details.url;
  details.verification_url = verificationUrl;
  details.target_url = verificationUrl;
  updateState(stateForVerification(details), details);

  // Human Handover: Focus & navigate target tab so user sees the verification UI immediately
  const targetTabId = details?.tab_id;
  if (targetTabId && chrome.tabs) {
    try {
      let tab = null;
      if (verificationUrl && verificationUrl.includes("/verify/traffic")) {
        tab = await chrome.tabs.update(targetTabId, { url: verificationUrl, active: true });
      } else {
        tab = await chrome.tabs.update(targetTabId, { active: true });
      }
      if (tab && tab.windowId && chrome.windows) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }
    } catch (e) {
      console.warn("[bridge] Could not focus/navigate tab for verification:", e?.message);
    }
  }

  // Evidence Capture: Capture screenshot of the challenge page if not already attached
  if (targetTabId && !details.evidence_screenshot) {
    details.evidence_screenshot = await captureTabEvidence(targetTabId);
  }

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

  if (details.kind === "verification") {
    // Căn giữa giao diện CAPTCHA một lần duy nhất lúc handover người dùng
    if (targetTabId) {
      try {
        await detectCaptchaInTab(targetTabId, { scrollIntoView: true });
      } catch {}
    }
    startVerificationWatcher(details);
  }
}

async function resumeVerification(jobId, options = {}) {
  stopVerificationWatcher();
  stopApiBlockedWatcher();
  if (verificationInfo && verificationInfo.kind === "login") {
    console.warn("[bridge] Không thể resume verification cho job yêu cầu đăng nhập:", jobId);
    return;
  }
  if (verificationInfo && verificationInfo.kind === "api_blocked" && !options.recheck) {
    console.warn("[bridge] Job đang ở trạng thái api_blocked, cần preflight recheckApi trước khi resume:", jobId);
    return;
  }

  let jobToResume = (jobId && pendingVerificationJobs.get(jobId)) || lastVerificationJob;
  const targetJobId = jobId || jobToResume?.id || (verificationInfo && verificationInfo.job_id);
  console.log("[bridge] Resuming verification, jobId:", targetJobId);

  // If not found in memory (e.g. after Service Worker idle/restart), recover from storage
  if (!jobToResume && targetJobId) {
    try {
      const stored = await chrome.storage.local.get([`pending_job_${targetJobId}`]);
      jobToResume = stored[`pending_job_${targetJobId}`] || null;
    } catch {}
  }

  if (targetJobId) {
    notifiedVerificationJobIds.delete(targetJobId);
  }
  updateState("connected");

  const cycle = options.verification_cycle || (targetJobId ? verificationCycles.get(targetJobId) || 1 : 1);
  const checkpoint = options.checkpoint || (targetJobId && verificationInfo?.checkpoint) || null;

  if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
    bridgeSend({
      v: 1,
      type: "verification.resolved",
      id: "resumed-" + Date.now(),
      params: {
        status: "resumed",
        message: options.message || "User confirmed verification in tab",
        jobId: targetJobId,
        job_id: targetJobId,
        verification_cycle: cycle,
        recheck: Boolean(options.recheck),
        checkpoint,
      },
    });
  }

  if (jobToResume) {
    console.log("[bridge] Retrying job after verification:", jobToResume.id);
    pendingVerificationJobs.delete(jobToResume.id);
    if (lastVerificationJob && lastVerificationJob.id === jobToResume.id) {
      lastVerificationJob = null;
    }
    if (targetJobId) {
      resumeInFlightMap.set(targetJobId, false);
      try {
        await chrome.storage.local.remove([`pending_job_${targetJobId}`]);
      } catch {}
    }
    knownJobs.delete(jobToResume.id);
    jobToResume.retry = true;
    if (checkpoint) {
      jobToResume.checkpoint = checkpoint;
    }
    enqueueLegacyJob(jobToResume);
  } else if (targetJobId) {
    resumeInFlightMap.set(targetJobId, false);
    // Fallback: construct minimal job from checkpoint/verificationInfo if missing
    const fallbackUrl = verificationInfo?.url;
    if (fallbackUrl) {
      const fallbackJob = {
        id: targetJobId,
        url: fallbackUrl,
        retry: true,
        checkpoint,
      };
      console.log("[bridge] Retrying with fallback job after verification:", fallbackJob.id);
      knownJobs.delete(targetJobId);
      enqueueLegacyJob(fallbackJob);
    }
  }
}

async function handleRecheckApi(jobId) {
  let job = (jobId && pendingVerificationJobs.get(jobId)) || lastVerificationJob;
  if (!job && jobId) {
    try {
      const stored = await chrome.storage.local.get([`pending_job_${jobId}`]);
      job = stored[`pending_job_${jobId}`] || null;
    } catch {}
  }
  if (!job && verificationInfo?.url) {
    job = {
      id: jobId || verificationInfo.job_id,
      url: verificationInfo.url,
      checkpoint: verificationInfo.checkpoint,
      _targetTabId: verificationInfo.tab_id,
    };
  }
  if (!job) {
    return { ok: false, error: "Không tìm thấy thông tin job để kiểm tra lại." };
  }

  let checkpoint = null;
  try {
    const stored = await chrome.storage.local.get([`checkpoint_${job.id}`]);
    checkpoint = stored[`checkpoint_${job.id}`] || job.checkpoint || null;
  } catch {}

  const parsed = idsFrom(job.url || "");
  const itemid = job.itemid || parsed.itemid || checkpoint?.itemid;
  const shopid = job._targetShopId || job.shopid || parsed.shopid || checkpoint?.shopid;

  let targetTabId = job._targetTabId;
  if (!targetTabId) {
    const tab = await findOrOpenShopeeTab(job.url, itemid);
    targetTabId = tab?.id;
    job._targetTabId = targetTabId;
  }

  if (!targetTabId) {
    return { ok: false, error: "Không tìm thấy hoặc không mở được tab Shopee." };
  }

  let preflightRes;
  try {
    preflightRes = await preflightRatingsInTab(targetTabId, itemid, shopid, job._targetTabUrl || job.url);
  } catch (err) {
    return { ok: false, error: `Lỗi kết nối preflight: ${err.message}` };
  }

  const evalRes = evaluatePreflightResult(preflightRes);
  const hasValidRatings = isUsableRatingsPreflight(preflightRes);

  if (hasValidRatings) {
    console.log("[bridge] Recheck API thành công (HTTP 200), resuming job:", job.id);
    updateState("resuming", { job_id: job.id, message: "Kiểm tra API thành công! Đang tiếp tục cào..." });
    resumeVerification(job.id, { recheck: true, checkpoint });
    return { ok: true, message: "Kiểm tra thành công (HTTP 200)! Đang tiếp tục cào từ checkpoint." };
  } else {
    const status = preflightRes?.status || "unknown";
    const reason = evalRes.error || formatShopeeFailure(preflightRes);
    console.warn(`[bridge] Recheck API thất bại (HTTP ${status}):`, reason);
    return {
      ok: false,
      status: preflightRes?.status,
      error: `API vẫn bị chặn (HTTP ${status}): ${reason}`,
    };
  }
}

// ── Action Registry Handlers (18 Precompiled Actions) ───────────────────────
async function handleAction(action, params) {
  params = params || {};
  switch (action) {
    case "browser.health":
      return {
        status: "ok",
        version: chrome.runtime.getManifest?.().version || "2.3.0",
        connected: bridgeSocket ? bridgeSocket.readyState === WebSocket.OPEN : false,
        activeJobsCount: activeJobs.size,
        activeJobIds: Array.from(activeJobs.keys()),
        queuedJobIds: jobQueue.map((entry) => entry?.job?.id).filter(Boolean),
        currentJobId: currentJobExecution?.job?.id || null,
        isQueueRunning,
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
      try {
        const tab = await chrome.tabs.create({ url: params.url, active: params.active !== false });
        return { tabId: tab.id, url: tab.url };
      } catch (err) {
        if (err.message.includes("No current window") || err.message.includes("window")) {
          const win = await chrome.windows.create({ url: params.url, focused: params.active !== false });
          const tab = (win.tabs && win.tabs[0]) || { id: null, url: params.url };
          return { tabId: tab.id, url: tab.url };
        }
        throw err;
      }
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

    case "page.screenshot": {
      const targetTabId = params.tabId || (await getActiveTabId());
      if (!targetTabId) throw new Error("No target tab available for screenshot");
      const tab = await chrome.tabs.get(targetTabId);
      if (!tab || !tab.windowId) throw new Error("Cannot locate tab window");
      if (params.active !== false && chrome.tabs && chrome.windows) {
        try {
          await chrome.tabs.update(targetTabId, { active: true });
          if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
          await new Promise((r) => setTimeout(r, 150));
        } catch {}
      }
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
        format: params.format === "jpeg" ? "jpeg" : "png",
        quality: params.quality,
      });
      return {
        status: "ok",
        tabId: targetTabId,
        url: tab.url,
        dataUrl,
      };
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
      await cleanupPendingJobState(jobId, { clearVerification: true });
      if (activeJobs.has(jobId)) {
        activeJobs.delete(jobId);
        if (currentJobExecution?.job?.id === jobId) {
          try { currentJobExecution.abortController?.abort(); } catch {}
          currentJobExecution = null;
        }
        for (let i = jobQueue.length - 1; i >= 0; i--) {
          if (jobQueue[i]?.job?.id === jobId) jobQueue.splice(i, 1);
        }
        if (activeJobs.size === 0 && jobQueue.length === 0) {
          isQueueRunning = false;
          updateState("connected");
          notifyQueueWaiters();
        }
        return { cancelled: true, jobId };
      }
      return { cancelled: false, message: "Job not running" };
    }

    case "job.clearStale": {
      const activeJobIds = Array.from(activeJobs.keys());
      const queuedJobIds = jobQueue.map((entry) => entry?.job?.id).filter(Boolean);
      try { currentJobExecution?.abortController?.abort(); } catch {}
      activeJobs.clear();
      jobQueue.length = 0;
      currentJobExecution = null;
      isQueueRunning = false;
      updateState("connected");
      notifyQueueWaiters();
      return { cleared: true, activeJobIds, queuedJobIds };
    }

    case "page.scroll": {
      const targetTabId = params.tabId || (await getActiveTabId());
      const res = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: (opts) => {
          const el = opts.selector ? document.querySelector(opts.selector) : null;
          const target = el || window;
          if (el) {
            el.scrollBy({
              top: opts.deltaY || 0,
              left: opts.deltaX || 0,
              behavior: opts.behavior || "smooth",
            });
          } else if (opts.top !== undefined || opts.left !== undefined) {
            window.scrollTo({
              top: opts.top !== undefined ? opts.top : window.scrollY,
              left: opts.left !== undefined ? opts.left : window.scrollX,
              behavior: opts.behavior || "smooth",
            });
          } else {
            window.scrollBy({
              top: opts.deltaY || 300,
              left: opts.deltaX || 0,
              behavior: opts.behavior || "smooth",
            });
          }
          return {
            scrollX: window.scrollX,
            scrollY: window.scrollY,
            scrollHeight: document.body.scrollHeight,
            clientHeight: document.documentElement.clientHeight,
          };
        },
        args: [{ selector: params.selector, top: params.top, left: params.left, deltaY: params.deltaY, deltaX: params.deltaX, behavior: params.behavior || "smooth" }],
      });
      return res[0]?.result || {};
    }

    case "extension.reload": {
      setTimeout(() => {
        try { chrome.runtime.reload(); } catch (e) { console.error("Reload failed:", e); }
      }, 150);
      return { reloading: true, version: "2.2.0" };
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

  if (envelope.type === "error") {
    const error = envelope.error || {};
    if (error.code === "CLIENT_ALREADY_CONNECTED") {
      connectionEnabled = false;
      clearReconnect();
      connectionConflict = {
        ...(error.details || {}),
        message: error.message || "Gateway đang được client khác sử dụng",
        at: Date.now(),
      };
      chrome.storage.local.set({
        connectionEnabled: false,
        connectionConflict,
        lastConnectionError: "Gateway đang được client khác sử dụng; hãy bấm Connect để takeover có chủ đích",
      });
      closeBridgeSocket({ preserveState: "client_conflict", rejectPending: true });
    } else {
      console.warn("[bridge] Gateway error:", error.code || "UNKNOWN", error.message || "");
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
    if (targetId) {
      await cleanupPendingJobState(targetId, { clearVerification: true });
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
    const params = envelope.params || {};
    resumeVerification(params.jobId || params.job_id || params.id, params);
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
    const job = envelope.job ? { ...envelope.job } : {};
    if (envelope.retry || envelope.job?.retry) job.retry = true;
    if (!job.id && envelope.id) job.id = envelope.id;
    traceJob(job, null, "job-dispatch", { sourceUrl: job.url });
    enqueueLegacyJob(job, "websocket");
    return;
  }

  // Extension reload message
  if (envelope.type === "extension.reload" || envelope.type === "system.reload") {
    console.log("[bridge] Reload requested via WebSocket. Reloading extension...");
    bridgeSend({
      v: 1,
      type: "accepted",
      id: envelope.id || ("reload-" + Date.now()),
      params: { reloading: true },
    });
    setTimeout(() => {
      try { chrome.runtime.reload(); } catch (e) { console.error("Reload failed:", e); }
    }, 150);
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

function closeBridgeSocket({ preserveDetails = false, preserveState = null, rejectPending = false } = {}) {
  // Transient disconnects preserve pending RPCs. Their stable request ids are
  // replayed after socket.onopen, so an active crawl survives a short outage.
  if (rejectPending) clearPendingIngestRequests("WebSocket closed");
  if (bridgeHandshakeTimeout) clearTimeout(bridgeHandshakeTimeout);
  bridgeHandshakeTimeout = null;
  if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
  bridgeHeartbeat = null;
  if (bridgeSocket) {
    const s = bridgeSocket;
    s.onopen = s.onmessage = s.onerror = s.onclose = null;
    if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
      try { s.close(1000, "Normal closure"); } catch {}
    }
  }
  bridgeSocket = null;
  if (preserveState) {
    updateState(preserveState, preserveState === "client_conflict" ? connectionConflict : verificationInfo);
  } else if (preserveDetails && verificationInfo) {
    extensionState = "disconnected";
    setBadge("");
    chrome.storage.local.set({
      extensionState,
      verificationInfo,
      connectionConflict,
      currentJob: activeJobs.size > 0 ? Array.from(activeJobs.values())[0] : null,
    });
  } else {
    updateState("disconnected");
  }
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
  if (bridgeSocket && bridgeSocket.readyState !== WebSocket.CLOSED) return;
  if (bridgeConnecting) return bridgeConnecting;
  const generation = connectionGeneration;
  const current = () => generation === connectionGeneration && connectionEnabled;
  const attempt = (async () => {
    try {
      const stored = await chrome.storage.local.get([
        "gatewayUrl",
        "pairingToken",
        "connectionEnabled",
        "clientId",
        "takeover",
      ]);
      if (!current()) return;
      if (stored.connectionEnabled === false) {
        connectionEnabled = false;
        closeBridgeSocket({ rejectPending: true });
        return;
      }
      let token = stored.pairingToken;
      let wsUrl = stored.gatewayUrl || "ws://127.0.0.1:8766/browser/v1/ws";
      const clientId = stored.clientId || crypto.randomUUID();
      const takeover = stored.takeover === true;
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
      await chrome.storage.local.set({
        gatewayUrl: wsUrl,
        pairingToken: token,
        clientId,
        takeover: false,
      });
      if (!current()) return;
      target.searchParams.set('token', token);
      target.searchParams.set('client_id', clientId);
      if (takeover) target.searchParams.set('takeover', '1');
      const socket = new WebSocket(target.toString());
      bridgeSocket = socket;
      const ownsSocket = () => current() && bridgeSocket === socket;
      const fail = message => {
        if (!ownsSocket()) return;
        connectionError(message);
        closeBridgeSocket({ preserveDetails: true });
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
        const restoredState = verificationInfo
          ? stateForVerification(verificationInfo)
          : (activeJobs.size ? "busy" : "connected");
        updateState(restoredState, verificationInfo);
        restorePendingVerificationWatchers().catch((error) => console.warn("[bridge] restore watcher failed:", error?.message || error));
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
      closeBridgeSocket({ preserveDetails: true });
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
  closeBridgeSocket({ rejectPending: true });
  bridgeConnecting = null;
  const enabled = message.action === "connect";
  const config = { connectionEnabled: enabled };
  if (enabled) {
    if (typeof message.url === "string") config.gatewayUrl = message.url.trim();
    if (typeof message.token === "string") config.pairingToken = message.token.trim();
    config.takeover = message.takeover === true;
  }
  const generation = connectionGeneration;
  await chrome.storage.local.set(config);
  if (generation !== connectionGeneration) return;
  connectionEnabled = enabled;
  if (enabled) await connectBridge();
}

// ── WebSocket Job Delivery ─────────────────────────────────────────────────
// ── Job Queue Executor ─────────────────────────────────────────────────────
const jobQueue = [];
let isQueueRunning = false;
let currentJobExecution = null;
let queueWaiters = [];

function notifyQueueWaiters() {
  if (!isQueueRunning && jobQueue.length === 0) {
    jobChain = Promise.resolve();
    const waiters = queueWaiters.slice();
    queueWaiters = [];
    for (const w of waiters) {
      try { w(); } catch {}
    }
  }
}

async function processJobQueue() {
  if (isQueueRunning) return;
  isQueueRunning = true;

  while (jobQueue.length > 0) {
    const queueItem = jobQueue.shift();
    const job = queueItem.job;
    const abortController = new AbortController();
    currentJobExecution = { job, abortController, startedAt: Date.now() };

    try {
      console.log("[bridge] Executor starting job from queue:", job.id);
      await runJob(job);
    } catch (err) {
      console.error("[bridge] Job execution error in queue:", job.id, err?.message || err);
    } finally {
      currentJobExecution = null;
      activeJobs.delete(job.id);
      if (knownJobs.size > 1000) knownJobs.delete(job.id);
    }
  }

  isQueueRunning = false;
  notifyQueueWaiters();
}

function enqueueLegacyJob(job) {
  if (!job || !job.id) return;
  bridgeSend({ v: 1, type: "accepted", id: job.id, jobId: job.id });
  traceJob(job, null, "job-accepted", { sourceUrl: job.url });

  // ACK every delivery, including a duplicate replay, so the server can release
  // its queue entry. Only the first non-active delivery is allowed to execute.
  if (activeJobs.has(job.id) || (currentJobExecution && currentJobExecution.job && currentJobExecution.job.id === job.id)) {
    console.log("[bridge] Job already active, skipping duplicate enqueue:", job.id);
    return;
  }
  if (jobQueue.some((item) => item.job && item.job.id === job.id)) {
    console.log("[bridge] Job already in queue, skipping duplicate enqueue:", job.id);
    return;
  }

  if (job.retry) {
    knownJobs.delete(job.id);
    notifiedVerificationJobIds.delete(job.id);
  }
  if (knownJobs.has(job.id)) return;
  knownJobs.add(job.id);

  jobQueue.push({ job });

  // Maintain jobChain promise for backwards compatibility with tests:
  jobChain = new Promise((resolve) => {
    queueWaiters.push(resolve);
  });

  processJobQueue().catch((err) => {
    console.error("[bridge] processJobQueue unexpected error:", err);
    isQueueRunning = false;
    notifyQueueWaiters();
  });
}

// ── Top-Level Listeners (MV3 Requirement) ───────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("bridge_heartbeat_alarm", { periodInMinutes: 1 });
  connectBridge();
});

chrome.runtime.onStartup.addListener(() => {
  connectBridge();
  restorePendingVerificationWatchers().catch(() => {});
});
chrome.alarms.onAlarm.addListener(() => {
  connectBridge();
  restorePendingVerificationWatchers().catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "getStatus") {
    chrome.storage.local.get([
      "gatewayUrl",
      "lastConnectionError",
      "lastCompletedResult",
    ], (stored) => {
      const socketConnected = Boolean(
        bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN,
      );
      sendResponse({
        ok: true,
        extensionState,
        connected: socketConnected,
        socketReadyState: bridgeSocket ? bridgeSocket.readyState : WebSocket.CLOSED,
        socketUrl: bridgeSocket?.url || null,
        gatewayUrl: stored.gatewayUrl || null,
        lastConnectionError: stored.lastConnectionError || "",
        verificationInfo,
        connectionConflict,
        currentJob: activeJobs.size > 0 ? Array.from(activeJobs.values())[0] : null,
        lastCompletedResult: lastCompletedResult || stored.lastCompletedResult || null,
        reconnectAttempts,
        lastBridgeMessageAt,
      });
    });
    return true;
  }
  if (msg.action === "clearCompletedResult") {
    lastCompletedResult = null;
    chrome.storage.local.remove("lastCompletedResult", () => {
      sendResponse({ ok: !chrome.runtime.lastError });
    });
    return true;
  }
  if (msg.action === "connect" || msg.action === "disconnect") {
    configureConnection(msg).then(() => sendResponse({ ok: true }), error => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (msg.action === "resumeVerification") {
    resumeVerification(msg.jobId);
    sendResponse({ ok: true });
    return true;
  }
  if (msg.action === "recheckApi") {
    handleRecheckApi(msg.jobId).then(
      (res) => sendResponse(res),
      (err) => sendResponse({ ok: false, error: err?.message || String(err) })
    );
    return true;
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
    isShopeeHostname,
    isValidShopeeUrl,
    findOrOpenShopeeTab,
    resolveShopId,
    preflightRatingsInTab,
    evaluatePreflightResult,
    isUsableRatingsPreflight,
    extractShopeeReviews,
    normaliseRating,
    reviewFingerprint,
    ratingTotal,
    RATING_TYPES,
    crawlPercent,
    classifyShopeeFailure,
    formatShopeeFailure,
    extractVerificationUrl,
    runJob,
    resumeVerification,
    handleRecheckApi,
    verificationCycles,
    resumeInFlightMap,
    triggerVerificationRequired,
    enqueueLegacyJob,
    jobQueue,
    processJobQueue,
    sendCheckpoint,
    clearPendingIngestRequests,
    executeScriptResultWithRetry,
    rememberCompletedResult,
    knownJobs,
    pendingVerificationJobs,
    notifiedVerificationJobIds,
    captureTabEvidence,
    detectCaptchaInTab,
    startVerificationWatcher,
    stopVerificationWatcher,
    startApiBlockedWatcher,
    stopApiBlockedWatcher,
    restorePendingVerificationWatchers,
    cleanupPendingJobState,
    probeFilterCandidates,
    normalizeCheckpointV2,
    extractRatingSummary,
    mergeReview,
  };
}
