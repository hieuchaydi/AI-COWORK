"""Single-command launcher for the Workspace (OpenWorker) experience.

    python launch.py

Spawns TWO background children + one in-process helper:
  1. connect-ai-server        (FastAPI backend on 127.0.0.1:8765)
  2. connect-AI GUI (Vite)    (React app on http://localhost:1420 — the main UI:
                               chat/tools/connectors/approvals/MCP)
  3. helper HTTP (:8766)      (in-process — Google wizard, connectors wizard,
                               /outputs + /artifacts file serving)

Then waits on the GUI in the foreground. On Ctrl+C (or when the GUI exits
for any reason) all children are torn down together — nothing leaks.

Logs:
  logs/connect-ai-server.log
  logs/connect-ai-gui.log
"""

from __future__ import annotations

import atexit
import csv
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# Our banners and Vietnamese hints use non-cp1252 characters (⇒, ✓, ─). A Windows
# console — or a piped stdout — defaults to the ANSI codepage and raises
# UnicodeEncodeError mid-print, which used to take the whole launcher down AFTER
# the children were already up. Force UTF-8 and never die on an unprintable glyph.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — older/exotic stream objects
        pass

ROOT = Path(__file__).resolve().parent


def _find_venv() -> Path:
    """The ONE virtualenv this project runs on: `.venv/` at the repo root, which
    hosts the sidecar, every MCP bridge dependency and the Google refresher.

    Older checkouts kept it at `connect-ai/.venv` (inside the vendored tree); fall
    back to that so a machine that hasn't been migrated still launches.
    """
    for candidate in (ROOT / ".venv", ROOT / "connect-ai" / ".venv"):
        if (candidate / "Scripts" / "python.exe").exists():
            return candidate
    return ROOT / ".venv"  # not created yet — the error message below names it


VENV_DIR = _find_venv()
VENV_PY = VENV_DIR / "Scripts" / "python.exe"
VENV_PIP = VENV_DIR / "Scripts" / "pip.exe"
SERVER_EXE = VENV_DIR / "Scripts" / "connect-ai-server.exe"
GUI_DIR = ROOT / "connect-ai" / "surfaces" / "gui"
LOG_DIR = ROOT / "logs"
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """Minimal .env parser — sets os.environ from KEY=value lines.
    Existing env vars win (so `set X=y & python launch.py` overrides .env)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env_file(ENV_FILE)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
CEREBRAS_KEY = os.environ.get("CEREBRAS_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
# Cloudflare Workers AI — partner models (Gemini/GPT/Claude) on Cloudflare billing.
# Both halves are required: the endpoint URL is account-scoped.
CLOUDFLARE_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
# Defaults are the @cf/* open-weight models: those run on the account's Workers AI
# allocation, while partner models (google/…, openai/…) bill against an AI Gateway
# balance and answer 402 until it's funded — a dead picker entry we don't want by
# default. Add "cloudflare:google/gemini-3.6-flash" here once the gateway has credit.
CLOUDFLARE_MODELS = [
    m.strip()
    for m in os.environ.get(
        "CLOUDFLARE_MODEL",
        "cloudflare:@cf/openai/gpt-oss-120b,"
        "cloudflare:@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    ).split(",")
    if m.strip()
]
API_TOKEN = os.environ.get("CONNECT_AI_API_TOKEN") or os.environ.get(
    "COWORKER_API_TOKEN", "connect-ai-dev-token"
)
API_HOST = "127.0.0.1"
API_PORT = "8765"
# Gemini 2.5 Flash — cloud, fast, generous free tier, follows language
# instructions cleanly (Qwen tends to slip into Chinese on ambiguous prompts).
# Alternatives:
#   "ollama:qwen2.5:7b"              local, free, no rate limits (but Chinese-biased)
#   "groq:llama-3.3-70b-versatile"   cloud, very fast, but the free tier's 12k TPM is
#                                    below one agent turn — every call 413s (2026-08-08)
#   "cloudflare:@cf/openai/gpt-oss-120b"  free neuron allocation, 128k ctx, real tool calls
OW_MODEL = "gemini:gemini-3.1-flash-lite"
GUI_PORT = "1420"
HELPER_PORT = 8766  # tiny sidecar: Google/connectors wizards + outputs/artifacts serving


def _outputs_root() -> Path:
    """Where every generated file lands — same tree crawl.py/browser.py write to."""
    return Path(os.environ.get("COWORKER_OUTPUT_DIR") or (ROOT / "outputs")).expanduser()


# Jobs the agent queues for the browser extension to run. In memory on purpose: a job
# is only meaningful while both the helper and Chrome are up, and a stale job replayed
# after a restart would scrape something nobody asked for.
_INGEST_JOBS: list[dict] = []
_INGEST_JOBS_LOCK = threading.Lock()
_INGEST_JOB_EVENT = threading.Event()
# job id → what /ingest received for it, so the agent can block on completion instead of
# guessing when to look for the file (and can see the error when a job fails).
_INGEST_RESULTS: dict[str, dict] = {}


def _queue_ingest_job(url: str, kind: str) -> dict:
    job = {
        "id": f"job-{int(time.time() * 1000)}",
        "url": url,
        "kind": kind,
        "queued_at": time.strftime("%H:%M:%S"),
    }
    with _INGEST_JOBS_LOCK:
        _INGEST_JOBS.append(job)
    _INGEST_JOB_EVENT.set()
    print(f"[ingest] job queued: {kind} {url}", file=sys.stderr)
    return job


def _claim_ingest_jobs(wait_seconds: float) -> list[dict]:
    """Long-poll: hand the extension every pending job, blocking until one shows up.

    Long-poll rather than a plain interval because an MV3 service worker is killed when
    idle — an open fetch keeps it alive, and the job starts the moment it is queued
    instead of on the next tick. ThreadingHTTPServer gives each poll its own thread, so
    blocking here costs nothing.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        with _INGEST_JOBS_LOCK:
            if _INGEST_JOBS:
                jobs = list(_INGEST_JOBS)
                _INGEST_JOBS.clear()
                _INGEST_JOB_EVENT.clear()
                return jobs
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        _INGEST_JOB_EVENT.wait(min(remaining, 1.0))


def _write_ingest_csv(json_name: str, rows: list) -> str | None:
    """Flat list-of-dicts → outputs/csv/<name>.csv. Returns the relative path, or None
    when the shape isn't tabular (nested payloads stay JSON-only).

    Columns are the union of keys in first-seen order, so a row missing a field lines up
    instead of shifting every column after it. BOM because Excel reads a BOM-less UTF-8
    CSV as ANSI and turns every Vietnamese diacritic into mojibake.
    """
    if not rows or not all(isinstance(r, dict) for r in rows):
        return None
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    if not cols:
        return None
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    out_dir = _outputs_root() / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = json_name[:-5] if json_name.endswith(".json") else json_name
    target = out_dir / f"{stem}.csv"
    try:
        target.write_text("﻿" + buf.getvalue(), encoding="utf-8", newline="")
    except OSError:
        return None
    return f"outputs/csv/{target.name}"


# The bookmarklet body. Runs in the user's ORDINARY Chrome — no Playwright, no CDP,
# nothing for an anti-bot to fingerprint: it is the page's own JS calling the page's
# own API with the page's own cookies. Pages the data out, POSTs it to /ingest, and
# falls back to a plain file download if the site's CSP blocks connect-src to
# 127.0.0.1 (Shopee's CSP has not, but that can change any day).
_INGEST_BOOKMARKLET_JS = r"""
(async () => {
  const BASE = 'http://127.0.0.1:__HELPER_PORT__/ingest';
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#111;'
    + 'color:#fff;font:13px system-ui;padding:10px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.4)';
  document.body.appendChild(box);
  const say = t => { box.textContent = t; };
  const finish = (msg, ms) => { say(msg); setTimeout(() => box.remove(), ms || 9000); };
  try {
    // Shopee ships the same product under several URL shapes; all of them carry the two
    // ids somewhere. /product/<shopid>/<itemid> is what the site itself links to.
    let shopid, itemid;
    const m = location.pathname.match(/^\/product\/(\d+)\/(\d+)/)
           || location.href.match(/i\.(\d+)\.(\d+)/);
    if (m) { shopid = m[1]; itemid = m[2]; }
    if (!shopid || !itemid) {
      // /<shopname>/<itemid> and any future shape: dig the ids out of the embedded state.
      const h = document.documentElement.innerHTML;
      shopid = shopid || (h.match(/"shopid":\s*"?(\d+)/) || [])[1];
      itemid = itemid || (h.match(/"itemid":\s*"?(\d+)/) || [])[1]
                      || (location.pathname.match(/\/(\d{6,})\/?$/) || [])[1];
    }
    if (!shopid || !itemid)
      return finish('Khong doc duoc shopid/itemid tu ' + location.pathname
        + ' — mo trang san pham roi bam lai', 15000);
    const rows = [];
    for (let off = 0; off < 3000; off += 50) {
      say('Dang lay... ' + rows.length + ' danh gia');
      const r = await fetch('/api/v2/item/get_ratings?itemid=' + itemid + '&shopid=' + shopid
        + '&type=0&filter=0&limit=50&offset=' + off, { headers: { 'x-requested-with': 'XMLHttpRequest' } });
      if (!r.ok) { say('HTTP ' + r.status + ' — dung lai o ' + rows.length); break; }
      const j = await r.json();
      const batch = (j.data && j.data.ratings) || [];
      for (const x of batch) rows.push({
        user: x.author_username || '',
        sao: x.rating_star,
        noi_dung: (x.comment || '').replace(/\s+/g, ' ').trim(),
        thoi_gian: new Date((x.ctime || 0) * 1000).toISOString().slice(0, 19).replace('T', ' '),
        phan_loai: (x.product_items || []).map(p => p.model_name).join('|')
      });
      if (batch.length < 50) break;
      await new Promise(s => setTimeout(s, 700));
    }
    if (!rows.length) return finish('Khong co danh gia nao');
    try {
      const res = await fetch(BASE + '?name=shopee_' + itemid, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: location.href, rows })
      });
      const out = await res.json();
      finish(out.ok ? ('Xong: ' + rows.length + ' danh gia -> ' + (out.csv || out.path))
                    : ('Loi: ' + JSON.stringify(out)));
    } catch (e) {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([JSON.stringify({ source: location.href, rows })],
        { type: 'application/json' }));
      a.download = 'shopee_' + itemid + '.json';
      a.click();
      finish('Khong goi duoc helper (' + e.message + ') — da tai file JSON ve may, keo vao chat', 15000);
    }
  } catch (e) { finish('Loi: ' + e.message); }
})()
"""

