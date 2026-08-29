// AI cowork ingest worker.
//
// Why an extension and not the agent's Playwright browser: Shopee flags a CDP-driven
// browser on the very first request — measured 2026-08-08, both bundled Chromium and
// real Chrome land on /verify/traffic/error even before login, and even when the visit
// starts at the homepage. Requests issued from here carry this profile's ordinary
// cookies and no automation surface, so the site sees a normal session.
//
// Flow: agent → GET helper /ingest/job?url=… → this worker long-polls /ingest/jobs →
// fetches the site's own API → POSTs rows back to /ingest → helper writes CSV + JSON.

const HELPER = "http://127.0.0.1:8766";
const PAGE_SIZE = 50;
const MAX_REVIEWS = 20000; // emergency cap; normal completion is batch < PAGE_SIZE.
const PACE_MS = 700; // between pages — a burst is what gets a session flagged

async function reportProgress(job, progress) {
  if (!job || !job.id) return;
  try {
    await fetch(`${HELPER}/ingest/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job: job.id, progress }),
    });
  } catch (e) {
    console.warn("[ingest] progress report failed:", e.message);
  }
}

// ── FIX A: keepalive ──────────────────────────────────────────────────────────
// MV3 service workers are killed when idle (no pending fetch). We keep the worker
// alive during a long job by pinging /ping every 20 s. The interval is cleared in
// the finally block, so a crash still lets Chrome eventually GC the worker.
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
  const base = kind === "video" ? "https://down-vn.img.susercontent.com/file/" : "https://down-vn.img.susercontent.com/file/";
  return base + value;
}

function asList(value) {
  if (Array.isArray(value)) return value;
  return value ? [value] : [];
}

function normaliseRating(x) {
  const imageUrls = asList(x.images).map((v) => mediaUrl(v, "image")).filter(Boolean);
  const videoUrls = asList(x.videos || x.video)
    .map((v) => mediaUrl(v, "video"))
    .filter(Boolean);
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
  // Explicit ids in the query string — the agent sometimes queues API-style urls like
  // /api/v2/item/get_ratings?itemid=…&shopid=… instead of the plain product page.
  const q = u.searchParams;
  const qItem = q.get("itemid") || q.get("item_id");
  if (qItem) return { shopid: q.get("shopid") || q.get("shop_id") || null, itemid: qItem };
  let m = u.pathname.match(/^\/product\/(\d+)\/(\d+)/) || u.href.match(/i\.(\d+)\.(\d+)/);
  if (m) return { shopid: m[1], itemid: m[2] };
  // /<shopname>/<itemid>: the shop id is not in the URL, so ask Shopee for it.
  const tail = u.pathname.match(/\/(\d{6,})\/?$/);
  if (tail) return { shopid: null, itemid: tail[1] };
  // Fallback: grab any 8+ digit number from the URL (itemid is always long)
  const any = u.href.match(/[.\-/](\d{8,})(?:[?&#/]|$)/);
  return any ? { shopid: null, itemid: any[1] } : {};
}

async function resolveShopId(itemid, originalUrl, progress) {
  console.log("[ingest] resolveShopId: item", itemid, "url", originalUrl);
  if (progress) await progress({ stage: "resolve-shop", message: "Đang tìm shopid", itemid, percent: 10 });

  // ── helper: pull shopid out of any string blob ──────────────────────
  function scrape(text) {
    const m =
      text.match(/"shopid"\s*:\s*"?(\d+)/) ||
      text.match(/"shop_id"\s*:\s*"?(\d+)/) ||
      text.match(/shopid=(\d+)/);
    return m ? m[1] : null;
  }

  // ── Method 1: PDP API (fastest, try twice with a pause) ─────────────
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(
        `https://shopee.vn/api/v4/pdp/get_pc?item_id=${itemid}&detail_level=0`,
        { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
      );
      const j = await r.json().catch(() => null);
      console.log("[ingest] pdp attempt", attempt, "→",
        j ? JSON.stringify(j).slice(0, 500) : "(parse failed)");
      if (j && j.data) {
        const d = j.data.item || j.data;
        const sid = d && (d.shop_id || d.shopid);
        if (sid) {
          if (progress) await progress({ stage: "resolve-shop", message: "Đã tìm shopid qua PDP API", shopid: String(sid), percent: 18 });
          return String(sid);
        }
      }
      if (j && j.error) console.warn("[ingest] pdp error:", j.error);
    } catch (e) {
      console.warn("[ingest] pdp fetch error:", e.message);
    }
    if (attempt === 0) await new Promise((s) => setTimeout(s, 1200));
  }

  // ── Method 2: fetch product page(s) HTML, scrape embedded JSON ──────
  const pagesToTry = [
    originalUrl,                              // the real URL the user gave
    `https://shopee.vn/-i.0.${itemid}`,       // manufactured canonical-style URL
  ].filter(Boolean);
  for (const pageUrl of pagesToTry) {
    try {
      console.log("[ingest] HTML scrape:", pageUrl);
      const r = await fetch(pageUrl, {
        credentials: "include",
        redirect: "follow",
      });
      // Check if Shopee redirected to a canonical URL containing shopid
      const rm = r.url.match(/i\.(\d+)\.(\d+)/);
      if (rm && rm[2] === itemid) {
        console.log("[ingest] shopid from redirect:", rm[1]);
        if (progress) await progress({ stage: "resolve-shop", message: "Đã tìm shopid qua redirect", shopid: rm[1], percent: 18 });
        return rm[1];
      }
      const html = await r.text();
      const sid = scrape(html);
      if (sid) {
        console.log("[ingest] shopid from HTML:", sid);
        if (progress) await progress({ stage: "resolve-shop", message: "Đã tìm shopid trong HTML", shopid: sid, percent: 18 });
        return sid;
      }
      console.warn("[ingest] no shopid in HTML of", r.url);
    } catch (e) {
      console.warn("[ingest] HTML error for", pageUrl, ":", e.message);
    }
  }

  // ── Method 3: item/get API with shopid=0 ────────────────────────────
  try {
    console.log("[ingest] trying item/get shopid=0…");
    const r = await fetch(
      `https://shopee.vn/api/v2/item/get?itemid=${itemid}&shopid=0`,
      { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
    );
    const j = await r.json().catch(() => null);
    console.log("[ingest] item/get →",
      j ? JSON.stringify(j).slice(0, 500) : "(parse failed)");
    const d = j && (j.item || (j.data && j.data));
    const sid = d && (d.shopid || d.shop_id);
    if (sid) {
      if (progress) await progress({ stage: "resolve-shop", message: "Đã tìm shopid qua item API", shopid: String(sid), percent: 18 });
      return String(sid);
    }
  } catch (e) {
    console.warn("[ingest] item/get error:", e.message);
  }

  // ── Method 4 (nuclear): open a real tab, let JS render, read DOM ────
  // This works when fetch-based methods fail because Shopee's CSR hydrates
  // the shopid into the page state only after JavaScript runs.
  let tabId;
  try {
    const tabUrl = originalUrl || `https://shopee.vn/-i.0.${itemid}`;
    console.log("[ingest] opening tab:", tabUrl);
    const tab = await chrome.tabs.create({ url: tabUrl, active: false });
    tabId = tab.id;

    // Wait for 'complete' with a 15-second timeout
    await Promise.race([
      new Promise((resolve) => {
        const onUp = (id, info) => {
          if (id === tabId && info.status === "complete") {
            chrome.tabs.onUpdated.removeListener(onUp);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(onUp);
      }),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("tab load timeout")), 15000)
      ),
    ]);

    // Give CSR a moment to hydrate
    await new Promise((s) => setTimeout(s, 3000));

    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        // 1) canonical URL might now contain i.<shopid>.<itemid>
        const um = location.href.match(/i\.(\d+)\.(\d+)/);
        if (um) return um[1];
        const pm = location.pathname.match(/\/product\/(\d+)\/(\d+)/);
        if (pm) return pm[1];
        // 2) scrape embedded JSON from the rendered DOM
        const html = document.documentElement.innerHTML;
        const hm =
          html.match(/"shopid"\s*:\s*"?(\d+)/) ||
          html.match(/"shop_id"\s*:\s*"?(\d+)/) ||
          html.match(/shopid=(\d+)/);
        return hm ? hm[1] : null;
      },
    });

    const sid = results && results[0] && results[0].result;
    if (sid) {
      console.log("[ingest] shopid from tab:", sid);
      if (progress) await progress({ stage: "resolve-shop", message: "Đã tìm shopid bằng tab thật", shopid: String(sid), percent: 18 });
      chrome.tabs.remove(tabId).catch(() => {});
      return String(sid);
    }
    console.warn("[ingest] tab method: no shopid found in rendered page");
  } catch (e) {
    console.warn("[ingest] tab method error:", e.message);
  }
  if (tabId) chrome.tabs.remove(tabId).catch(() => {});

  console.error("[ingest] ALL 4 methods failed for item", itemid);
  return null;
}

