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
const MAX_REVIEWS = 3000;
const PACE_MS = 700; // between pages — a burst is what gets a session flagged

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

async function resolveShopId(itemid, originalUrl) {
  console.log("[ingest] resolveShopId: item", itemid, "url", originalUrl);

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
        if (sid) return String(sid);
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
        return rm[1];
      }
      const html = await r.text();
      const sid = scrape(html);
      if (sid) {
        console.log("[ingest] shopid from HTML:", sid);
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
    if (sid) return String(sid);
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

async function collectReviews(url) {
  console.log("[ingest] collectReviews:", url);
  let { shopid, itemid } = idsFrom(url);
  console.log("[ingest] parsed ids → shopid:", shopid, "itemid:", itemid);
  if (!itemid) throw new Error("không đọc được itemid từ " + url);
  if (!shopid) shopid = await resolveShopId(itemid, url);
  if (!shopid) throw new Error("không đọc được shopid cho item " + itemid);

  // ── Try 1: fetch ratings from the background service worker ─────────
  try {
    const rows = await _fetchRatingsBackground(shopid, itemid);
    return { itemid, rows };
  } catch (bgErr) {
    console.warn("[ingest] background fetch failed:", bgErr.message);
    // 403/429 = Shopee blocks the background context → fall back to tab
    if (!/403|429|Shopee error/.test(bgErr.message)) throw bgErr;
  }

  // ── Try 2: open a real tab on shopee.vn and fetch from page context ─
  console.log("[ingest] falling back to tab-based ratings fetch…");
  return await _fetchRatingsViaTab(shopid, itemid, url);
}

// Background fetch — fast when Shopee trusts the service-worker origin.
async function _fetchRatingsBackground(shopid, itemid) {
  const rows = [];
  for (let offset = 0; offset < MAX_REVIEWS; offset += PAGE_SIZE) {
    const r = await fetch(
      `https://shopee.vn/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}` +
        `&type=0&filter=0&limit=${PAGE_SIZE}&offset=${offset}`,
      { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
    );
    if (!r.ok) throw new Error(`HTTP ${r.status} ở offset ${offset}`);
    const j = await r.json();
    if (j && j.error) throw new Error(`Shopee error ${j.error} (is_login=${j.is_login})`);
    const batch = (j.data && j.data.ratings) || [];
    for (const x of batch) {
      rows.push({
        user: x.author_username || "",
        sao: x.rating_star,
        noi_dung: (x.comment || "").replace(/\s+/g, " ").trim(),
        thoi_gian: new Date((x.ctime || 0) * 1000).toISOString().slice(0, 19).replace("T", " "),
        phan_loai: (x.product_items || []).map((p) => p.model_name).filter(Boolean).join("|"),
        so_anh: (x.images || []).length,
        huu_ich: x.like_count || 0,
      });
    }
    setBadge(String(rows.length));
    if (batch.length < PAGE_SIZE) break;
    await new Promise((s) => setTimeout(s, PACE_MS));
  }
  return rows;
}

// Tab fetch — opens a real shopee.vn tab and runs the paginated fetch loop
// INSIDE the page context (world: MAIN). The script re-reads shopid/itemid from
// the loaded page — this is critical because Shopee may redirect the URL, making
// the pre-resolved IDs wrong. Mirrors the approach in launch.py's bookmarklet.
async function _fetchRatingsViaTab(fallbackShopid, fallbackItemid, productUrl) {
  const tab = await chrome.tabs.create({ url: productUrl, active: false });
  const tabId = tab.id;

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
    await new Promise((s) => setTimeout(s, 3000));

    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [fallbackShopid, fallbackItemid],
      func: async (fbShopid, fbItemid) => {
        // ── Step 1: extract shopid + itemid from the LOADED page ──────
        let shopid, itemid;

        // Try URL patterns (after any redirect)
        let m = location.pathname.match(/\/product\/(\d+)\/(\d+)/);
        if (m) { shopid = m[1]; itemid = m[2]; }
        if (!shopid || !itemid) {
          m = location.href.match(/i\.(\d+)\.(\d+)/);
          if (m) { shopid = m[1]; itemid = m[2]; }
        }
        // Try embedded JSON in the rendered HTML
        if (!shopid || !itemid) {
          const html = document.documentElement.innerHTML;
          shopid = shopid || (html.match(/"shopid"\s*:\s*"?(\d+)/) || [])[1];
          itemid = itemid || (html.match(/"itemid"\s*:\s*"?(\d+)/) || [])[1];
        }
        // Last resort: use the values the extension pre-resolved
        shopid = shopid || fbShopid;
        itemid = itemid || fbItemid;

        if (!shopid || !itemid)
          return { error: "no shopid/itemid on page: " + location.href };

        // ── Step 2: paginate the ratings API (same-origin, no headers) ─
        const rows = [];
        for (let offset = 0; offset < 3000; offset += 50) {
          const r = await fetch(
            `/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}` +
              `&type=0&filter=0&limit=50&offset=${offset}`
          );
          if (!r.ok)
            return { error: `HTTP ${r.status} at offset ${offset}`, shopid, itemid };
          const j = await r.json();
          if (j && j.error)
            return { error: `Shopee error ${j.error}`, shopid, itemid };
          const batch = (j.data && j.data.ratings) || [];
          for (const x of batch) {
            rows.push({
              user: x.author_username || "",
              sao: x.rating_star,
              noi_dung: (x.comment || "").replace(/\s+/g, " ").trim(),
              thoi_gian: new Date((x.ctime || 0) * 1000)
                .toISOString()
                .slice(0, 19)
                .replace("T", " "),
              phan_loai: (x.product_items || [])
                .map((p) => p.model_name)
                .filter(Boolean)
                .join("|"),
              so_anh: (x.images || []).length,
              huu_ich: x.like_count || 0,
            });
          }
          if (batch.length < 50) break;
          await new Promise((s) => setTimeout(s, 700));
        }
        return { shopid, itemid, rows };
      },
    });

    const data = results && results[0] && results[0].result;
    if (!data) throw new Error("tab script returned no data");
    if (data.error) throw new Error(data.error);
    console.log(
      "[ingest] tab fetch got", data.rows.length,
      "reviews (shopid:", data.shopid, "itemid:", data.itemid, ")"
    );
    return { itemid: data.itemid, rows: data.rows };
  } finally {
    chrome.tabs.remove(tabId).catch(() => {});
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
  try {
    const { itemid, rows } = await collectReviews(job.url);
    await report(job, { itemid, rows });
    console.log("[ingest] ✔ job", job.id, "done —", rows.length, "reviews");
    setBadge(String(rows.length), "#188038");
  } catch (e) {
    console.error("[ingest] ✘ job", job.id, "failed:", e.message);
    await report(job, { error: String(e.message || e) });
    setBadge("err", "#d93025");
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
