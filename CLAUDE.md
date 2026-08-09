# connect-AI — Claude Code guide

Desktop AI workspace chạy hoàn toàn local. Runtime + GUI nằm trong `connect-ai/` — bản vendored
của **OpenWorker** (`andrewyng/openworker`) đã patch tại chỗ (xem PATCHES.md). Python FastAPI
backend + React/Vite GUI ("Workspace") + Tauri desktop (chưa build). 40 connector, 14 browser tool
Playwright, 8 crawl tool, scheduled automations, và tập MCP bridge tự viết trong `bridge/`.

## Launcher

`run-web.bat` là entry duy nhất — tự bootstrap lần đầu (tạo `.venv`, cài Python deps + npm +
Chromium) rồi gọi `.venv/Scripts/python.exe launch.py`, spawn:

| Port | Process | Log |
|---|---|---|
| `127.0.0.1:8765` | `connect-ai-server.exe` (FastAPI runtime; auth header `x-connect-ai-token`) | `logs/connect-ai-server.log` |
| `127.0.0.1:8766` | stdlib `http.server` in-process trong `launch.py` — Google wizard, connectors wizard, `/ingest`, serve `/outputs` + `/artifacts` | (không có log file) |
| `localhost:1420` | `npm run dev` trong `connect-ai/surfaces/gui` — **UI chính** | `logs/connect-ai-gui.log` |

`launch.py` còn seed model picker + MCP bridges + bật mọi connected connector cho mọi persona +
start Google token refresher. Foreground wait trên process GUI; `Ctrl+C` → teardown mọi child
(`atexit` + `CTRL_BREAK_EVENT`).

`run.bat` = terminal UI, cùng môi trường và cùng bộ MCP bridge.

## URL nhanh

- Workspace GUI: <http://localhost:1420>
- Runtime health: <http://127.0.0.1:8765/v1/health>
- Google wizard: <http://127.0.0.1:8766/google/wizard>
- Connectors wizard: <http://127.0.0.1:8766/connectors/wizard>
- Ingest (bookmarklet, luồng không-CDP): <http://127.0.0.1:8766/ingest>

## Tên và biến — cái nào đổi được, cái nào không

Repo đã rebrand OpenWorker → connect-AI. Quy tắc: **thứ người dùng thấy thì đổi, wire protocol thì
nhận cả hai**.

| Hạng mục | Tên hiện tại | Tương thích ngược |
|---|---|---|
| Thư mục runtime | `connect-ai/` | — (git mv, history giữ nguyên) |
| CLI | `connect-ai`, `connect-ai-server`, `connect-ai-connectors` | `openworker*` vẫn đăng ký trong pyproject |
| Auth header | `x-connect-ai-token` | server nhận cả `x-openworker-token` |
| WS subprotocol | `connect-ai` | server echo đúng nhãn client gửi (`_accept_subprotocol`) |
| Env token (user) | `CONNECT_AI_API_TOKEN` trong `.env` | đọc fallback `COWORKER_API_TOKEN` |
| Env token (nội bộ) | `COWORKER_API_TOKEN` | **giữ nguyên** — `run.py` ghi tên này, cả test suite dựa vào nó |
| Bridge base URL | `CONNECT_AI_BASE` | fallback `OW_BASE` |
| Log | `connect-ai-server.log`, `connect-ai-gui.log` | — |
| GUI package | `connect-ai-gui` | — |

**KHÔNG đổi** (đổi là gãy, không đem lại gì cho user): Python package `coworker`, state dir
`%APPDATA%\coworker\` (chứa secrets/sessions/mcp.json — đổi = mất hết kết nối hiện có).

## Cấu hình secret

`.env` (gitignored) — `launch.py` load bằng parser stdlib nhỏ, `run.bat` load qua `for /f`.
Copy `.env.example` → `.env`:
- `GEMINI_API_KEY` — bắt buộc
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` — optional, có key thì model vào picker
- `CONNECT_AI_API_TOKEN` — token chung runtime 8765 + GUI 1420 + helper 8766

Token inject vào Vite qua `VITE_CONNECT_AI_API_TOKEN` (kèm `VITE_COWORKER_API_TOKEN` cho bundle cũ).