async function collectReviews(url, progress) {
  console.log("[ingest] collectReviews:", url);
  let { shopid, itemid } = idsFrom(url);
  console.log("[ingest] parsed ids → shopid:", shopid, "itemid:", itemid);
  if (!itemid) throw new Error("không đọc được itemid từ " + url);
  if (progress) await progress({ stage: "parse-url", message: "Đã đọc itemid từ URL", itemid, shopid, percent: shopid ? 18 : 8 });
  if (!shopid) shopid = await resolveShopId(itemid, url, progress);
  if (!shopid) throw new Error("không đọc được shopid cho item " + itemid);
  if (progress) await progress({ stage: "fetch-background", message: "Đang lấy review bằng background fetch", itemid, shopid, percent: 20 });

  // ── Try 1: fetch ratings from the background service worker ─────────
  try {
    const rows = await _fetchRatingsBackground(shopid, itemid, progress);
    return { itemid, rows };
  } catch (bgErr) {
    console.warn("[ingest] background fetch failed:", bgErr.message);
    if (progress) await progress({ stage: "fallback-tab", message: "Background fetch lỗi, chuyển qua tab thật: " + bgErr.message, itemid, shopid, percent: 25 });
    // ── FIX B: mở rộng fallback condition ──────────────────────────────
    // Trước đây chỉ catch 403|429|Shopee error — thiếu các lỗi network/worker
    // bị kill (TypeError, Failed to fetch, NetworkError, v.v.)
    // Giờ: fallback về tab với MỌI lỗi để tránh bỏ sót.
    console.log("[ingest] falling back to tab (any error triggers fallback)");
  }

  // ── Try 2: open a real tab on shopee.vn and fetch from page context ─
  console.log("[ingest] falling back to tab-based ratings fetch…");
  return await _fetchRatingsViaTab(shopid, itemid, url, progress);
}

