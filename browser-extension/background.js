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
// 6. Backward-compatible Shopee reviews extraction and HTTP long-poll fallback.

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
let jobChain = Promise.resolve();
let extensionState = "disconnected";
let activeJobs = new Map();
let inFlightCommands = new Map();
let verificationInfo = null;

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
    if (!verificationInfo) {
      setBadge("OK", "#137333");
    } else {
      setBadge("PAUS", "#f9ab00");
    }
  } else if (newState === "busy") {
    setBadge("BUSY", "#1a73e8");
  } else {
    // disconnected
    if (!verificationInfo) {
      setBadge("");
    }
  }
  chrome.storage.local.set({
    extensionState: verificationInfo ? "awaiting_user_verification" : extensionState,
    verificationInfo,
    currentJob: activeJobs.size > 0 ? Array.from(activeJobs.values())[0] : null,
  });
}

// ── Ingest & Progress Helpers ────────────────────────────────────────────────
async function reportProgress(job, progress) {
  if (!job || !job.id) return;
  try {
    await fetch(`${HELPER}/ingest/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job: job.id, progress }),
    });
  } catch (e) {
    console.warn("[bridge] progress report failed:", e.message);
  }
}

function startKeepalive() {
  return setInterval(() => {
    fetch(`${HELPER}/ping`).catch(() => {});
  }, 20_000);
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

function idsFrom(url) {
  const u = new URL(url);
  const q = u.searchParams;
  const qItem = q.get("itemid") || q.get("item_id");
  if (qItem) return { shopid: q.get("shopid") || q.get("shop_id") || null, itemid: qItem };
  let m = u.pathname.match(/^\/product\/(\d+)\/(\d+)/) || u.href.match(/i\.(\d+)\.(\d+)/);
  if (m) return { shopid: m[1], itemid: m[2] };
  const tail = u.pathname.match(/\/(\d{6,})\/?$/);
  if (tail) return { shopid: null, itemid: tail[1] };
  const any = u.href.match(/[.\-/](\d{8,})(?:[?&#/]|$)/);
  return any ? { shopid: null, itemid: any[1] } : {};
}

async function resolveShopId(itemid, originalUrl, progress) {
  if (progress) await progress({ stage: "resolve-shop", message: "Đang tìm shopid", itemid, percent: 10 });
  function scrape(text) {
    const m = text.match(/"shopid"\s*:\s*"?(\d+)/) || text.match(/"shop_id"\s*:\s*"?(\d+)/);
    return m ? m[1] : null;
  }
  try {
    const r = await fetch(originalUrl, { headers: { "User-Agent": navigator.userAgent } });
    const s = scrape(await r.text());
    if (s) return s;
  } catch {}
  return null;
}

// Function injected and executed in the shopee.vn page context
async function inTabExtractShopeeRatings(itemid, shopid, offset, limit) {
  const url = `/api/v2/item/get_ratings?filter=0&flag=1&itemid=${itemid}&limit=${limit}&offset=${offset}&shopid=${shopid}&type=0`;
  try {
    const res = await fetch(url, {
      headers: {
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json"
      },
      credentials: "include"
    });
    if (!res.ok) {
      if (res.status === 403 || res.url.includes("/verify/traffic")) {
        return { ok: false, verificationRequired: true, status: res.status };
      }
      return { ok: false, status: res.status, error: "HTTP " + res.status };
    }
    const json = await res.json();
    if (json.error === 90309999 || (json.data && json.data.is_login === false)) {
      return { ok: false, verificationRequired: true, is_login: false, error: "Shopee verification / login required" };
    }
    return { ok: true, data: json.data };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// Function injected into the Shopee tab page context to scrape reviews from DOM
function inTabScrapeShopeePage() {
  const cardElements = Array.from(document.querySelectorAll(
    '.shopee-product-rating, [class*="shopee-product-rating__main"], .product-rating-overview ~ div .shopee-product-rating'
  ));
  
  const rawReviews = [];
  for (const item of cardElements) {
    if (item.closest('.product-rating-overview')) continue;

    // Tác giả
    const authorEl = item.querySelector('.shopee-product-rating__author-name') 
      || item.querySelector('[class*="author-name"]')
      || item.querySelector('a[href*="/shop/"]')
      || item.querySelector('a');
    const author = authorEl ? authorEl.textContent.trim() : "Khách hàng";

    // Số sao
    let stars = item.querySelectorAll('svg.icon-rating-solid, .icon-rating-solid, svg[class*="icon-rating-solid"]').length;
    if (!stars) {
      stars = item.querySelectorAll('svg[style*="fill: rgb(238, 77, 45)"], svg[fill="#ee4d2d"], svg[style*="fill:#ee4d2d"]').length;
    }
    if (!stars) {
      const ratingBox = item.querySelector('.shopee-product-rating__rating') || item.querySelector('[class*="rating__rating"]');
      if (ratingBox) stars = ratingBox.querySelectorAll('svg').length;
    }
    if (!stars || stars > 5) stars = 5;

    // Thời gian & phân loại
    const timeEl = item.querySelector('.shopee-product-rating__time') 
      || item.querySelector('[class*="rating__time"]')
      || item.querySelector('div[style*="color: rgba(0, 0, 0, 0.54)"]');
    const timeText = timeEl ? timeEl.textContent.trim() : "";
    
    let ctime = "";
    let variation = "";
    if (timeText) {
      const parts = timeText.split("|");
      ctime = parts[0].trim();
      if (parts.length > 1) {
        variation = parts.slice(1).join("|").replace(/Phân loại hàng:\s*/i, "").trim();
      }
    }

    // Nội dung bình luận
    const contentEl = item.querySelector('.shopee-product-rating__content') 
      || item.querySelector('div[style*="word-break"]')
      || item.querySelector('div[style*="pre-wrap"]');
    const comment = contentEl ? contentEl.textContent.replace(/\s+/g, ' ').trim() : "";

    // Ảnh đính kèm
    const imgEls = Array.from(item.querySelectorAll('.shopee-rating-image-list img, img[src*="susercontent"], img[class*="rating-image"]'));
    const imageUrls = [];
    for (const img of imgEls) {
      let src = img.getAttribute('src') || img.src || "";
      if (src && !src.startsWith("data:") && (src.includes("susercontent") || src.includes("shopee"))) {
        src = src.replace(/_tn$/, '').replace(/_tn\./, '.');
        if (!imageUrls.includes(src)) imageUrls.push(src);
      }
    }

    // Video đính kèm
    const videoEls = Array.from(item.querySelectorAll('video source[src], video[src]'));
    const videoUrls = [];
    for (const vid of videoEls) {
      const vsrc = vid.getAttribute('src') || vid.src || "";
      if (vsrc && !videoUrls.includes(vsrc)) videoUrls.push(vsrc);
    }

    if (author || comment || imageUrls.length) {
      rawReviews.push({
        author,
        stars,
        comment,
        ctime,
        variation,
        imageUrls,
        videoUrls,
      });
    }
  }

  // Tìm nút Next page
  const nextBtn = document.querySelector('button.shopee-icon-button--right')
    || document.querySelector('.shopee-page-controller button:last-child')
    || document.querySelector('button[aria-label="Next page"]')
    || document.querySelector('button[aria-label="Trang tiếp"]');

  let hasNext = false;
  if (nextBtn) {
    const isDisabled = nextBtn.disabled 
      || nextBtn.getAttribute('disabled') !== null 
      || nextBtn.classList.contains('shopee-button-solid--disabled')
      || nextBtn.getAttribute('aria-disabled') === 'true';
    if (!isDisabled) {
      hasNext = true;
      nextBtn.click();
    }
  }

  return { reviews: rawReviews, hasNext };
}

async function inTabExtractShopeeDOM(tabId, maxReviews, progress) {
  try {
    await chrome.tabs.update(tabId, { active: true });
  } catch {}

  if (progress) {
    await progress({
      stage: "dom-scroll",
      message: "Đang cuộn trang đến phần đánh giá sản phẩm...",
      percent: 25,
    });
  }

  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const el = document.querySelector('.product-ratings-comments-view')
        || document.querySelector('.product-ratings')
        || document.querySelector('.shopee-product-rating')
        || document.querySelector('[class*="product-rating"]');
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        window.scrollTo({ top: document.body.scrollHeight * 0.45, behavior: 'smooth' });
      }
    },
  });

  await new Promise((r) => setTimeout(r, 2500));

  const all = [];
  const seenKeys = new Set();
  let page = 1;
  const maxPages = 40;

  while (all.length < maxReviews && page <= maxPages) {
    let res = null;
    try {
      const [scriptRes] = await chrome.scripting.executeScript({
        target: { tabId },
        func: inTabScrapeShopeePage,
      });
      res = scriptRes && scriptRes.result;
    } catch (err) {
      console.warn("[bridge] inTabScrapeShopeePage execution error:", err.message);
      break;
    }

    if (!res || !res.reviews || res.reviews.length === 0) {
      if (page === 1) {
        await chrome.scripting.executeScript({
          target: { tabId },
          func: () => window.scrollBy({ top: 350, behavior: 'smooth' }),
        });
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const [retryRes] = await chrome.scripting.executeScript({
            target: { tabId },
            func: inTabScrapeShopeePage,
          });
          res = retryRes && retryRes.result;
        } catch {}
      }
      if (!res || !res.reviews || res.reviews.length === 0) {
        break;
      }
    }

    let addedCount = 0;
    for (const r of res.reviews) {
      const key = `${r.author}_${r.ctime}_${r.comment.slice(0, 30)}`;
      if (seenKeys.has(key)) continue;
      seenKeys.add(key);

      all.push({
        user: r.author || "Khách hàng",
        sao: r.stars || 5,
        noi_dung: (r.comment || "").replace(/\s+/g, " ").trim(),
        thoi_gian: r.ctime || new Date().toISOString().slice(0, 19).replace("T", " "),
        phan_loai: r.variation || "",
        anh: r.imageUrls.length ? 1 : 0,
        so_anh: r.imageUrls.length,
        anh_urls: r.imageUrls.join("|"),
        video: r.videoUrls.length ? 1 : 0,
        so_video: r.videoUrls.length,
        video_urls: r.videoUrls.join("|"),
        media_urls: [...r.imageUrls, ...r.videoUrls].join("|"),
        huu_ich: 0,
      });
      addedCount++;
    }

    if (progress) {
      await progress({
        stage: "dom-crawl",
        message: `Đang cào trang ${page} (đã thu thập ${all.length} đánh giá)`,
        rows: all.length,
        percent: Math.min(85, 30 + page * 2),
      });
    }

    if (!res.hasNext || addedCount === 0) {
      break;
    }

    page++;
    await new Promise((r) => setTimeout(r, 2000));
  }

  return all;
}

async function extractShopeeReviews(job, progress) {
  const parsed = idsFrom(job.url);
  let itemid = parsed.itemid;
  let shopid = parsed.shopid;
  if (!itemid) throw new Error(`Không đọc được itemid từ URL: ${job.url}`);

  // 1. Check for open Shopee tab
  let targetTab = null;
  try {
    const tabs = await chrome.tabs.query({});
    targetTab = tabs.find((t) => t.url && (t.url.includes(itemid) || (shopid && t.url.includes(shopid))));
    if (!targetTab) {
      targetTab = tabs.find((t) => t.url && t.url.includes("shopee.vn"));
    }
  } catch (err) {
    console.warn("[bridge] Error querying tabs:", err.message);
  }

  // 2. If no tab open, open product page in background
  if (!targetTab) {
    try {
      if (progress) await progress({ stage: "open-tab", message: "Đang mở tab Shopee...", percent: 10 });
      targetTab = await chrome.tabs.create({ url: job.url, active: false });
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, 12000);
        const onUpdated = (tId, info) => {
          if (tId === targetTab.id && info.status === "complete") {
            clearTimeout(timer);
            chrome.tabs.onUpdated.removeListener(onUpdated);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(onUpdated);
      });
    } catch (err) {
      console.warn("[bridge] Failed to create tab:", err.message);
    }
  }

  // 3. Resolve shopid: try tab DOM first, then fallback to HTTP resolve
  if (!shopid && targetTab && targetTab.id) {
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: targetTab.id },
        func: () => {
          const html = document.documentElement.innerHTML;
          const m = html.match(/"shopid"\s*:\s*"?(\d+)/) || html.match(/"shop_id"\s*:\s*"?(\d+)/);
          return m ? m[1] : null;
        },
      });
      if (res && res.result) shopid = res.result;
    } catch {}
  }

  if (!shopid) shopid = await resolveShopId(itemid, job.url, progress);
  if (!shopid) throw new Error(`Không tìm thấy shopid cho item ${itemid}`);

  let all = [];
  let offset = 0;
  let total = null;

  while (all.length < MAX_REVIEWS) {
    let result = null;

    // A. Preferred path: In-tab fetch with user cookies & anti-bot tokens
    if (targetTab && targetTab.id) {
      try {
        const [execRes] = await chrome.scripting.executeScript({
          target: { tabId: targetTab.id },
          func: inTabExtractShopeeRatings,
          args: [itemid, shopid, offset, PAGE_SIZE],
        });
        if (execRes && execRes.result) {
          result = execRes.result;
        }
      } catch (scriptErr) {
        console.warn("[bridge] In-tab execution failed, trying direct fetch:", scriptErr.message);
      }
    }

    // B. Fallback path: Background fetch with proper headers
    if (!result) {
      const apiUrl = `https://shopee.vn/api/v2/item/get_ratings?filter=0&flag=1&itemid=${itemid}&limit=${PAGE_SIZE}&offset=${offset}&shopid=${shopid}&type=0`;
      const res = await fetch(apiUrl, {
        headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
      });
      if (!res.ok) {
        if (res.status === 403 || res.url.includes("/verify/traffic")) {
          result = { ok: false, verificationRequired: true, status: res.status };
        } else {
          throw new Error(`Shopee API error HTTP ${res.status}`);
        }
      } else {
        const json = await res.json();
        if (json.error === 90309999 || (json.data && json.data.is_login === false)) {
          result = { ok: false, verificationRequired: true, error: "Shopee error 90309999 (is_login=false)" };
        } else {
          result = { ok: true, data: json.data };
        }
      }
    }

    if (!result.ok) {
      // If API is blocked (e.g. is_login=false, error 90309999, 403), seamlessly fallback to in-tab DOM extraction
      if (targetTab && targetTab.id) {
        if (progress) {
          await progress({
            stage: "dom-crawl",
            message: "Shopee API yêu cầu đăng nhập. Tự động chuyển sang cào trực tiếp từ DOM của tab...",
            percent: 30,
          });
        }
        try {
          const domResults = await inTabExtractShopeeDOM(targetTab.id, MAX_REVIEWS, progress);
          if (domResults && domResults.length > 0) {
            return domResults;
          }
        } catch (domErr) {
          console.warn("[bridge] In-tab DOM extraction failed:", domErr.message);
        }
      }

      if (result.verificationRequired) {
        // Bring tab to front so user can easily solve challenge or log in
        if (targetTab && targetTab.id) {
          try {
            await chrome.tabs.update(targetTab.id, { active: true });
            const tabObj = await chrome.tabs.get(targetTab.id);
            if (tabObj.windowId) await chrome.windows.update(tabObj.windowId, { focused: true });
          } catch {}
        }
        throw new Error("Shopee challenge / verification required");
      }
      throw new Error(result.error || `Shopee extraction failed with status ${result.status}`);
    }

    const ratings = (result.data && result.data.ratings) || [];
    if (!ratings.length) break;
    if (total === null) total = ratingTotal(result);

    for (const r of ratings) all.push(normaliseRating(r));
    if (progress) {
      await progress({
        stage: "fetch",
        message: `Đã lấy ${all.length} đánh giá`,
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
  const keepalive = startKeepalive();

  const progress = (patch) => reportProgress(job, patch);
  try {
    await progress({ status: "running", stage: "init", message: "Bắt đầu cào", percent: 5 });
    let rows = [];
    if (job.kind === "shopee-reviews" || job.url.includes("shopee.vn")) {
      rows = await extractShopeeReviews(job, progress);
    }
    await progress({ status: "saving", stage: "upload", message: `Đang lưu ${rows.length} dòng`, rows: rows.length, percent: 95 });
    await fetch(`${HELPER}/ingest?name=${encodeURIComponent("shopee_" + job.id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job: job.id, source: job.url, rows }),
    });
    console.log("[bridge] ✔ job", job.id, "finished:", rows.length, "rows");
  } catch (e) {
    console.error("[bridge] ✘ job", job.id, "failed:", e.message);
    if (e.message.includes("verification required") || e.message.includes("is_login=false")) {
      triggerVerificationRequired({ job_id: job.id, url: job.url, reason: e.message });
    }
    await progress({ status: "error", stage: "failed", message: String(e.message || e), percent: 100 });
    await fetch(`${HELPER}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job: job.id, error: String(e.message || e) }),
    });
  } finally {
    clearInterval(keepalive);
    activeJobs.delete(job.id);
    updateState(verificationInfo ? "awaiting_user_verification" : (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN ? "connected" : "disconnected"));
  }
}

// ── Verification Challenge Trigger ──────────────────────────────────────────
function triggerVerificationRequired(details) {
  console.warn("[bridge] ⚠️ Verification required:", details);
  updateState("awaiting_user_verification", details);
  if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
    bridgeSend({
      v: 1,
      type: "verification.required",
      id: "verif-" + Date.now(),
      params: details,
    });
  }
}