**Lưu ý**: 2 key gốc từng bị hardcode (Gemini + Groq) — rotate trước khi push public.

## Google OAuth — 1 login = TOÀN BỘ Google

**Luật bất di bất dịch**: Google là *một tài khoản*, không phải 3 connector. Một lần đăng nhập
kết nối luôn `gmail` + `google_calendar` + `google_drive`; sign out thu hồi quyền tại Google +
ngắt cả 3. **Không có chỗ nào trong UI cho dán access_token Google** — thêm connector Google mới
thì thêm tên vào `google_auth.CONNECTORS` + `GOOGLE_CONNECTORS` (api.ts) + scope vào
`DEFAULT_SCOPES`, không tạo đường credential riêng.

- GUI `startGoogleLogin()` → `GET /google/auth-start` (helper 8766 mở tab consent) →
  `/google/auth-callback` lưu token + push cả 3 connector. GUI chỉ poll connector flip.
- Cần `google-oauth.json` (Desktop OAuth client của user) — lưu 1 lần qua wizard
  (`POST /google/oauth-client`). Chưa có → `startGoogleLogin()` fallback mở wizard; card Google
  đổi nhãn thành "Set up Google sign-in" thay vì giả vờ mở consent.
- Endpoint helper: `GET /google/setup-state` → `{has_client, accounts, services}`;
  `POST /google/connect-all` (nối lại cả 3, không cần consent); `POST /google/logout` `{account?}`
  (revoke + xoá token + disconnect; thiếu `account` = tất cả); `/google/disconnect` = alias logout.
- `google_auth.sync_all_accounts(only_connected=…)` refresh **mọi** account mỗi 50 phút.
  `only_connected=True` (refresher) chỉ push vào connector/account runtime đang giữ → user ngắt
  Drive bằng tay thì nó ở yên; ngoại lệ: runtime chưa có Google nào → coi như boot đầu, nối hết.
- CLI: `python google_auth.py setup|status|refresh|connect-all|logout [account]`.

## Hết quota model → đổi model / park rồi chạy tiếp

- `classify_model_error()` phân `QUOTA` / `RATE_LIMIT` / `NO_ACCESS`. `friendly_model_error()` giữ
  hợp đồng cũ (429 thường vẫn trả `None`).
- `TurnEngine._recover_from_model_error()`: quota/no-access → đánh dấu model "exhausted" →
  `switch_model()` sang model kế trong `fallback_models()` (manager truyền `usable_models`, đúng
  danh sách picker) → emit `MODEL_FAILOVER` → retry, **không tốn** iteration budget.
- Hết model → `_wait_for_capacity()`: park turn, backoff 20/45/90/180/300s, emit `MODEL_WAITING`,
  tổng thời gian chờ giới hạn bởi `COWORKER_QUOTA_WAIT_SECONDS` (mặc định 900s, `0` = tắt). Sau
  mỗi lần chờ xoá danh sách exhausted để thử lại cả list. Stop → `INTERRUPTED`, không phải error.
- `NO_ACCESS` không bao giờ chờ. Lỗi thường vẫn kết thúc turn như cũ.
- Test: `connect-ai/tests/test_quota_failover.py` (13 test).

## MCP bridges

Đăng ký trong `%APPDATA%\coworker\mcp.json` — `launch.py` (`_seed_mcp_servers`) seed lại mỗi boot
qua API runtime. Bridge tự viết trong `bridge/`: telegram-bot, telegram-mtproto, subagents, skills,
artifacts, commands, computer-use. Third-party: filesystem, memory, sequential-thinking, fetch,
git, puppeteer (`git`/`fetch` chạy bằng `.venv` ở root).

**Runtime 8765 CÓ nạp MCP** (đo 2026-08-08: log boot cho thấy cả 14 server trả `ListToolsRequest`;
ghi chú cũ "chỉ TUI mới có" là sai). Hệ quả cần nhớ: mỗi request mang **~200 tool definition** —
119 từ bridge nội bộ (riêng `telegram-bot` **91**), ~45 từ bridge bên thứ ba, ~40 built-in
(browser 14 + crawl 8 + core). Runtime **không** cắt bớt theo trần của provider, nên
`groq:*` trả `400 "tools": maximum number of items is 128`. `cloudflare:@cf/openai/gpt-oss-120b`
nuốt 200 tool bình thường (đã test). Tool nhiều cũng làm model yếu chọn sai tool.