// Background fetch — fast when Shopee trusts the service-worker origin.
async function _fetchRatingsBackground(shopid, itemid, progress) {
  const rows = [];
  let total = null;
  for (let offset = 0; offset < MAX_REVIEWS; offset += PAGE_SIZE) {
    const r = await fetch(
      `https://shopee.vn/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}` +
        `&type=0&filter=0&limit=${PAGE_SIZE}&offset=${offset}`,
      { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
    );
    if (!r.ok) throw new Error(`HTTP ${r.status} ở offset ${offset}`);
    const j = await r.json();
    if (j && j.error) throw new Error(`Shopee error ${j.error} (is_login=${j.is_login})`);
    total = total || ratingTotal(j);
    const batch = (j.data && j.data.ratings) || [];
    for (const x of batch) {
      rows.push(normaliseRating(x));
    }
    setBadge(String(rows.length));
    if (progress) {
      const percent = crawlPercent(rows.length, total, 25, 70);
      await progress({
        status: "running",
        stage: "fetch-background",
        message: `Đã lấy ${rows.length} review`,
        rows: rows.length,
        total,
        offset,
        itemid,
        shopid,
        percent,
      });
    }
    if (batch.length < PAGE_SIZE) break;
    await new Promise((s) => setTimeout(s, PACE_MS));
  }
  return rows;
}

// ── FIX C: Tab fetch với inject + polling thay vì async executeScript ─────────
//
// VẤN ĐỀ CŨ: chrome.scripting.executeScript với `func: async () => { ... }` KHÔNG
// await async function — nó chỉ nhận Promise object (unresolved), không phải giá trị
// thực → data luôn là undefined → tab đóng ngay, không lấy được review nào.
//
// CÁCH FIX: inject một script đồng bộ vào page, script đó tự khởi động async crawl
// và ghi kết quả vào window.__ingestResult. Background poll window.__ingestResult
// mỗi 2 giây cho đến khi có data hoặc timeout.
async function _fetchRatingsViaTab(fallbackShopid, fallbackItemid, productUrl, progress) {
  const tab = await chrome.tabs.create({ url: productUrl, active: true });
  const tabId = tab.id;
  if (progress) await progress({ status: "running", stage: "tab-opened", message: "Đã mở tab Chrome thật để crawl", tabId, percent: 30 });

  try {
    // Wait for the tab to finish loading
    await Promise.race([
      new Promise((resolve) => {
        const onUp = (id, info) => {
          if (id === tabId && info.status === "complete") {
            chrome.tabs.onUpdated.removeListener(onUp);
            resolve();
          }
        };
        chrome.tabs.onUpdated.addListener(onUp);
      }),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("tab load timeout")), 20000)
      ),
    ]);
    // Give CSR time to hydrate — reviews won't be in the DOM without this.
    if (progress) await progress({ status: "running", stage: "tab-loaded", message: "Tab đã load, chờ hydrate dữ liệu", tabId, percent: 35 });
    await new Promise((s) => setTimeout(s, 3000));

    // ── Step 1: inject script khởi động crawl (không dùng async func) ──
    // Inject một IIFE đồng bộ vào page. IIFE đó kick-off async crawl ngầm
    // và ghi kết quả vào window.__ingestResult khi xong.
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [fallbackShopid, fallbackItemid],
      func: (fbShopid, fbItemid) => {
        // Reset trạng thái cho lần chạy này
        window.__ingestResult = undefined;  // undefined = chưa xong; null = lỗi
        window.__ingestProgress = { rows: 0, offset: 0, shopid: fbShopid, itemid: fbItemid };

        let box = document.getElementById("__aiCoworkIngestProgress");
        if (!box) {
          box = document.createElement("div");
          box.id = "__aiCoworkIngestProgress";
          box.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;"
            + "max-width:360px;background:#111827;color:#f9fafb;font:13px system-ui;"
            + "padding:12px 14px;border-radius:8px;box-shadow:0 12px 28px rgba(0,0,0,.35);"
            + "border:1px solid rgba(255,255,255,.12)";
          document.documentElement.appendChild(box);
        }
        const say = (text) => {
          box.textContent = text;
        };
        say("AI cowork: bắt đầu crawl review...");

        // Kick-off async crawl — KHÔNG await ở đây (executeScript không đợi được)
        (function startCrawl() {
          // ── Extract shopid + itemid từ page đã load ──────────────────
          let shopid, itemid;
          let m = location.pathname.match(/\/product\/(\d+)\/(\d+)/);
          if (m) { shopid = m[1]; itemid = m[2]; }
          if (!shopid || !itemid) {
            m = location.href.match(/i\.(\d+)\.(\d+)/);
            if (m) { shopid = m[1]; itemid = m[2]; }
          }
          if (!shopid || !itemid) {
            const html = document.documentElement.innerHTML;
            shopid = shopid || (html.match(/"shopid"\s*:\s*"?(\d+)/) || [])[1];
            itemid = itemid || (html.match(/"itemid"\s*:\s*"?(\d+)/) || [])[1];
          }
          shopid = shopid || fbShopid;
          itemid = itemid || fbItemid;

          if (!shopid || !itemid) {
            window.__ingestResult = { error: "no shopid/itemid on page: " + location.href };
            say("AI cowork: lỗi đọc shopid/itemid");
            return;
          }
          say(`AI cowork: đang lấy review cho item ${itemid}`);

          // ── Paginate ratings API ─────────────────────────────────────
          const rows = [];
          let offset = 0;
          const PAGE = 50;
          const MAX = 20000;
          const PACE = 700;
          let total = null;

          function mediaUrl(value, kind = "image") {
            if (!value) return "";
            if (typeof value === "object") {
              value = value.url || value.video_url || value.play_url || value.cover || value.image_id || value.id || "";
            }
            value = String(value || "").trim();
            if (!value) return "";
            if (value.startsWith("//")) return "https:" + value;
            if (/^https?:\/\//i.test(value)) return value;
            return "https://down-vn.img.susercontent.com/file/" + value;
          }

          function asList(value) {
            if (Array.isArray(value)) return value;
            return value ? [value] : [];
          }

          function normaliseRating(x) {
            const imageUrls = asList(x.images).map((v) => mediaUrl(v, "image")).filter(Boolean);
            const videoUrls = asList(x.videos || x.video)
              .map((v) => mediaUrl(v, "video"))
              .filter(Boolean);
            const mediaUrls = [...imageUrls, ...videoUrls];
            return {
              user: x.author_username || "",
              sao: x.rating_star,
              noi_dung: (x.comment || "").replace(/\s+/g, " ").trim(),
              thoi_gian: new Date((x.ctime || 0) * 1000)
                .toISOString().slice(0, 19).replace("T", " "),
              phan_loai: (x.product_items || [])
                .map((p) => p.model_name).filter(Boolean).join("|"),
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
            const value = data && (data.total || data.count || (summary && (summary.rating_total || summary.total_count || summary.count)));
            const n = Number(value);
            return Number.isFinite(n) && n > 0 ? n : null;
          }

          function fetchPage() {
            fetch(
              `/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}` +
                `&type=0&filter=0&limit=${PAGE}&offset=${offset}`
            )
              .then((r) => {
                if (!r.ok) {
                  window.__ingestResult = { error: `HTTP ${r.status} at offset ${offset}`, shopid, itemid };
                  say(`AI cowork: lỗi HTTP ${r.status} tại offset ${offset}`);
                  return;
                }
                return r.json();
              })
              .then((j) => {
                if (!j) return; // error already set above
                if (j && j.error) {
                  window.__ingestResult = { error: `Shopee error ${j.error}`, shopid, itemid };
                  say(`AI cowork: Shopee error ${j.error}`);
                  return;
                }
                const batch = (j.data && j.data.ratings) || [];
                total = total || ratingTotal(j);
                for (const x of batch) {
                  rows.push(normaliseRating(x));
                }
                offset += PAGE;
                window.__ingestProgress = { rows: rows.length, total, offset, shopid, itemid };
                say(`AI cowork: đã lấy ${rows.length} review`);
                if (batch.length < PAGE || offset >= MAX) {
                  // Done — publish result
                  window.__ingestResult = { shopid, itemid, rows };
                  say(`AI cowork: xong ${rows.length} review, đang lưu CSV...`);
                } else {
                  // Next page after pace delay
                  setTimeout(fetchPage, PACE);
                }
              })
              .catch((e) => {
                window.__ingestResult = { error: String(e.message || e), shopid, itemid };
                say("AI cowork: lỗi " + String(e.message || e));
              });
          }

          fetchPage();
        })();
      },
    });

    // ── Step 2: poll window.__ingestResult mỗi 2s, timeout 180s ────────
    // 180s = 3000 reviews / 50 per page * 700ms + buffer ≈ 42s thực tế,
    // 180s là safety net cho mạng chậm.
    const POLL_INTERVAL = 2000;
    const POLL_TIMEOUT = 180_000;
    const pollStart = Date.now();

    let data = null;
    while (true) {
      await new Promise((s) => setTimeout(s, POLL_INTERVAL));

      if (Date.now() - pollStart > POLL_TIMEOUT) {
        throw new Error("tab-based fetch timed out after 180s");
      }

      let pollResult;
      try {
        const pr = await chrome.scripting.executeScript({
          target: { tabId },
          world: "MAIN",
          func: () => window.__ingestResult,
        });
        pollResult = pr && pr[0] && pr[0].result;
      } catch (e) {
        // Tab may have navigated away or been closed — treat as error
        throw new Error("tab poll failed: " + e.message);
      }

      if (pollResult === undefined || pollResult === null) {
        // Still running — log progress if rows are counting up
        console.log("[ingest] tab crawl in progress, polling…");
        try {
          const pp = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: () => window.__ingestProgress,
          });
          const tabProgress = pp && pp[0] && pp[0].result;
          if (tabProgress && progress) {
            const percent = crawlPercent(tabProgress.rows, tabProgress.total, 35, 60);
            await progress({
              status: "running",
              stage: "fetch-tab",
              message: `Tab thật đã lấy ${tabProgress.rows} review`,
              rows: tabProgress.rows,
              total: tabProgress.total,
              offset: tabProgress.offset,
              itemid: tabProgress.itemid,
              shopid: tabProgress.shopid,
              tabId,
              percent,
            });
          }
        } catch (e) {
          console.warn("[ingest] tab progress poll failed:", e.message);
        }
        continue;
      }

      data = pollResult;
      break;
    }

    if (!data) throw new Error("tab script returned no data");
    if (data.error) throw new Error(data.error);

    console.log(
      "[ingest] tab fetch got", data.rows.length,
      "reviews (shopid:", data.shopid, "itemid:", data.itemid, ")"
    );
    return { itemid: data.itemid, rows: data.rows };
  } finally {
    // Keep the visible crawl tab open so the user can inspect what happened.
  }
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text: text || "" });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function report(job, body) {
  await fetch(`${HELPER}/ingest?name=shopee_${body.itemid || job.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job: job.id, source: job.url, ...body }),
  });
}

async function runJob(job) {
  console.log("[ingest] ▶ starting job", job.id, "→", job.url);
  setBadge("...", "#1a73e8");
  const progress = (patch) => reportProgress(job, patch);
  await progress({
    status: "running",
    stage: "started",
    message: "Extension đã nhận job",
    percent: 5,
    rows: 0,
    url: job.url,
    kind: job.kind,
  });

  // ── FIX A: keepalive để MV3 service worker không bị Chrome kill ──────
  // Khi job đang chạy (tab load + paginate = 30–120s), không có request nào
  // pending → Chrome kill worker → tab bị đóng đột ngột. Ping /ping mỗi
  // 20s giữ worker sống suốt quá trình. Cleared trong finally block.
  const keepalive = startKeepalive();

  try {
    const { itemid, rows } = await collectReviews(job.url, progress);
    await progress({
      status: "saving",
      stage: "saving",
      message: `Đang lưu ${rows.length} review ra JSON/CSV`,
      rows: rows.length,
      itemid,
      percent: 98,
    });
    await report(job, { itemid, rows });
    console.log("[ingest] ✔ job", job.id, "done —", rows.length, "reviews");
    setBadge(String(rows.length), "#188038");
  } catch (e) {
    console.error("[ingest] ✘ job", job.id, "failed:", e.message);
    await progress({
      status: "error",
      stage: "failed",
      message: String(e.message || e),
      error: String(e.message || e),
      percent: 100,
    });
    await report(job, { error: String(e.message || e) });
    setBadge("err", "#d93025");
  } finally {
    clearInterval(keepalive);
  }
  setTimeout(() => setBadge(""), 20000);
}

let looping = false;

async function loop() {
  if (looping) return;
  looping = true;
  try {
    // Long-poll: returns as soon as a job exists, and the open request is what keeps
    // this MV3 service worker from being shut down between jobs.
    for (;;) {
      let jobs = [];
      try {
        const r = await fetch(`${HELPER}/ingest/jobs?wait=25`);
        jobs = (await r.json()).jobs || [];
      } catch {
        // Helper down (launcher not running) — back off, the alarm will retry.
        await new Promise((s) => setTimeout(s, 5000));
        return;
      }
      for (const job of jobs) await runJob(job);
    }
  } finally {
    looping = false;
  }
}

// The alarm is the safety net: if Chrome kills the worker mid-poll, this restarts it.
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("poll", { periodInMinutes: 1 });
  loop();
});
chrome.runtime.onStartup.addListener(loop);
chrome.alarms.onAlarm.addListener(loop);
loop();
