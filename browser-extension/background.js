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
  return tail ? { shopid: null, itemid: tail[1] } : {};
}

async function resolveShopId(itemid) {
  // The PDP endpoint answers with the numeric shop id for a bare item id.
  const r = await fetch(
    `https://shopee.vn/api/v4/pdp/get_pc?item_id=${itemid}&detail_level=0`,
    { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
  );
  const j = await r.json().catch(() => null);
  const d = j && j.data && (j.data.item || j.data);
  return d && (d.shop_id || d.shopid) ? String(d.shop_id || d.shopid) : null;
}

async function collectReviews(url) {
  let { shopid, itemid } = idsFrom(url);
  if (!itemid) throw new Error("không đọc được itemid từ " + url);
  if (!shopid) shopid = await resolveShopId(itemid);
  if (!shopid) throw new Error("không đọc được shopid cho item " + itemid);

  const rows = [];
  for (let offset = 0; offset < MAX_REVIEWS; offset += PAGE_SIZE) {
    const r = await fetch(
      `https://shopee.vn/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}` +
        `&type=0&filter=0&limit=${PAGE_SIZE}&offset=${offset}`,
      { credentials: "include", headers: { "x-requested-with": "XMLHttpRequest" } }
    );
    if (!r.ok) throw new Error(`HTTP ${r.status} ở offset ${offset}`);
    const j = await r.json();
    // Shopee answers 200 with an error code when it distrusts the caller.
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
  return { itemid, rows };
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
  setBadge("...", "#1a73e8");
  try {
    const { itemid, rows } = await collectReviews(job.url);
    await report(job, { itemid, rows });
    setBadge(String(rows.length), "#188038");
  } catch (e) {
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