## Layout

```
AI-cowork/
├── .venv/                  Python 3.13 — MÔI TRƯỜNG DUY NHẤT, ở ROOT (launch.py `_find_venv`)
├── connect-ai/             runtime + GUI (vendored OpenWorker, đã patch)
│   ├── coworker/             backend package (tên package giữ nguyên, cố ý)
│   └── surfaces/gui/         Workspace GUI (Vite React, :1420)
├── bridge/                 MCP stdio server tự viết
├── skills/  commands/      skill pack + slash-command template
├── logs/                   connect-ai-server.log + connect-ai-gui.log
├── launch.py               launcher: runtime + GUI + helper 8766
├── google_auth.py          Google OAuth refresher
├── run-web.bat  run.bat    entry GUI / terminal UI
└── README · SETUP · TOOLS · AUTOMATIONS · PATCHES
```

## Convention khi Claude làm việc trên repo này

- **KHÔNG** sửa file trong `connect-ai/` trừ khi user yêu cầu rõ — đây là vendored upstream, ưu
  tiên patch qua wrapper (`launch.py`, `bridge/`). Bắt buộc sửa thì **ghi vào PATCHES.md** để lần
  merge upstream sau còn đối chiếu.
- Path Windows: backslash trong `.bat`, forward slash trong Python string, đừng trộn.
- Log filename theo pattern `logs/<service>.log` — `launch.py` mở/đóng handle theo tên này.
- Endpoint mới cho runtime tự động cần header token (middleware check sẵn).
- User dùng PowerShell + Git Bash trên Windows — lệnh nên POSIX-friendly khi có thể; bắt buộc
  Windows-only thì nói rõ.
- User trao đổi bằng **tiếng Việt** — trả lời tiếng Việt trừ khi đang viết code/comment.
- README.md tiếng Anh (public repo); SETUP/TOOLS/AUTOMATIONS tiếng Việt.

## Troubleshoot nhanh

- **Port bị chiếm**: `netstat -ano | findstr :8765` → `taskkill /F /PID <pid>`. `_port_in_use()`
  dò cả IPv4 lẫn IPv6 (Vite bind `::1`) nên launcher báo rõ thay vì để Vite chết câm.
- **Runtime restart nhưng GUI vẫn cũ**: launcher reuse runtime nếu 8765 trả đúng token — kill
  process cũ trước nếu code backend đổi.
- **Model out-of-quota trong picker**: `launch.py` hide model hỏng mỗi boot; còn thấy thì
  `POST /v1/settings/models/remove`.
- **`/v1/mcp` trả `[]` dù mcp.json có entries**: phải launch từ terminal NATIVE. MSIX
  AppContainer của Claude Desktop redirect file I/O sang overlay riêng cho từng child;
  `COWORKER_STATE_DIR` không cứu được vì đây là redirect ở tầng OS.
- **Agent trả sai ngôn ngữ / đi lạc quy trình**: sửa `AGENTS.md` ở **root repo**, launcher tự
  copy sang `%APPDATA%\coworker\AGENTS.md` mỗi boot (`_seed_agents_md`) và runtime nhét nó vào
  mọi system prompt. Đừng sửa persona trong `connect-ai/coworker/agents/`, cũng đừng sửa thẳng
  bản trong state dir — lần launch sau bị ghi đè.
- **Google 401**: card Google ▸ Reconnect all, hoặc `POST http://127.0.0.1:8766/google/connect-all`.

## Trạng thái đã biết

- Chưa build Tauri desktop (cần Rust toolchain).
- Cần user tạo Google OAuth client 1 lần (wizard 8766) để kích hoạt Google connectors —
  automation không tự làm bước này được.
- Backend test: **947 pass / 13 fail**. 13 fail là **có sẵn từ upstream trên Windows**
  (slack_relay timeout, standing_approvals, ui_refresh_e2e, config, environment) — đã verify bằng
  cách stash patch chạy lại, không liên quan thay đổi của repo này.

## Đã fix