_INGEST_HELP_HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>Ingest — kéo dữ liệu từ Chrome thường về agent</title>
<style>
 body{font:15px/1.6 system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#111}
 code,pre{background:#f4f4f5;border-radius:5px;padding:2px 6px;font-size:13px}
 pre{padding:12px;overflow-x:auto}
 .bm{display:inline-block;background:#111;color:#fff;padding:10px 18px;border-radius:8px;
     text-decoration:none;font-weight:600;margin:8px 0}
 .hint{color:#666;font-size:13px}
 table{border-collapse:collapse;width:100%;margin-top:10px}
 td,th{border-bottom:1px solid #e5e5e5;padding:6px 8px;text-align:left;font-size:13px}
</style>
<h1>Ingest</h1>
<p>Luồng <strong>không đụng CDP</strong>: Chrome thường của bạn lấy dữ liệu bằng cookie thật rồi
đẩy về đây. Không có Playwright, không có gì để anti-bot bắt.</p>
<p class="hint">Vì sao cần: đo 2026-08-08, Shopee chặn browser điều khiển bằng CDP ngay request
đầu tiên — cả Chromium bundled lẫn Chrome thật, cả khi vào trang chủ trước, cả khi chưa đăng
nhập. Không có cờ nào bật lên để né được.</p>

<h2>Cách 1 — extension (agent tự làm, cài một lần)</h2>
<ol>
 <li>Mở <code>chrome://extensions</code> → bật <strong>Developer mode</strong> góc trên phải</li>
 <li><strong>Load unpacked</strong> → chọn thư mục <code>__EXT_DIR__</code></li>
</ol>
<p>Xong. Từ giờ chỉ cần bảo agent <em>"cào đánh giá &lt;link&gt;"</em> — nó xếp job, extension
chạy bằng phiên Chrome của bạn, CSV tự về. Bạn không phải bấm gì.</p>
<p class="hint">Số trên icon extension là số đánh giá đã lấy được. Xanh lá = xong, đỏ = lỗi.</p>

<h2>Cách 2 — bookmarklet (không cài gì, bấm tay mỗi lần)</h2>
<p><a class="bm" href="__BOOKMARKLET__">Cào đánh giá Shopee</a></p>
<p class="hint">Kéo lên thanh bookmark — đừng bấm ở đây, trang này không có sản phẩm nào. Rồi mở
trang sản phẩm trong Chrome thường và bấm nó.</p>

<h2>3. Xong — CSV đã có sẵn</h2>
<p>Helper tự ghi <code>outputs/csv/&lt;tên&gt;.csv</code> (kèm BOM, Excel đọc tiếng Việt đúng) và
<code>outputs/inbox/&lt;tên&gt;.json</code> bản thô. Cả hai hiện luôn ở panel Artifacts — không cần
agent làm gì.</p>
<p>Muốn phân tích thêm thì bảo agent:</p>
<pre>Đọc http://127.0.0.1:__HELPER_PORT__/outputs/inbox/shopee_&lt;itemid&gt;.json
bằng web_fetch rồi tóm tắt khen/chê</pre>

<h2>Đã nhận</h2>
<table><tr><th>File</th><th>Số dòng</th><th>Lúc</th></tr>__ROWS__</table>

<h2>Đẩy dữ liệu khác vào</h2>
<p>Endpoint dùng chung, không riêng Shopee:</p>
<pre>POST http://127.0.0.1:__HELPER_PORT__/ingest?name=ten_file
{"rows": [ ... ], "source": "tuỳ chọn"}</pre>
"""


# Unified connectors wizard — served at /connectors/wizard. Vanilla HTML+JS,
# no framework. Talks to helper endpoints below to add/switch/remove accounts
# for Google, Telegram bots, and any token-based connector the OpenWorker
# sidecar knows about. Placeholder __HELPER_PORT__ is substituted at serve
# time so the page works even if HELPER_PORT changes.
_CONNECTORS_WIZARD_HTML = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>Connectors wizard</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0b0d10;color:#e6e6e6;padding:24px;max-width:960px;margin:auto}
h1{color:#60a5fa;margin:0 0 8px}
h2{color:#a5b4fc;margin:32px 0 8px;font-size:18px}
.sub{color:#9ca3af;margin-bottom:24px}
.card{background:#1a1d23;padding:16px 20px;border-radius:10px;margin:10px 0;border:1px solid #262b33}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.badge{background:#374151;color:#e5e7eb;padding:2px 8px;border-radius:12px;font-size:12px}
.badge.active{background:#065f46;color:#a7f3d0}
.badge.err{background:#7f1d1d;color:#fecaca}
button{background:#3b82f6;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{background:#2563eb}
button.ghost{background:#374151}button.ghost:hover{background:#4b5563}
button.danger{background:#991b1b}button.danger:hover{background:#b91c1c}
input,select{background:#0b0d10;color:#e6e6e6;border:1px solid #374151;padding:6px 10px;border-radius:5px;font-size:13px}
input{flex:1;min-width:180px}
code{background:#0b0d10;padding:1px 6px;border-radius:3px;color:#fbbf24}
a{color:#60a5fa}
.hint{color:#9ca3af;font-size:12px;margin-top:6px}
.out{color:#fbbf24;font-size:12px;margin-top:6px;white-space:pre-wrap;font-family:ui-monospace,monospace}
details{margin:6px 0}summary{cursor:pointer;color:#9ca3af;font-size:12px}
</style></head><body>
<h1>Connectors</h1>
<p class="sub">Kết nối tài khoản mới cho Google (Gmail/Drive/Calendar), Telegram bot, và mọi connector khác của AI cowork.</p>

<h2>Google</h2>
<div class="card">
  <p>Full wizard riêng có 2 flow (OAuth callback + Playground paste): <a href="/google/wizard" target="_blank">/google/wizard</a></p>
  <div id="google-accounts">loading…</div>
</div>

<h2>Telegram bots</h2>
<div class="card">
  <div id="tg-accounts">loading…</div>
  <div class="row" style="margin-top:12px">
    <input id="tg-token" placeholder="Bot token từ @BotFather (dạng 123:AAA...)">
    <input id="tg-name" placeholder="account name (vd: primary, notifier)" style="max-width:200px">
    <button onclick="tgConnect()">+ Add bot</button>
  </div>
  <div class="hint">Bot token: chat với <a href="https://t.me/BotFather" target="_blank">@BotFather</a> → /newbot → copy token. Multi-bot OK, switch anytime.</div>
  <div id="tg-out" class="out"></div>
</div>

<h2>Connectors khác (token-based)</h2>
<div class="card">
  <p class="hint">Chọn connector, paste API token / bot token / integration secret. Helper forward tới AI cowork sidecar <code>/v1/connectors/&lt;name&gt;/connect</code>. Hint field name theo provider.</p>
  <div class="row">
    <select id="conn-name">
      <option value="slack">Slack (Bot User OAuth Token · xoxb-…)</option>
      <option value="github">GitHub (Personal Access Token · ghp_…)</option>
      <option value="notion">Notion (Integration Secret · secret_…)</option>
      <option value="linear">Linear (Personal API Key · lin_api_…)</option>
      <option value="discord">Discord (Bot Token)</option>
      <option value="openai">OpenAI (API Key · sk-…)</option>
      <option value="anthropic">Anthropic (API Key · sk-ant-…)</option>
      <option value="groq">Groq (API Key · gsk_…)</option>
      <option value="__custom__">— khác (nhập tên) —</option>
    </select>
    <input id="conn-custom" placeholder="custom connector name" style="display:none;max-width:200px">
  </div>
  <div class="row">
    <input id="conn-token" placeholder="token / api key">
    <select id="conn-field">
      <option value="access_token">field: access_token (default)</option>
      <option value="api_key">field: api_key</option>
      <option value="bot_token">field: bot_token</option>
      <option value="token">field: token</option>
    </select>
    <button onclick="connSave()">Connect</button>
  </div>
  <details><summary>Extra fields (JSON, optional)</summary>
    <input id="conn-extra" placeholder='{"workspace_id":"...", "team_id":"..."}' style="width:100%;margin-top:8px">
  </details>
  <div id="conn-out" class="out"></div>
</div>

<h2>Sidecar view (raw)</h2>
<div class="card"><details><summary>Show what /v1/connectors returns</summary>
  <pre id="sidecar-raw" style="max-height:300px;overflow:auto;color:#9ca3af;font-size:11px">loading…</pre>
</details></div>

<script>
const H = 'http://127.0.0.1:__HELPER_PORT__';
async function J(url, opts){ const r = await fetch(url, opts); const t = await r.text(); try{ return JSON.parse(t); }catch{ return {ok:r.ok, raw:t}; }}
function esc(s){ return String(s??'').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'})[c]); }
function out(id, obj){ document.getElementById(id).textContent = typeof obj==='string' ? obj : JSON.stringify(obj, null, 2); }

async function refresh(){
  const d = await J(H + '/connectors');
  document.getElementById('sidecar-raw').textContent = JSON.stringify(d, null, 2);

  // Google
  const g = d.google || [];
  document.getElementById('google-accounts').innerHTML = g.length
    ? g.map(a=>`<div class="row"><span class="badge ${a.is_active?'active':''}">${a.is_active?'★ active':' '}</span>
        <b>${esc(a.name)}</b> — ${esc(a.email||'')} <span class="badge">ttl=${a.expires_in||'?'}s</span></div>`).join('')
    : '<p class="hint">Chưa có Google account. Bấm link wizard ở trên để kết nối.</p>';

  // Telegram
  const t = (d.telegram && d.telegram.accounts) || [];
  const active = d.telegram && d.telegram.active;
  document.getElementById('tg-accounts').innerHTML = t.length
    ? t.map(a=>`<div class="row"><span class="badge ${a.is_active?'active':''}">${a.is_active?'★ active':' '}</span>
        <b>${esc(a.account)}</b> — ${esc(a.username||'')} <span class="badge">id=${a.bot_id}</span>
        ${a.is_active?'':`<button class="ghost" onclick="tgSwitch('${esc(a.account)}')">Switch</button>`}
        <button class="danger" onclick="tgRemove('${esc(a.account)}')">Remove</button></div>`).join('')
    : '<p class="hint">Chưa có bot. Add ở dưới.</p>';
}

async function tgConnect(){
  const bot_token = document.getElementById('tg-token').value.trim();
  const account = document.getElementById('tg-name').value.trim() || 'default';
  if(!bot_token){ out('tg-out', 'thiếu bot_token'); return; }
  const r = await J(H+'/connector/telegram/connect', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bot_token, account, set_active:true})});
  out('tg-out', r);
  document.getElementById('tg-token').value = '';
  document.getElementById('tg-name').value = '';
  refresh();
}
async function tgSwitch(account){
  const r = await J(H+'/connector/telegram/switch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account})});
  out('tg-out', r); refresh();
}
async function tgRemove(account){
  if(!confirm('Remove bot slot '+account+'?')) return;
  const r = await J(H+'/connector/telegram/disconnect', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account})});
  out('tg-out', r); refresh();
}

document.getElementById('conn-name').addEventListener('change', e=>{
  document.getElementById('conn-custom').style.display = e.target.value==='__custom__' ? '' : 'none';
});
async function connSave(){
  let name = document.getElementById('conn-name').value;
  if(name==='__custom__') name = document.getElementById('conn-custom').value.trim();
  if(!name){ out('conn-out','thiếu connector name'); return; }
  const token = document.getElementById('conn-token').value.trim();
  const field_name = document.getElementById('conn-field').value;
  let fields = {};
  const extra = document.getElementById('conn-extra').value.trim();
  if(extra){ try{ fields = JSON.parse(extra); }catch(e){ out('conn-out','extra JSON parse error: '+e.message); return; } }
  const r = await J(H+'/connector/'+encodeURIComponent(name)+'/token', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token, field_name, fields})
  });
  out('conn-out', r);
  refresh();
}

refresh();
setInterval(refresh, 15000);
</script></body></html>
"""



# Google wizard — "Đăng nhập Google" 1 click là đường chính. Chưa có OAuth
# client thì hiện hướng dẫn tạo 1 lần (3 phút) + ô dán JSON ngay tại chỗ —
# không đụng terminal, không tự tạo file. Plain string (no f-string) so the
# JS braces stay readable; served same-origin nên URL relative.
_GOOGLE_WIZARD_HTML = r"""<html><head><meta charset="utf-8"><title>Đăng nhập Google</title>
<style>body{font-family:system-ui;background:#0b0d10;color:#e6e6e6;padding:36px;max-width:720px;margin:auto}
h1{color:#60a5fa}code{background:#1a1d23;padding:2px 6px;border-radius:4px}
button{background:#3b82f6;color:#fff;border:0;padding:10px 20px;border-radius:6px;cursor:pointer;font-size:15px}
button:hover{background:#2563eb} .card{background:#1a1d23;padding:20px;border-radius:8px;margin:16px 0}
textarea{width:100%;min-height:90px;padding:8px;background:#0b0d10;color:#e6e6e6;border:1px solid #333;border-radius:4px;font-family:ui-monospace,monospace;font-size:12px}
input{padding:8px;background:#0b0d10;color:#e6e6e6;border:1px solid #333;border-radius:4px}
.out{margin-top:12px;color:#fbbf24}.hint{color:#9ca3af;font-size:13px}
ol{padding-left:20px}li{margin:6px 0}
a{color:#60a5fa}details{margin-top:14px}summary{cursor:pointer;color:#9ca3af}</style></head><body>
<h1>Đăng nhập Google</h1>
<div class="card"><h3>Đăng nhập Google (1 click)</h3>
<p class="hint"><b>Một lần đăng nhập = toàn bộ Google.</b> Gmail, Google Calendar và Google Drive
đều chạy chung tài khoản này — không phải kết nối / dán token riêng cho từng cái.
Token lưu trên máy này, tự gia hạn mỗi 50 phút. Đăng nhập lần nữa để thêm tài khoản thứ hai.</p>
<button id="login-btn" onclick="login()">Đăng nhập Google →</button>
<p id="out" class="out"></p>
<div id="client-setup" style="display:none">
<p><b>Thiếu OAuth client — tạo 1 lần (~3 phút), sau đó mọi lần đăng nhập chỉ 1 click:</b></p>
<ol>
<li>Mở <a href="https://console.cloud.google.com/apis/credentials" target="_blank">Google Cloud Console → Credentials</a> (tạo project miễn phí nếu chưa có).</li>
<li><b>Create Credentials → OAuth client ID</b> → Application type: <b>Desktop app</b> → Create.<br>
<span class="hint">Nếu bị hỏi consent screen: chọn External → điền tên app + email → Save (khỏi cần verify, app ở chế độ Testing — nhớ add email của bạn vào Test users).</span></li>
<li>Bấm <b>Download JSON</b> (hoặc copy client_id + secret) rồi dán vào đây:</li>
</ol>
<textarea id="cjson" placeholder='{"installed":{"client_id":"...","client_secret":"..."}}  hoặc  {"client_id":"...","client_secret":"..."}'></textarea>
<div style="margin-top:8px"><button onclick="saveClient()">Lưu client</button></div>
<p id="cout" class="out"></p>
</div>
</div>
<div class="card"><details><summary>Cách khác — paste refresh_token từ OAuth Playground (không cần OAuth client)</summary>
<p>1. Vào <a href="https://developers.google.com/oauthplayground/" target="_blank">OAuth Playground</a> →
Settings (⚙️) → bật <b>Access type: Offline</b>.<br>
2. Chọn scope <code>gmail.modify</code>, <code>drive</code>, <code>calendar</code>, Authorize → Exchange authorization code for tokens.<br>
3. Copy <code>refresh_token</code> (bắt đầu <code>1//</code>) và paste dưới đây:</p>
<input id="rt" placeholder="1//..." style="width:100%">
<div style="margin-top:8px"><input id="name" placeholder="account name (email hay tên tuỳ chọn)" style="width:60%">
<button onclick="saveRt()">Lưu</button></div>
<p id="out2" class="out"></p>
</details></div>
<div class="card"><h3>Tài khoản đã kết nối</h3><div id="list">loading...</div>
<div id="services" class="hint" style="margin-top:10px"></div>
<div id="actions" style="margin-top:12px;display:none">
<button onclick="connectAll()">Kết nối lại tất cả</button>
<button onclick="logoutAll()" style="background:#7f1d1d;margin-left:8px">Đăng xuất Google</button>
<p id="aout" class="out"></p></div></div>
<script>
function esc(s){return String(s??'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'})[c]);}
function connectAll(){
  document.getElementById('aout').textContent='Đang kết nối Gmail + Calendar + Drive…';
  fetch('/google/connect-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(d=>{
      document.getElementById('aout').textContent=d.ok?'Xong — đang tải lại…':(d.error||'lỗi');
      if(d.ok) setTimeout(()=>location.reload(), 900);
    });
}
function logoutAll(){
  if(!confirm('Đăng xuất Google? Gmail, Calendar và Drive sẽ bị ngắt và quyền bị thu hồi tại Google.')) return;
  fetch('/google/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(()=>location.reload());
}
// Chưa có OAuth client thì KHÔNG có gì để mở — nói thẳng và đẩy người dùng
// xuống ô dán client, thay vì để nút trông như bấm không ăn.
let hasClient = false;
function showClientSetup(msg){
  const box = document.getElementById('client-setup');
  box.style.display='';
  document.getElementById('out').textContent = msg;
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
  const ta = document.getElementById('cjson');
  if(ta) ta.focus();
}
function login(){
  document.getElementById('out').textContent='';
  if(!hasClient){
    showClientSetup('Chưa có OAuth client nên chưa mở được cửa sổ đăng nhập Google. '
      + 'Làm 1 lần theo 3 bước ngay dưới đây (~3 phút) rồi bấm lại — hoặc dùng cách '
      + 'dán refresh_token ở mục "Cách khác" nếu không muốn tạo client.');
    return;
  }
  fetch('/google/auth-start').then(r=>r.json()).then(d=>{
    if(d.error){
      if(String(d.error).includes('google-oauth.json')) showClientSetup(d.error);
      else document.getElementById('out').textContent=d.error;
    } else {
      document.getElementById('out').innerHTML='Đã mở tab Google — chọn tài khoản, bấm cấp quyền; tab tự đóng khi xong. Trang này tự refresh sau 15s.';
      setTimeout(()=>location.reload(), 15000);
    }
  }).catch(e=>{document.getElementById('out').textContent='Không gọi được helper 8766: '+e;});
}
function saveClient(){
  fetch('/google/oauth-client',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({json:document.getElementById('cjson').value})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){ hasClient = true;
      document.getElementById('client-setup').style.display='none';
      document.getElementById('cout').textContent='';
      document.getElementById('login-btn').textContent='Đăng nhập Google →';
      document.getElementById('out').textContent='Đã lưu OAuth client — bấm Đăng nhập Google ở trên.'; }
    else document.getElementById('cout').textContent=d.error||'lỗi';
  });
}
function saveRt(){
  fetch('/google/add-account',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({refresh_token:document.getElementById('rt').value,
      account:document.getElementById('name').value||'default'})})
  .then(r=>r.json()).then(d=>{
    document.getElementById('out2').textContent=d.ok?('Đã kết nối '+(d.email||d.account)):(d.error||JSON.stringify(d));
    if(d.ok) setTimeout(()=>location.reload(), 1500);
  });
}
fetch('/google/setup-state').then(r=>r.json()).then(d=>{
  hasClient = !!d.has_client;
  if(!hasClient){
    document.getElementById('client-setup').style.display='';
    // Nút phải nói đúng việc nó làm khi chưa có client.
    document.getElementById('login-btn').textContent='Đăng nhập Google → (cần tạo OAuth client trước, 1 lần)';
  }
  const a=d.accounts||[];
  document.getElementById('list').innerHTML = a.length
    ? a.map(x=>`<div>${x.is_active?'★':' '} <b>${esc(x.name)}</b> — ${esc(x.email||'')} (ttl=${x.expires_in??'-'}s)
        <button onclick="fetch('/google/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:'${esc(x.name)}'})}).then(()=>location.reload())">Activate</button>
        <button onclick="if(confirm('Đăng xuất ${esc(x.name)} khỏi Gmail + Calendar + Drive?'))fetch('/google/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account:'${esc(x.name)}'})}).then(()=>location.reload())">Đăng xuất</button>
      </div>`).join('')
    : '<i>chưa có account nào — bấm Đăng nhập Google ở trên</i>';
  // Cùng 1 login, 3 dịch vụ — hiện thẳng cái nào đang chạy.
  const s=d.services||{};
  const keys=Object.keys(s);
  document.getElementById('services').innerHTML = keys.length
    ? 'Dịch vụ: ' + keys.map(k=>`${s[k].connected?'✓':'·'} ${esc(s[k].title||k)}`
        + (s[k].accounts&&s[k].accounts.length?` <span style="color:#6b7280">(${esc(s[k].accounts.join(', '))})</span>`:'')
      ).join(' &nbsp;·&nbsp; ')
    : '';
  if(a.length) document.getElementById('actions').style.display='';
});
</script></body></html>"""


def _fail(msg: str) -> None:
    print(f"[launch] {msg}", file=sys.stderr)
    sys.exit(1)


class _HelperHandler(BaseHTTPRequestHandler):
    """Tiny HTTP surface for the Workspace GUI: Google/connectors wizards,
    token refresh, and /outputs + /artifacts file serving.

    Not authenticated — bound to 127.0.0.1 only, single-user machine. If this
    grows, port to FastAPI with the same token as connect-ai-server.
    """

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Private Network Access: a page on a public origin (shopee.vn) POSTing to
        # http://127.0.0.1 gets a preflight that Chrome fails WITHOUT this header —
        # the /ingest bookmarklet dies silently otherwise.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Expected on the /ingest/jobs long-poll: the extension's MV3 service worker is
            # killed by Chrome when idle, so it hangs up before the wait returns and the
            # socket write fails. Not an error — swallow it instead of dumping a traceback.
            pass

    def do_GET(self) -> None:  # noqa: N802
        # /artifacts/<file> — serve files from ROOT/artifacts/. Content-type
        # inferred from extension (html renders inline in browser, md as text).
        if self.path.startswith("/artifacts/"):
            fname = self.path[len("/artifacts/"):].split("?")[0]
            art_path = ROOT / "artifacts" / fname
            if ".." in fname or not art_path.is_file():
                self.send_response(404)
                self._cors()
                self.end_headers()
                self.wfile.write(b"not found")
                return
            ext = art_path.suffix.lower()
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".md": "text/markdown; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".csv": "text/csv; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")
            data = art_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/artifacts":
            # Simple JSON index of all artifacts.
            art_dir = ROOT / "artifacts"
            files = []
            if art_dir.is_dir():
                for p in sorted(art_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                    if p.is_file():
                        files.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
            body = json.dumps({"artifacts": files}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # /ingest/job?url=… — the agent queues work here. GET, not POST, purely because
        # the agent's only HTTP tool is web_fetch and web_fetch cannot POST.
        if self.path.split("?", 1)[0] == "/ingest/job":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            url = (qs.get("url", [""])[0] or "").strip()
            kind = (qs.get("kind", ["shopee-reviews"])[0] or "shopee-reviews").strip()
            if not url.startswith(("http://", "https://")):
                self._json(400, {"ok": False, "error": "url must be http(s)"})
                return
            job = _queue_ingest_job(url, kind)
            self._json(
                200,
                {
                    "ok": True,
                    "job": job,
                    "note": "Queued. The Chrome extension picks this up within ~1s and "
                    "posts the result to outputs/. Poll GET /ingest/result?id=<job id>.",
                },
            )
            return

        # /ingest/jobs — the extension long-polls here.
        if self.path.split("?", 1)[0] == "/ingest/jobs":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                wait = min(float(qs.get("wait", ["25"])[0]), 55.0)
            except ValueError:
                wait = 25.0
            self._json(200, {"jobs": _claim_ingest_jobs(wait)})
            return

        # /ingest/result?id=… — did the job land yet? Lets the agent block on the answer.
        if self.path.split("?", 1)[0] == "/ingest/result":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            job_id = (qs.get("id", [""])[0] or "").strip()
            done = _INGEST_RESULTS.get(job_id)
            self._json(200, {"ok": bool(done), "result": done})
            return

        # /ingest — the bookmarklet + a receipt list. GET is the help page; the
        # POST that actually accepts data lives in do_POST.
        if self.path.split("?", 1)[0] == "/ingest":
            js = _INGEST_BOOKMARKLET_JS.replace("__HELPER_PORT__", str(HELPER_PORT))
            # Percent-encode, do NOT collapse to one line. An earlier version joined the
            # source with spaces so the href held no raw newline — which silently turned
            # every `//` comment into a comment over the whole remaining program
            # (SyntaxError: Unexpected end of input, caught 2026-08-08). quote() maps
            # newlines to %0A, which is just as newline-free and keeps the code intact.
            href = "javascript:" + urllib.parse.quote(js.strip(), safe="")
            rows_html = ""
            inbox = _outputs_root() / "inbox"
            if inbox.is_dir():
                for p in sorted(
                    inbox.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
                )[:15]:
                    try:
                        n = json.loads(p.read_text(encoding="utf-8")).get("count", "?")
                    except Exception:  # noqa: BLE001
                        n = "?"
                    when = time.strftime("%H:%M %d/%m", time.localtime(p.stat().st_mtime))
                    rows_html += (
                        f'<tr><td><a href="/outputs/inbox/{p.name}">{p.name}</a></td>'
                        f"<td>{n}</td><td>{when}</td></tr>"
                    )
            body = (
                _INGEST_HELP_HTML.replace("__BOOKMARKLET__", href)
                .replace("__ROWS__", rows_html or '<tr><td colspan="3">chưa có gì</td></tr>')
                .replace("__EXT_DIR__", str(ROOT / "browser-extension"))
                .replace("__HELPER_PORT__", str(HELPER_PORT))
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        # ─── /outputs/… — unified project outputs served here ───────────────
        if self.path.startswith("/outputs"):
            outputs_root = _outputs_root()
            rel = self.path[len("/outputs"):].split("?", 1)[0].lstrip("/")

            # Directory listing (root or subdir) → JSON tree
            if not rel or rel.endswith("/"):
                target_dir = outputs_root / rel if rel else outputs_root
                if not target_dir.is_dir():
                    self.send_response(404); self._cors(); self.end_headers()
                    self.wfile.write(b"{}"); return
                entries = []
                for p in sorted(target_dir.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                    if p.is_file():
                        rp = p.relative_to(outputs_root).as_posix()
                        entries.append({
                            "name": p.name,
                            "path": rp,
                            "url": f"/outputs/{rp}",
                            "size": p.stat().st_size,
                            "mtime": p.stat().st_mtime,
                            "kind": p.parent.name if p.parent != outputs_root else "misc",
                        })
                body = json.dumps({"root": str(outputs_root), "count": len(entries), "files": entries}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors(); self.end_headers()
                self.wfile.write(body); return

            # File download
            if ".." in rel:
                self.send_response(403); self._cors(); self.end_headers()
                self.wfile.write(b"forbidden"); return
            target = outputs_root / rel
            if not target.is_file():
                self.send_response(404); self._cors(); self.end_headers()
                self.wfile.write(b"not found"); return
            ext = target.suffix.lower()
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".md": "text/markdown; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".csv": "text/csv; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".pdf": "application/pdf",
                ".zip": "application/zip",
            }.get(ext, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Non-inline content types → prompt download; HTML/img inline
            if ctype == "application/octet-stream" or ext in (".zip", ".pdf"):
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self._cors(); self.end_headers()
            self.wfile.write(data); return
        # ─── Google OAuth endpoints ────────────────────────────────────────
        path_only = self.path.split("?", 1)[0]
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

        if path_only == "/google/status":
            try:
                import google_auth
                accounts = google_auth.list_accounts()
                first_active = next((a for a in accounts if a["is_active"]), None) or (accounts[0] if accounts else None)
                body = json.dumps({
                    "has_refresh_token": bool(first_active and first_active["has_refresh_token"]),
                    "expires_in": first_active.get("expires_in") if first_active else None,
                    "accounts": accounts,  # richer view for the UI
                    # Which Google connectors that login is actually driving right
                    # now — one sign-in, three services, reported in one place.
                    "services": google_auth.sidecar_google_state(
                        f"http://{API_HOST}:{API_PORT}", API_TOKEN
                    ),
                }).encode()
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path_only == "/google/accounts":
            try:
                import google_auth
                body = json.dumps({"accounts": google_auth.list_accounts()}).encode()
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path_only == "/google/auth-start":
            # Kick off OAuth consent. Requires the user's own Desktop OAuth client
            # in google-oauth.json (Playground creds don't accept 127.0.0.1 redirect).
            try:
                import google_auth
                if not google_auth.have_own_desktop_client():
                    body = json.dumps({
                        "error": "no google-oauth.json — download a Desktop OAuth client "
                                 "from https://console.cloud.google.com/apis/credentials and "
                                 "save its JSON as google-oauth.json in the project root",
                        "docs": "https://developers.google.com/identity/protocols/oauth2/native-app",
                    }).encode()
                    self.send_response(400)
                else:
                    redirect_uri = f"http://127.0.0.1:{HELPER_PORT}/google/auth-callback"
                    url = google_auth.build_authorization_url(
                        redirect_uri=redirect_uri,
                        login_hint=(query.get("login_hint") or [""])[0],
                    )
                    # Open the browser locally so the user goes straight to consent.
                    try:
                        import webbrowser
                        webbrowser.open(url, new=2)
                        opened = True
                    except Exception:
                        opened = False
                    body = json.dumps({"auth_url": url, "opened_browser": opened,
                                       "redirect_uri": redirect_uri}).encode()
                    self.send_response(200)
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path_only == "/google/auth-callback":
            # Google redirects the user's browser here with ?code=… (or ?error=…)
            code = (query.get("code") or [""])[0]
            err = (query.get("error") or [""])[0]
            html = ""
            status = 200
            if err:
                html = f"<h1>Google từ chối cấp quyền</h1><p>{err}</p><p>Đóng tab này và thử lại.</p>"
                status = 400
            elif not code:
                html = "<h1>Thiếu ?code=</h1>"
                status = 400
            else:
                try:
                    import google_auth
                    redirect_uri = f"http://127.0.0.1:{HELPER_PORT}/google/auth-callback"
                    result = google_auth.complete_oauth_callback(
                        code=code,
                        redirect_uri=redirect_uri,
                        push=True,
                        api_base=f"http://{API_HOST}:{API_PORT}",
                        ow_token=API_TOKEN,
                    )
                    if "error" in result:
                        html = f"<h1>Lỗi</h1><p>{result['error']}</p>"
                        status = 400
                    else:
                        html = (
                            "<html><head><title>Google connected</title>"
                            "<meta charset='utf-8'>"
                            "<style>body{font-family:system-ui;background:#0b0d10;color:#e6e6e6;"
                            "padding:48px;text-align:center}h1{color:#4ade80}code{background:#1a1d23;"
                            "padding:2px 8px;border-radius:4px}</style></head><body>"
                            f"<h1>✓ Đã kết nối {result['email']}</h1>"
                            f"<p>Slot: <code>{result['account']}</code></p>"
                            f"<p>Access token còn hiệu lực trong <code>{result['expires_in']}s</code>. "
                            f"Refresher sẽ tự gia hạn mỗi 50 phút.</p>"
                            "<p>Bạn có thể đóng tab này và quay lại AI cowork.</p>"
                            "<script>setTimeout(()=>window.close(),4000)</script>"
                            "</body></html>"
                        )
                except Exception as exc:  # noqa: BLE001
                    html = f"<h1>Callback failed</h1><pre>{exc}</pre>"
                    status = 500
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        # ─── Unified connectors endpoints ─────────────────────────────────
        if path_only == "/connectors":
            # Consolidated view: Google accounts + Telegram bots + sidecar connector states.
            out = {"google": [], "telegram": [], "sidecar": {}, "errors": []}
            try:
                import google_auth
                out["google"] = google_auth.list_accounts()
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"google: {exc}")
            try:
                import sys as _sys
                bridge_dir = str(ROOT / "bridge")
                if bridge_dir not in _sys.path:
                    _sys.path.insert(0, bridge_dir)
                import tg_bot_mcp  # noqa: E402
                out["telegram"] = tg_bot_mcp.list_connected_bots()
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"telegram: {exc}")
            try:
                req = urllib.request.Request(
                    f"http://{API_HOST}:{API_PORT}/v1/connectors",
                    headers={"x-connect-ai-token": API_TOKEN},
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    out["sidecar"] = json.loads(r.read())
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"sidecar: {exc}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(out, ensure_ascii=False).encode())
            return

        if path_only == "/connectors/wizard":
            html = _CONNECTORS_WIZARD_HTML.replace("__HELPER_PORT__", str(HELPER_PORT))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if path_only == "/google/wizard":
            # Tiny standalone wizard for users who don't want to touch a terminal.
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(_GOOGLE_WIZARD_HTML.encode("utf-8"))
            return

        if path_only == "/google/setup-state":
            # One call the GUI/wizard can use to decide which path to show:
            # has_client → the 1-click login button works; accounts → who's in;
            # services → which Google connectors that login is driving.
            try:
                import google_auth
                body = json.dumps({
                    "has_client": google_auth.have_own_desktop_client(),
                    "accounts": google_auth.list_accounts(),
                    "services": google_auth.sidecar_google_state(
                        f"http://{API_HOST}:{API_PORT}", API_TOKEN
                    ),
                }).encode()
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path_only = self.path.split("?", 1)[0]

        def _read_json() -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                return {}

        def _reply(status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        # ─── /ingest — data pushed in from a normal browser tab ──────────────
        # The CDP-free path: sites that fingerprint automation (Shopee) still serve
        # their own JS fine, so a bookmarklet in the user's everyday Chrome collects
        # the data and POSTs it here. Lands under outputs/ so the artifact rail and
        # `artifact:` chips pick it up with no extra wiring.
        if path_only == "/ingest":
            body = _read_json()
            job_id = (body.get("job") if isinstance(body, dict) else None) or ""
            # A job that failed in the browser reports here too — otherwise the agent
            # would poll /ingest/result forever waiting for a run that already died.
            if isinstance(body, dict) and body.get("error"):
                if job_id:
                    _INGEST_RESULTS[job_id] = {"ok": False, "error": str(body["error"])[:500]}
                _reply(200, {"ok": True, "recorded": "error"})
                return
            rows = body.get("rows") if isinstance(body, dict) else body
            if not isinstance(rows, list):
                _reply(400, {"ok": False, "error": "expected {rows: [...]} or a JSON array"})
                return
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            raw = (qs.get("name", [""])[0] or (body.get("name") if isinstance(body, dict) else "") or "")
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw)).strip("_")
            if not name:
                name = "ingest_" + time.strftime("%Y%m%d_%H%M%S")
            if not name.endswith(".json"):
                name += ".json"
            inbox = _outputs_root() / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            target = inbox / name
            payload = {
                "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": (body.get("source") if isinstance(body, dict) else None),
                "count": len(rows),
                "rows": rows,
            }
            try:
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except OSError as exc:
                _reply(500, {"ok": False, "error": f"write failed: {exc}"})
                return
            # Write the CSV here too, rather than leaving it to the agent. The whole
            # point of this path is that it works when the model can't help: the
            # workspace is elsewhere so read_file can't reach outputs/, and web_fetch
            # truncates at 100k chars — a few hundred reviews would arrive cut in half.
            # One click now produces the deliverable; the agent is only for summarising.
            csv_rel = _write_ingest_csv(name, rows)
            print(f"[ingest] {len(rows)} rows → {target}", file=sys.stderr)
            result = {
                "ok": True,
                "count": len(rows),
                "path": f"outputs/inbox/{name}",
                "url": f"/outputs/inbox/{name}",
                "csv": csv_rel,
            }
            if job_id:
                _INGEST_RESULTS[job_id] = result
            _reply(200, result)
            return

        if path_only == "/google/refresh":
            # Keep-alive: refresh every stored login and re-push it into the
            # connectors it is already signed into (a service the user
            # disconnected stays disconnected — use /google/connect-all for that).
            try:
                import google_auth
                results = google_auth.sync_all_accounts(
                    f"http://{API_HOST}:{API_PORT}", API_TOKEN, only_connected=True
                )
                if not results:
                    _reply(400, {"ok": False, "error": "no refresh_token — connect an account via /google/wizard"})
                else:
                    _reply(200, {"ok": True, "pushed": True, "results": results})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/connect-all":
            # "Make Google work again": re-wire Gmail + Calendar + Drive from the
            # stored login(s), no consent screen. This is the repair button for a
            # 401 or for a service that was disconnected by hand.
            try:
                import google_auth
                results = google_auth.sync_all_accounts(
                    f"http://{API_HOST}:{API_PORT}", API_TOKEN, only_connected=False
                )
                if not results:
                    _reply(400, {"ok": False, "error": "chưa đăng nhập Google — bấm Đăng nhập Google trước"})
                else:
                    _reply(200, {"ok": True, "results": results,
                                 "services": google_auth.sidecar_google_state(
                                     f"http://{API_HOST}:{API_PORT}", API_TOKEN)})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/logout":
            # Sign out of Google: revoke the grant at Google, delete the local
            # token, and disconnect Gmail + Calendar + Drive. Empty `account`
            # signs out of every Google account on this machine.
            payload = _read_json()
            account = (payload.get("account") or "").strip()
            try:
                import google_auth
                result = google_auth.logout(
                    account, f"http://{API_HOST}:{API_PORT}", API_TOKEN
                )
                _reply(200 if result.get("ok") else 400, result)
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/oauth-client":
            # One-time client setup from the wizard: user pastes the downloaded
            # OAuth Desktop client JSON (or just client_id + client_secret) and
            # we persist it as google-oauth.json — after this, login is 1 click.
            payload = _read_json()
            raw = (payload.get("json") or "").strip()
            data: dict = {}
            if raw:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    _reply(400, {"ok": False, "error": f"JSON không hợp lệ: {exc}"})
                    return
            else:
                data = {
                    k: (payload.get(k) or "").strip()
                    for k in ("client_id", "client_secret")
                    if payload.get(k)
                }
            node = data.get("installed") or data.get("web") or data
            cid = (node.get("client_id") or "").strip()
            secret = (node.get("client_secret") or "").strip()
            if not cid or not secret:
                _reply(400, {"ok": False, "error": "thiếu client_id / client_secret trong JSON"})
                return
            try:
                (ROOT / "google-oauth.json").write_text(
                    json.dumps(data, indent=2), encoding="utf-8"
                )
                _reply(200, {"ok": True, "client_id": cid})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/add-account":
            # Playground paste flow: user supplies a refresh_token, we validate + store.
            payload = _read_json()
            rt = (payload.get("refresh_token") or "").strip()
            account = (payload.get("account") or "default").strip() or "default"
            if not rt.startswith("1//"):
                _reply(400, {"ok": False, "error": "refresh_token phải bắt đầu bằng '1//' (Google OAuth prefix)"})
                return
            try:
                import google_auth
                store = google_auth._load_tokens()
                store.setdefault("accounts", {})[account] = {"refresh_token": rt}
                store["active"] = account
                google_auth._save_tokens(store)
                tok = google_auth.refresh_access_token(account)
                if not tok:
                    _reply(400, {"ok": False, "error": "refresh_token bị Google từ chối — sai token hoặc revoked"})
                    return
                # Enrich with userinfo (email) if possible
                info = google_auth._fetch_userinfo(tok)
                if info.get("email"):
                    entry = store["accounts"][account]
                    entry["email"] = info["email"]
                    entry["name_display"] = info.get("name")
                    google_auth._save_tokens(store)
                google_auth.push_to_connectors(tok, f"http://{API_HOST}:{API_PORT}", API_TOKEN)
                _reply(200, {"ok": True, "account": account, "email": info.get("email")})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/activate":
            payload = _read_json()
            account = (payload.get("account") or "").strip()
            try:
                import google_auth
                if not google_auth.set_active_account(account):
                    _reply(400, {"ok": False, "error": f"account '{account}' not found"})
                    return
                tok = google_auth.refresh_access_token(account)
                if tok:
                    google_auth.push_to_connectors(tok, f"http://{API_HOST}:{API_PORT}", API_TOKEN)
                _reply(200, {"ok": True, "active": account, "pushed": bool(tok)})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only == "/google/disconnect":
            # Alias of /google/logout kept for the wizard's Remove button —
            # removing a login locally while the connectors still hold its token
            # would leave a half-signed-out state.
            payload = _read_json()
            account = (payload.get("account") or "").strip()
            try:
                import google_auth
                result = google_auth.logout(
                    account, f"http://{API_HOST}:{API_PORT}", API_TOKEN
                )
                _reply(200 if result.get("ok") else 404,
                       {**result, "removed": account if result.get("ok") else None})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        # ─── Telegram bot management (via tg_bot_mcp bridge) ────────────
        if path_only.startswith("/connector/telegram/"):
            action = path_only[len("/connector/telegram/"):]
            payload = _read_json()
            try:
                import sys as _sys
                bridge_dir = str(ROOT / "bridge")
                if bridge_dir not in _sys.path:
                    _sys.path.insert(0, bridge_dir)
                import tg_bot_mcp
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": f"bridge import failed: {exc}"})
                return
            if action == "connect":
                r = tg_bot_mcp.connect_bot(
                    bot_token=(payload.get("bot_token") or "").strip(),
                    account=(payload.get("account") or "default").strip() or "default",
                    set_active=bool(payload.get("set_active", True)),
                )
            elif action == "switch":
                r = tg_bot_mcp.switch_bot((payload.get("account") or "").strip())
            elif action == "disconnect":
                r = tg_bot_mcp.disconnect_bot((payload.get("account") or "").strip())
            elif action == "list":
                r = tg_bot_mcp.list_connected_bots()
            else:
                _reply(404, {"error": f"unknown telegram action: {action}"})
                return
            _reply(200 if not (isinstance(r, dict) and "error" in r) else 400, r)
            return

        # ─── Generic token-based connector setup ────────────────────────
        # Forwards a bearer token / API key to the OpenWorker sidecar's
        # /v1/connectors/<name>/connect endpoint, plus stores metadata locally
        # so /connectors can enumerate them. Works for any connector whose
        # setup is "paste a token" (github PAT, notion integration, openai
        # key, anthropic key, slack bot token, discord bot token, ...).
        if path_only.startswith("/connector/") and path_only.endswith("/token"):
            name = path_only[len("/connector/"):-len("/token")]
            payload = _read_json()
            token = (payload.get("token") or "").strip()
            fields = payload.get("fields") or {}  # extra fields (workspace_id, org_id, …)
            if not token and not fields:
                _reply(400, {"ok": False, "error": "provide 'token' or 'fields'"})
                return
            body_fields = {**fields}
            if token:
                # Most connectors use `access_token`; some prefer `api_key` / `bot_token`.
                # Try the most common name; the sidecar validates and errors clearly.
                key = payload.get("field_name") or "access_token"
                body_fields[key] = token
            body = json.dumps({"fields": body_fields}).encode()
            try:
                req = urllib.request.Request(
                    f"http://{API_HOST}:{API_PORT}/v1/connectors/{name}/connect",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-connect-ai-token": API_TOKEN,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode() or "{}")
                _reply(200, {"ok": True, "connector": name, "sidecar": resp})
            except urllib.error.HTTPError as e:
                _reply(e.code, {"ok": False, "error": e.read().decode()[:400]})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        if path_only.startswith("/connector/") and path_only.endswith("/disconnect"):
            name = path_only[len("/connector/"):-len("/disconnect")]
            # Proxy to sidecar signout for anything except telegram (handled above).
            try:
                req = urllib.request.Request(
                    f"http://{API_HOST}:{API_PORT}/v1/connectors/{name}/signout",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "x-connect-ai-token": API_TOKEN,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    _reply(200, {"ok": True, "sidecar": json.loads(r.read().decode() or "{}")})
            except urllib.error.HTTPError as e:
                _reply(e.code, {"ok": False, "error": e.read().decode()[:400]})
            except Exception as exc:  # noqa: BLE001
                _reply(500, {"ok": False, "error": str(exc)})
            return

        _reply(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # noqa: N802
        pass  # keep stdout clean


def _start_helper() -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", HELPER_PORT), _HelperHandler)
    except OSError as exc:
        print(f"[launch] helper HTTP :{HELPER_PORT} not started ({exc})", file=sys.stderr)
        return None
    t = threading.Thread(target=srv.serve_forever, name="helper-http", daemon=True)
    t.start()
    print(f"[launch] Helper HTTP on http://127.0.0.1:{HELPER_PORT}")
    print(f"[launch]   wizard: http://127.0.0.1:{HELPER_PORT}/google/wizard")
    return srv


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port — probed over EVERY
    address the host resolves to, not just IPv4.

    Vite binds `::1` only. An AF_INET-only probe reported 1420 free, the
    preflight waved the launch through, and the user got a cryptic
    "connect-ai-gui exited immediately (code 1)" instead of the port-conflict
    message with the taskkill instructions.
    """
    try:
        infos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _canonname, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(0.5)
                if s.connect_ex(sockaddr) == 0:
                    return True
        except OSError:
            continue
    return False


def _ping_our_sidecar() -> bool:
    """True if port 8765 already answers our token — a leftover from a
    half-crashed prior launch that we can reuse instead of forcing a kill."""
    if not _port_in_use(API_HOST, int(API_PORT)):
        return False
    try:
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}/v1/mcp",
            headers={"x-connect-ai-token": API_TOKEN},
        )
        with urllib.request.urlopen(req, timeout=2) as res:
            return res.status == 200
    except Exception:
        return False


def _ow_post(path: str, body: dict, timeout: float = 5) -> dict | None:
    """Best-effort POST to connect-ai-server, returning the decoded JSON body when there
    is one. Silently ignores failures because this seeding runs at boot before the caller
    can fix anything anyway; callers that care about the answer must handle None."""
    try:
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-connect-ai-token": API_TOKEN,
            },
            method="POST",
        )
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as exc:  # noqa: BLE001
        print(f"[launch] seed {path} failed: {exc}", file=sys.stderr)
        return None
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return None


def _seed_runtime_state() -> None:
    """Idempotent boot seeding — every restart re-asserts our full config so nothing
    drifts between runs. Writes MUST go through the sidecar API (not direct file I/O
    on mcp.json / prefs.json) so we land in the same MSIX overlay the sidecar reads.

    Seeds: model picker + hides broken models + set default model + MCP servers +
    per-persona connector fanout.
    """
    # 1. Model picker — everything the user might switch to. Add is idempotent (re-add
    # of an existing id is a no-op inside OpenWorker).
    picker = [
        "gemini:gemini-2.5-flash",                # DEFAULT — best tool-use / VN language handling
        "gemini:gemini-3.1-flash-lite",
        "gemini:gemini-3.1-flash-lite-preview",
        "gemini:gemini-3.5-flash-lite",
        "gemini:gemini-2.5-pro",                  # bigger reasoning
        "gemini:gemini-3.1-pro-preview",          # free-tier quota=0 but visible
        # groq:llama-3.3-70b-versatile RE-ENABLED (2026-08-09). The 400 (Groq caps `tools`
        # at 128, we ship ~190) is now handled provider-side — OpenAIProvider trims the
        # tool list to the cap for api.groq.com (see providers/openai_provider.py). Kept
        # LAST so quota failover only lands here after the free models. CAVEAT: on the free
        # tier the 12k TPM limit still 413s a >14k agent turn — needs a Dev-tier key.
        "groq:llama-3.3-70b-versatile",
        # ollama:qwen2.5:7b REMOVED from picker — the 7B tier hallucinates tool
        # calls (writes pseudo Node.js instead of calling save_csv / browser_open),
        # so users who leave it on end up with fake artifacts and links to nowhere.
    ]
    # Cerebras — very fast OpenAI-compatible inference. Only seed when the key is present
    # (compat providers fail on first use without a key). gpt-oss-120b = best tool use.
    if CEREBRAS_KEY:
        picker.append("cerebras:gpt-oss-120b")
    # Auto-add Claude models when the key is present — best tool-use quality.
    if ANTHROPIC_KEY:
        picker[:0] = [
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
            "anthropic:claude-opus-4-8",
        ]
    # Add OpenAI models when key is present.
    if OPENAI_KEY:
        picker[:0] = ["openai:gpt-5.5", "openai:gpt-5.6-luna"]
    # Cloudflare needs BOTH halves (the endpoint is account-scoped) — with only one of
    # them the model would sit in the picker and fail on first use, so skip it instead.
    if CLOUDFLARE_TOKEN and CLOUDFLARE_ACCOUNT:
        fields = {"api_key": CLOUDFLARE_TOKEN, "account_id": CLOUDFLARE_ACCOUNT}
        _ow_post("/v1/providers", {"name": "cloudflare", "fields": fields})
        # Gate the picker entry on a live credential check. A model that 401s is worse
        # than a missing one: quota failover walks the picker, so a dead entry at the end
        # of the chain turns "out of quota, retrying" into a hard turn failure (seen
        # 2026-08-08 with a zone-scoped token). Only an outright auth rejection hides it —
        # a 404/timeout proves nothing, so those still get the benefit of the doubt.
        check = _ow_post(
            "/v1/providers/verify", {"name": "cloudflare", "fields": fields}, timeout=20
        )
        if isinstance(check, dict) and not check.get("ok") and "Workers AI" in str(
            check.get("error", "")
        ):
            print(
                f"[launch] skipping Cloudflare models: {check.get('error')}",
                file=sys.stderr,
            )
        else:
            picker.extend(CLOUDFLARE_MODELS)
    for model in picker:
        _ow_post("/v1/settings/models/add", {"model": model})

    # 1b. Hide gemini-3.6-flash (no quota anywhere in the user's tier) + qwen 7B
    # (produces fake tool calls — see picker comment above).
    hide = ["gemini:gemini-3.6-flash", "ollama:qwen2.5:7b"]
    # Cloudflare partner models bill against an AI Gateway balance and 402 while it's
    # empty. Skipping the add is NOT enough: models/add persists, so an entry seeded by
    # an earlier boot (or a hand-run curl) survives forever and keeps poisoning the
    # quota-failover chain. Anything not opted into via CLOUDFLARE_MODEL gets removed.
    for partner in ("cloudflare:google/gemini-3.6-flash",):
        if partner not in CLOUDFLARE_MODELS:
            hide.append(partner)
    for model in hide:
        _ow_post("/v1/settings/models/remove", {"model": model})

    # 1c. Pin default model: Claude Haiku > Cloudflare gpt-oss-120b > Gemini 2.5 Flash.
    # Pinned every boot so a fresh chat can't inherit qwen from a stale session (the
    # original cause of "why is my agent writing fake code").
    # gpt-oss-120b sits above Gemini deliberately (2026-08-08): the Gemini free tier here
    # empties by mid-morning, and a default that 429s on the user's first message is worse
    # than a slightly different model that answers. gpt-oss runs on Workers AI neurons,
    # has 128k context, and tool-calls correctly (verified against the live endpoint).
    if ANTHROPIC_KEY:
        default_model = "anthropic:claude-haiku-4-5"
    elif "cloudflare:@cf/openai/gpt-oss-120b" in picker:
        default_model = "cloudflare:@cf/openai/gpt-oss-120b"
    else:
        default_model = "gemini:gemini-2.5-flash"
    _ow_post("/v1/settings/default-model", {"model": default_model})

    # 1c-bis. Global AGENTS.md. SETUP told the user to copy this into the state dir by
    # hand and — of course — it never happened, so every rule in it (Vietnamese, timezone,
    # the scraping ladder) was dead weight for months. Sync it here instead: the repo file
    # is the source of truth, edits take effect on the next launch.
    _seed_agents_md()

    # 1d. MCP servers we want present every run. Non-destructive: `put_global_server`
    # replaces the entry keyed by name, leaving unrelated servers (slack, notion, ...
    # the OpenWorker demo catalog) untouched. Templates the user has customized are
    # NOT re-seeded here — we only own our own bridges.
    _seed_mcp_servers()


def _seed_agents_md() -> None:
    """Copy repo AGENTS.md → <state-dir>/AGENTS.md, which the runtime injects into every
    system prompt (coworker/project.py). Written directly rather than through the API:
    there is no endpoint for it, and unlike mcp.json this file is only ever read at engine
    build time, so the MSIX-overlay hazard doesn't apply."""
    src = ROOT / "AGENTS.md"
    if not src.is_file():
        return
    dst = Path(os.path.expandvars("%APPDATA%")) / "coworker" / "AGENTS.md"
    try:
        text = src.read_text(encoding="utf-8")
        if dst.is_file() and dst.read_text(encoding="utf-8") == text:
            return  # unchanged — stay quiet
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        print(f"[launch] AGENTS.md synced → {dst}")
    except OSError as exc:
        print(f"[launch] AGENTS.md sync failed: {exc}", file=sys.stderr)


def _seed_mcp_servers() -> None:
    """Register our own MCP bridges via the sidecar API. Runs through /v1/mcp so it
    hits the same file overlay the sidecar reads back from — direct writes to
    %APPDATA%\\coworker\\mcp.json from a differently-spawned process can land in a
    different MSIX overlay and be invisible to the sidecar."""
    venv_py = str(VENV_PY)
    project_root = str(ROOT).replace("\\", "/")
    bridges = {
        # -- Our own bridges --------------------------------------------------
        # Bot API bridge — uses the token already stored in coworker secrets,
        # zero user setup. Gives get_me / get_chat / send_photo / edit_message /
        # delete_message / etc on top of the built-in send_message.
        "telegram-bot": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "tg_bot_mcp.py")],
            "enabled": True,
            "requires_approval": False,
        },
        # Full-account Telegram (MTProto/Telethon) — bot-read scope only.
        # Requires one-time `python bridge/tg_mtproto_mcp.py setup`.
        "telegram-mtproto": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "tg_mtproto_mcp.py")],
            "enabled": True,
            "requires_approval": False,
        },
        # Spawn parallel sub-agents for fan-out work.
        "subagents": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "subagents_mcp.py")],
            "env": {"CONNECT_AI_BASE": f"http://{API_HOST}:{API_PORT}"},
            "enabled": True,
            "requires_approval": False,
        },
        # Reusable skill packs from skills/*.md — agent can load per-task.
        "skills": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "skills_mcp.py")],
            "enabled": True,
            "requires_approval": False,
        },
        # Publish local artifacts to http://localhost:8766/artifacts/*.
        "artifacts": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "artifacts_mcp.py")],
            "enabled": True,
            "requires_approval": False,
        },
        # Slash commands (commands/*.md) — parameterized prompt templates.
        "commands": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "commands_mcp.py")],
            "enabled": True,
            "requires_approval": False,
        },
        # Computer use — mouse/keyboard/screenshot via pyautogui. DANGEROUS —
        # controls the host desktop directly. requires_approval=True so each
        # click/type asks the user in interactive mode. Auto mode still gates
        # via the same tool_calls channel.
        "computer-use": {
            "command": venv_py,
            "args": [str(ROOT / "bridge" / "computer_use_mcp.py")],
            "enabled": True,
            "requires_approval": True,
        },
        # -- Third-party essentials (baked into every boot so a fresh sidecar
        # overlay isn't missing them). All npx-based; first call downloads. -----
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", project_root],
            "enabled": True,
            "requires_approval": False,
        },
        "memory": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {"MEMORY_FILE_PATH": f"{project_root}/memory-store.json"},
            "enabled": True,
            "requires_approval": False,
        },
        "sequential-thinking": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "enabled": True,
            "requires_approval": False,
        },
        "fetch": {
            "command": venv_py,
            "args": ["-m", "mcp_server_fetch"],
            "enabled": True,
            "requires_approval": False,
        },
        "git": {
            "command": venv_py,
            "args": ["-m", "mcp_server_git", "--repository", project_root],
            "enabled": True,
            "requires_approval": False,
        },
        # Seeded but OFF: its 7 tools duplicate the Playwright browser_* set (which keeps
        # a persistent logged-in Chromium, puppeteer doesn't), and every tool definition
        # ships on every request — the runtime sends ~200 already, past Groq's 128 cap.
        # Kept registered so the MCP page can flip it back on if Playwright ever breaks.
        "puppeteer": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
            "enabled": False,
            "requires_approval": False,
        },
    }
    for name, config in bridges.items():
        # POST /v1/mcp is add-or-replace; safe to re-run every boot.
        _ow_post("/v1/mcp", {"name": name, "config": config})

    # 2. For every persona, enable every currently-connected connector so the
    #    agent actually gets the tools attached (persona defaults otherwise
    #    only auto-enable "core" recommends).
    try:
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}/v1/connectors",
            headers={"x-connect-ai-token": API_TOKEN},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        connected = [c["name"] for c in data.get("connectors", []) if c.get("connected")]
        for persona in ("cowork", "code", "chat", "ops"):
            for conn in connected:
                _ow_post(
                    f"/v1/personas/{persona}/connections",
                    {"connector": conn, "enabled": True},
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[launch] connector-seed failed: {exc}", file=sys.stderr)


def _wait_sidecar_ready(timeout: int = 30) -> None:
    """Block until connect-ai-server answers /v1/health with our token, or timeout.
    Prevents the seed POSTs from racing FastAPI's startup and being dropped."""
    import urllib.error

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://{API_HOST}:{API_PORT}/v1/health",
                headers={"x-connect-ai-token": API_TOKEN},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    print(f"[launch] sidecar did not respond within {timeout}s — seeding may fail", file=sys.stderr)


def _verify_mcp_seed(expected_min: int, log_final: bool = False) -> bool:
    """Return True iff at least `expected_min` MCP entries are visible to the sidecar.
    Optionally prints the final list — used for the last retry so the user sees the
    outcome in the terminal."""
    try:
        req = urllib.request.Request(
            f"http://{API_HOST}:{API_PORT}/v1/mcp",
            headers={"x-connect-ai-token": API_TOKEN},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        servers = data.get("servers", [])
        if log_final:
            print(f"[launch] MCP after retry: {len(servers)} entries", file=sys.stderr)
            for s in servers:
                print(f"           {s['name']:22} {s.get('status')}", file=sys.stderr)
        return len(servers) >= expected_min
    except Exception as exc:  # noqa: BLE001
        print(f"[launch] MCP verify failed: {exc}", file=sys.stderr)
        return False


def _print_setup_banner(google_ok: bool) -> None:
    """One-shot status card. Highlights any optional feature that needs a manual
    setup step so the user knows what to expect from a fresh install."""
    tg_creds = ROOT / "bridge" / ".tg_creds.json"
    tg_session = ROOT / "bridge" / ".tg_session.session"
    telethon_ready = tg_creds.is_file() and tg_session.is_file()
    google_tokens = ROOT / "google-tokens.json"
    google_ready = google_tokens.is_file()

    lines = [
        "",
        "=" * 60,
        "  connect-AI ready",
        "=" * 60,
        f"  Workspace GUI      http://localhost:{GUI_PORT}",
        f"  OpenWorker sidecar http://127.0.0.1:{API_PORT}  (token: {API_TOKEN})",
        f"  Helper / wizards   http://127.0.0.1:{HELPER_PORT}/connectors/wizard",
        f"  Default model      {OW_MODEL}",
        "-" * 60,
        f"  Google (Drive/Gmail/Calendar): {'OK — auto-refresh running' if google_ok else 'NOT SET UP'}",
    ]
    if not google_ready:
        lines.append(
            f"     ⇒ sign in: http://127.0.0.1:{HELPER_PORT}/google/wizard (1-click login)"
        )
    lines.append(
        f"  Telegram full-account (MTProto): {'OK — creds + session found' if telethon_ready else 'NOT SET UP'}"
    )
    if not telethon_ready:
        lines.append(
            f"     ⇒ setup: `{VENV_PY} bridge\\tg_mtproto_mcp.py setup`"
        )
    lines.append("=" * 60)
    lines.append("")
    print("\n".join(lines))


def _installer_cmd() -> Optional[list[str]]:
    """How to add a package to our venv: `python -m pip` when pip is there, else
    `uv pip --python <venv>`. A venv created by `uv venv` (no --seed) has NO pip
    at all, and the old pip.exe check silently skipped every install below —
    leaving the Telegram/computer-use bridges dead with no error anywhere."""
    if subprocess.run(
        [str(VENV_PY), "-m", "pip", "--version"], capture_output=True
    ).returncode == 0:
        return [str(VENV_PY), "-m", "pip", "install", "-q"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", str(VENV_PY), "-q"]
    return None


def _ensure_pip_deps() -> None:
    """Install packages our MCP bridges + Google refresher need but the base
    venv may not carry yet. Idempotent — a no-op when already present."""
    # The root .venv hosts everything: telethon (MTProto), pyautogui (computer
    # use), pillow, and the git/fetch MCP servers (npx alternative).
    missing = [
        pkg
        for pkg, mod in (
            ("telethon", "telethon"),
            ("pyautogui", "pyautogui"),
            ("pillow", "PIL"),
            ("mcp-server-git", "mcp_server_git"),
            ("mcp-server-fetch", "mcp_server_fetch"),
        )
        if subprocess.run(
            [str(VENV_PY), "-c", f"import {mod}"], capture_output=True
        ).returncode
        != 0
    ]
    if not missing:
        return
    installer = _installer_cmd()
    if installer is None:
        print(
            f"[launch] cannot install {', '.join(missing)} — no pip in {VENV_DIR} and "
            f"no uv on PATH. Bridges needing them will not start.",
            file=sys.stderr,
        )
        return
    print(f"[launch] installing into {VENV_DIR.name}: {', '.join(missing)} ...")
    subprocess.run(installer + missing, check=False)


def _preflight_port(host: str, port: int, label: str) -> None:
    """Bail early with a clear message when a previous run leaked a listener.

    Windows: `netstat -ano | findstr :<port>`, then `taskkill /F /PID <pid>`.
    """
    if _port_in_use(host, port):
        _fail(
            f"Port {port} ({label}) is already in use. A previous {label} process "
            f"is probably still running (e.g. this launcher was killed hard instead "
            f"of Ctrl+C). Free it and retry:\n"
            f"  netstat -ano | findstr :{port}\n"
            f"  taskkill /F /PID <pid_from_last_column>"
        )


def _popen_kwargs(log_handle) -> dict:
    kwargs: dict = {
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "cwd": str(ROOT),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _stop(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[launch] Stopping {name}...")
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main() -> None:
    if not SERVER_EXE.exists():
        _fail(
            f"connect-ai-server not found: {SERVER_EXE}\n"
            f"  Create the environment first (once):\n"
            f"    uv venv --python 3.13 .venv\n"
            f"    .venv\\Scripts\\python.exe -m pip install -e connect-ai[messaging,browser]"
        )
    if not (GUI_DIR / "package.json").exists():
        _fail(f"connect-AI GUI missing: {GUI_DIR}")
    if not GEMINI_KEY:
        _fail(
            "GEMINI_API_KEY not set. Copy .env.example → .env and fill it in "
            "(or export GEMINI_API_KEY before launching)."
        )

    _ensure_pip_deps()

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        _fail("npm not found on PATH — the OpenWorker GUI needs Node.js/npm.")

    # Reuse an existing connect-ai-server if it already speaks our token
    # (previous half-crashed launch left just the sidecar alive). Otherwise
    # fail fast on port conflict instead of a cryptic bind error 3s in.
    reuse_sidecar = _ping_our_sidecar()
    if not reuse_sidecar:
        _preflight_port(API_HOST, int(API_PORT), "connect-ai-server")
    # "localhost", not "127.0.0.1": Vite listens on ::1, so the literal IPv4
    # address would resolve to one family and miss the conflict entirely.
    _preflight_port("localhost", int(GUI_PORT), "connect-ai-gui (Vite)")

    LOG_DIR.mkdir(exist_ok=True)
    server_log = (LOG_DIR / "connect-ai-server.log").open(
        "w", encoding="utf-8", buffering=1
    )
    gui_log = (LOG_DIR / "connect-ai-gui.log").open(
        "w", encoding="utf-8", buffering=1
    )

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = GEMINI_KEY
    env["GROQ_API_KEY"] = GROQ_KEY
    if CEREBRAS_KEY:
        env["CEREBRAS_API_KEY"] = CEREBRAS_KEY
    if ANTHROPIC_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_KEY
    if OPENAI_KEY:
        env["OPENAI_API_KEY"] = OPENAI_KEY
    if CLOUDFLARE_TOKEN:
        env["CLOUDFLARE_API_TOKEN"] = CLOUDFLARE_TOKEN
    if CLOUDFLARE_ACCOUNT:
        env["CLOUDFLARE_ACCOUNT_ID"] = CLOUDFLARE_ACCOUNT
    env["CONNECT_AI_API_TOKEN"] = API_TOKEN
    env["COWORKER_API_TOKEN"] = API_TOKEN  # legacy readers (bridges, old scripts)
    # Pin state dir explicitly. Without this, if launch.py is invoked from a
    # sandboxed shell (Claude Desktop's MSIX terminal, Windows Store Python,
    # ...) `state_dir()` can silently resolve to a per-package writable
    # overlay — mcp.json ends up empty for the sidecar even though the real
    # %APPDATA%\coworker\mcp.json is populated. Always use the real one.
    real_appdata = Path(os.path.expandvars("%APPDATA%")) / "coworker"
    if real_appdata.parent.exists():
        env["COWORKER_STATE_DIR"] = str(real_appdata)
    # Vite picks these up as import.meta.env.VITE_* so the GUI hits the same
    # sidecar and passes the same auth header we already configured.
    env["VITE_CONNECT_AI_API_TOKEN"] = API_TOKEN
    env["VITE_COWORKER_API_TOKEN"] = API_TOKEN  # legacy GUI bundles
    env["VITE_COWORKER_HTTP"] = f"http://{API_HOST}:{API_PORT}"
    env["VITE_COWORKER_WS"] = f"ws://{API_HOST}:{API_PORT}"

    if reuse_sidecar:
        print(f"[launch] Reusing existing connect-ai-server on http://{API_HOST}:{API_PORT}")
        server_proc = None
    else:
        print(f"[launch] Starting connect-ai-server on http://{API_HOST}:{API_PORT}")
        server_proc = subprocess.Popen(
            [
                str(SERVER_EXE),
                "--host", API_HOST,
                "--port", API_PORT,
                "--model", OW_MODEL,
                # `auto` = skip approval prompts for every tool call. User asked
                # for "always accept". Swap to `interactive` for a prompt per
                # side-effect action, `plan` to preview before running anything.
                "--mode", "auto",
            ],
            env=env,
            **_popen_kwargs(server_log),
        )

    print(f"[launch] Starting connect-AI GUI (Vite) on http://localhost:{GUI_PORT}")
    gui_proc = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", GUI_PORT, "--strictPort"],
        env=env,
        cwd=str(GUI_DIR),
        stdout=gui_log,
        stderr=subprocess.STDOUT,
        # npm on Windows is a .cmd shim; shell=True lets Windows resolve it.
        shell=sys.platform == "win32",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        ),
    )

    def _cleanup() -> None:
        _stop(gui_proc, "connect-ai-gui")
        _stop(server_proc, "connect-ai-server")
        for handle in (server_log, gui_log):
            try:
                handle.close()
            except Exception:
                pass

    atexit.register(_cleanup)

    # Let the sidecars boot. Vite takes a couple seconds to compile even for
    # a hot cache.
    time.sleep(2.5)
    if server_proc is not None and server_proc.poll() is not None:
        _cleanup()
        _fail(
            f"connect-ai-server exited immediately (code {server_proc.returncode}). "
            f"See {LOG_DIR / 'connect-ai-server.log'}"
        )
    if gui_proc.poll() is not None:
        _cleanup()
        _fail(
            f"connect-ai-gui exited immediately (code {gui_proc.returncode}). "
            f"See {LOG_DIR / 'connect-ai-gui.log'}"
        )

    # Wait for the sidecar to actually accept requests before seeding. Without
    # this we race — the first few POSTs land while FastAPI is still starting
    # up and get dropped silently, leaving the sidecar without MCP entries.
    _wait_sidecar_ready(timeout=30)

    # Seed OpenWorker's model dropdown + MCP bridges + persona connectors. Runs
    # every boot so a fresh sidecar overlay (MSIX quirk) is repopulated instead
    # of coming up empty.
    _seed_runtime_state()

    # Verify all seeded MCPs actually landed. Retry seed once if the sidecar
    # was still finishing internal setup during the first POST batch.
    if not _verify_mcp_seed(expected_min=6):
        print("[launch] MCP seed incomplete — retrying once...", file=sys.stderr)
        time.sleep(2)
        _seed_runtime_state()
        _verify_mcp_seed(expected_min=6, log_final=True)

    # Kick off the Google token refresher if the user has stored a refresh
    # token. Runs as a daemon so it dies with the launcher — no leak.
    google_ok = False
    try:
        import google_auth

        if google_auth.start_refresher(f"http://{API_HOST}:{API_PORT}", API_TOKEN):
            google_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"[launch] google_auth wiring failed (non-fatal): {exc}", file=sys.stderr)

    # Tiny helper HTTP: Google/connectors wizards + outputs/artifacts serving.
    _start_helper()

    # One consolidated status card, printed after all seeding is done. Tells
    # the user which optional features are still missing setup so they aren't
    # surprised by "not connected" errors mid-session.
    _print_setup_banner(google_ok)

    # Foreground wait on the GUI (Vite) — the long-running child. Ctrl+C (or
    # the GUI exiting for any reason) tears everything down via _cleanup.
    try:
        gui_proc.wait()
    except KeyboardInterrupt:
        print("\n[launch] Ctrl+C — shutting down all children.")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