function resumeVerification() {
  console.log("[bridge] Resuming verification");
  verificationInfo = null;
  updateState("connected");
  if (bridgeSocket && bridgeSocket.readyState === WebSocket.OPEN) {
    bridgeSend({
      v: 1,
      type: "verification.resolved",
      id: "resumed-" + Date.now(),
      params: { status: "resumed", message: "User confirmed verification in tab" },
    });
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
    resumeVerification();
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

function closeBridgeSocket() {
  if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
  bridgeHeartbeat = null;
  if (bridgeSocket) {
    const s = bridgeSocket;
    bridgeSocket = null;
    s.onclose = null;
    s.onerror = null;
    s.onmessage = null;
    try {
      if (s.readyState === WebSocket.OPEN || s.readyState === WebSocket.CONNECTING) {
        s.close(1000, "Normal closure");
      }
    } catch {}
  }
  updateState("disconnected");
}

function scheduleBridgeReconnect() {
  if (bridgeReconnectTimeout) return;
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  reconnectAttempts++;
  // Exponential backoff with random jitter (1s, 1.5s, 2.25s ... capped at 30s)
  const baseDelay = Math.min(30000, 1000 * Math.pow(1.5, Math.min(reconnectAttempts, 8)));
  const jitter = Math.floor(Math.random() * 1000);
  const delay = baseDelay + jitter;

  console.log(`[bridge] Scheduling reconnect in ${(delay / 1000).toFixed(1)}s (attempt ${reconnectAttempts})`);
  bridgeReconnectTimeout = setTimeout(() => {
    bridgeReconnectTimeout = null;
    connectBridge();
  }, delay);
}

async function fetchPairingToken() {
  try {
    const extId = chrome.runtime.id || "";
    const pairResponse = await fetch(`${HELPER}/browser/pair?ext_id=${encodeURIComponent(extId)}`, {
      headers: { "X-Extension-Id": extId },
      cache: "no-store",
    });
    if (!pairResponse.ok) throw new Error(`pair HTTP ${pairResponse.status}`);
    const pair = await pairResponse.json();
    if (pair && pair.ok && pair.token) {
      await chrome.storage.local.set({
        gatewayUrl: pair.wsUrl || `${HELPER.replace("http", "ws")}/browser/v1/ws`,
        fallbackWsUrl: pair.fallbackWsUrl || "ws://127.0.0.1:8767/browser-extension",
        pairingToken: pair.token,
      });
      return pair;
    }
  } catch (err) {
    console.warn("[bridge] Auto-pairing request failed:", err.message);
  }
  return null;
}

async function connectBridge(overrideUrl, overrideToken) {
  if (bridgeSocket && (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (bridgeConnecting) return bridgeConnecting;

  bridgeConnecting = (async () => {
    try {
      let stored = await chrome.storage.local.get(["gatewayUrl", "fallbackWsUrl", "pairingToken"]);
      let token = overrideToken || stored.pairingToken;
      let wsUrl = overrideUrl || stored.gatewayUrl;

      // Auto-pair or refresh token if missing or after reconnection attempts
      if (!token || reconnectAttempts > 0) {
        const pair = await fetchPairingToken();
        if (pair && pair.token) {
          token = pair.token;
          wsUrl = pair.wsUrl || wsUrl;
        }
      }

      if (!wsUrl) wsUrl = "ws://127.0.0.1:8766/browser/v1/ws";
      const fullUrl = `${wsUrl}${wsUrl.includes("?") ? "&" : "?"}token=${encodeURIComponent(token || "")}`;

      const socket = new WebSocket(fullUrl);
      bridgeSocket = socket;
      let opened = false;

      socket.onopen = () => {
        opened = true;
        console.log("[bridge] WebSocket connected to", wsUrl);
        if (bridgeReconnectTimeout) {
          clearTimeout(bridgeReconnectTimeout);
          bridgeReconnectTimeout = null;
        }
        reconnectAttempts = 0;
        updateState("connected");

        // Authenticate envelope
        bridgeSend({
          v: 1,
          type: "authenticate",
          id: "auth-" + Date.now(),
          auth: { token },
        });

        // 20s Heartbeat keeps MV3 worker active
        if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
        bridgeHeartbeat = setInterval(() => {
          bridgeSend({
            v: 1,
            type: "ping",
            id: "ping-" + Date.now(),
            params: { at: Date.now() },
          });
        }, 20000);
      };

      socket.onmessage = (event) => {
        let envelope;
        try {
          envelope = JSON.parse(event.data);
        } catch {
          return;
        }
        if (envelope && envelope.type === "error" && envelope.error && envelope.error.code === "AUTHENTICATION_FAILED") {
          console.warn("[bridge] Authentication failed, clearing stale token");
          chrome.storage.local.remove("pairingToken");
          closeBridgeSocket();
          scheduleBridgeReconnect();
          return;
        }
        dispatchEnvelope(envelope);
      };

      socket.onerror = (err) => {
        console.warn("[bridge] WebSocket error:", err);
      };

      socket.onclose = (ev) => {
        console.log("[bridge] WebSocket disconnected", ev.code, ev.reason);
        if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
        bridgeHeartbeat = null;
        if (bridgeSocket === socket) {
          bridgeSocket = null;
          updateState("disconnected");
        }
        // If connection was never opened or rejected with auth codes, clear pairing token
        if (!opened || ev.code === 4001 || ev.code === 4003) {
          console.warn("[bridge] Connection not opened or authentication rejected, clearing pairing token");
          chrome.storage.local.remove("pairingToken");
        }
        if (!opened && stored.fallbackWsUrl && wsUrl !== stored.fallbackWsUrl) {
          chrome.storage.local.set({ gatewayUrl: stored.fallbackWsUrl });
        }
        fallbackHttpLoop();
        scheduleBridgeReconnect();
      };
    } catch (error) {
      console.warn("[bridge] WebSocket unavailable, falling back to HTTP:", error.message);
      if (bridgeHeartbeat) clearInterval(bridgeHeartbeat);
      bridgeHeartbeat = null;
      if (bridgeSocket) {
        try {
          if (bridgeSocket.readyState === WebSocket.OPEN || bridgeSocket.readyState === WebSocket.CONNECTING) {
            bridgeSocket.close();
          }
        } catch {}
        bridgeSocket = null;
      }
      updateState("disconnected");
      fallbackHttpLoop();
      scheduleBridgeReconnect();
    }
  })().finally(() => {
    bridgeConnecting = null;
  });

  return bridgeConnecting;
}

// ── HTTP Long-Poll Fallback ──────────────────────────────────────────────────
let looping = false;
function enqueueLegacyJob(job, transport) {
  if (!job || !job.id) return;
  if (transport === "websocket") {
    bridgeSend({
      v: 1,
      type: "accepted",
      id: job.id,
      jobId: job.id,
      params: { kind: "legacy.ingest.job", jobId: job.id },
    });
  }
  jobChain = jobChain.then(() => runJob(job)).catch((err) => console.error("[bridge] Job failed:", err));
}

async function fallbackHttpLoop() {
  if (looping) return;
  looping = true;
  try {
    while (!bridgeSocket || bridgeSocket.readyState !== WebSocket.OPEN) {
      let jobs = [];
      try {
        const r = await fetch(`${HELPER}/ingest/jobs?wait=25`);
        jobs = (await r.json()).jobs || [];
      } catch {
        await new Promise((s) => setTimeout(s, 5000));
        return;
      }
      for (const job of jobs) enqueueLegacyJob(job, "http");
    }
  } finally {
    looping = false;
  }
}

// ── Top-Level Listeners (MV3 Requirement) ───────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("bridge_heartbeat_alarm", { periodInMinutes: 1 });
  connectBridge();
});

chrome.runtime.onStartup.addListener(connectBridge);
chrome.alarms.onAlarm.addListener(connectBridge);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "connect") {
    closeBridgeSocket();
    connectBridge(msg.url, msg.token);
    sendResponse({ ok: true });
  } else if (msg.action === "autoPair") {
    closeBridgeSocket();
    fetchPairingToken().then((pair) => {
      if (pair) {
        connectBridge(pair.wsUrl, pair.token);
        sendResponse({ ok: true, token: pair.token, wsUrl: pair.wsUrl });
      } else {
        sendResponse({ ok: false, error: "Auto-pair failed" });
      }
    });
    return true;
  } else if (msg.action === "disconnect") {
    closeBridgeSocket();
    sendResponse({ ok: true });
  } else if (msg.action === "resumeVerification") {
    resumeVerification();
    sendResponse({ ok: true });
  }
  return true;
});

// Auto-start
connectBridge();