- **Ingest tự động qua Chrome extension (2026-08-08)**: Shopee chặn CDP ngay request đầu — đo cả
  Chromium bundled, Chrome thật (`channel="chrome"`), lẫn warm-up qua trang chủ, tất cả rơi vào
  `/verify/traffic/error`. Không có cờ Playwright nào né được, nên bỏ hẳn browser tool cho site
  loại này. Thay bằng job queue ở helper 8766 (`/ingest/job` GET vì `web_fetch` không POST được →
  `/ingest/jobs` long-poll → `POST /ingest` → `/ingest/result`) + MV3 extension trong
  `browser-extension/` chạy bằng cookie Chrome thật của user. Agent chỉ gọi web_fetch, user cài
  extension một lần. Luật ở `AGENTS.md`; test ở `tests/test_ingest.py`.
- **Dọn picker + luồng ingest không-CDP (2026-08-08)**: picker giờ chỉ chứa model **thật sự chạy
  được**. Bỏ `groq:llama-3.3-70b-versatile` (free tier chết 2 đường: 400 vì trần 128 tool, 413 vì
  12k TPM < 1 lượt agent) và `cloudflare:google/gemini-3.6-flash` (402 khi AI Gateway chưa nạp
  tiền). Thêm `cloudflare:@cf/*` chạy bằng neuron free; default → `@cf/openai/gpt-oss-120b` vì
  Gemini free tier cạn từ giữa buổi sáng. **Bài học**: `models/add` ghi bền, nên chỉ "bỏ qua bước
  add" là chưa đủ — entry cũ sống mãi và đầu độc chuỗi quota-failover; phải `models/remove` chủ
  động. Kèm `/ingest` ở helper 8766 (bookmarklet → JSON + CSV vào `outputs/`) cho site chặn
  automation; test ở `tests/test_ingest.py` (8 test, chạy `python -m pytest tests/` từ root).
- **Rebrand hoàn tất (2026-08-07)**: `openworker/` → `connect-ai/` (git mv, 430 file giữ history);
  CLI → `connect-ai*`; header → `x-connect-ai-token`; WS subprotocol client-driven; env
  `CONNECT_AI_API_TOKEN` / `CONNECT_AI_BASE`; log + GUI package đổi tên; toàn bộ docs viết lại.
  Bẫy gặp phải: `run.py` ghi tên env mới làm rò biến giữa các test (conftest chỉ xoá tên cũ) →
  giữ tên nội bộ, chỉ đổi tên user-facing.
- **Venv ra root (2026-08-07)**: `openworker/.venv` → `.venv`. `launch.py._find_venv()` ưu tiên
  root, fallback path cũ. `run-web.bat` tự bootstrap toàn bộ.
- **`uv venv` không tạo pip (2026-08-07)**: `_ensure_pip_deps()` kiểm tra `pip.exe` rồi mới cài →
  im lặng bỏ qua telethon/pyautogui/mcp-server-*. Giờ dùng `python -m pip`, fallback `uv pip`,
  không có cả hai thì cảnh báo ra stderr. `run-web.bat` thêm `--seed`.
- **Launcher chết vì Unicode (2026-08-07)**: banner in `⇒` làm console cp1252 raise
  `UnicodeEncodeError` *sau khi* child đã lên. Fix: reconfigure stdout/stderr UTF-8 +
  `errors="replace"`.
- **Preflight port bỏ sót IPv6 (2026-08-07)**: Vite bind `::1`, `_port_in_use` chỉ dò AF_INET →
  thông báo hữu ích bị thay bằng "exited immediately (code 1)". Giờ dò qua `getaddrinfo`.
- **Wizard Google im lặng khi thiếu OAuth client (2026-08-07)**: `/google/auth-start` trả 400
  nhưng JS nuốt lỗi → nút trông như chết. Giờ đổi nhãn nút, hiện thông báo, cuộn + focus xuống ô
  dán client.
- **1 login Google = toàn bộ Google (2026-08-07)** — xem mục Google ở trên + PATCHES.md.
- **Quota failover + park/resume (2026-08-07)** — xem mục quota ở trên.
- Bỏ toàn bộ UI cloud sign-in (2026-08-06); hợp nhất browser tools (2026-08-06); xoá DeepTutor
  (2026-08-06); `.env` + `.gitignore` tách secrets (2026-07-31); `AGENTS.md` pin tiếng Việt.
