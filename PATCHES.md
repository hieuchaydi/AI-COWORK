# Upstream patches — inventory

`connect-ai/` là bản vendored của upstream `andrewyng/openworker` **đã patch trực tiếp và track đầy đủ trong repo này** — clone repo này là có sẵn mọi patch, không cần áp lại gì. File này chỉ là inventory những gì khác upstream, để dễ đối chiếu khi merge upstream mới. (DeepTutor đã bị xóa khỏi project 2026-08-06.)

## Khác biệt so với upstream

### Giờ Việt Nam (timezone pin)
- `coworker/environment.py` — session start chỉ in date + pointer sang `<current-time>` block.
- `coworker/agent.py` — inject `<current-time>` block (UTC+7, env `TZ_OFFSET_HOURS`/`TZ_LABEL`) mỗi turn; register tool `current_time`.
- `coworker/tools/clock.py` — file mới: tool `current_time()` (offset_minutes → target.iso cho scheduler).
- `coworker/permissions.py` — loose match theo colon-tail cho scheduled-task grants.

### Bỏ cloud, local-first (2026-08-06)
- `surfaces/gui/` — xóa `CloudSignIn.tsx` + toàn bộ `cloudLogin/cloudLogout/getCloudStatus/waitForCloudSignIn/connectManaged` trong `api.ts`; Google connect qua wizard local 8766 (`openGoogleWizard()`); connector khác chỉ còn manual token / MCP OAuth local; Onboarding/AutomationQuickstart/GalleryModal bỏ gate sign-in; rebrand "AI cowork" → "Workspace".

### 1 login Google = toàn bộ Google (2026-08-07)
- `coworker/connectors/descriptors.py` — thêm `_validate_gmail` + `_validate_google_calendar`, gắn `validate=` cho 2 connector đó (account key theo email thật, multi-account mới đúng); instructions/help của gmail + google_calendar + google_drive đổi sang "sign in once", bỏ hướng dẫn dán access token.
- `surfaces/gui/src/components/connectors/GoogleAccountCard.tsx` — file mới: card Google (1 tài khoản, 3 chip dịch vụ, Make default / Reconnect all / Sign out) đứng đầu trang Connectors.
- `surfaces/gui/src/api.ts` — `GoogleAccount`/`GoogleService`/`GoogleState` + `getGoogleState`, `googleConnectAll`, `googleSignOut`, `googleActivate`.
- `surfaces/gui/src/components/ManageTabs.tsx` — `ConnectSetup` tách thành dispatcher: connector Google → `GoogleConnectSetup` (nút sign-in), còn lại → `ManualConnectSetup`.
- `surfaces/gui/src/components/connectors/{ConnectorsList,ConnectorsSection,GmailDetail,CalendarDetail}.tsx`, `components/Onboarding.tsx`, `e2e/fixtures.ts` — gắn card, `google_drive` dùng `AccountsDetail`, copy "one sign-in", mock `setup-state` kèm `services`.

### Quota failover + park/resume (2026-08-07)
- `coworker/providers/errors.py` — thêm `classify_model_error()` (QUOTA/RATE_LIMIT/NO_ACCESS); `friendly_model_error()` viết lại trên nền nó, hành vi cũ giữ nguyên.
- `coworker/engine.py` — `fallback_models` param; `_recover_from_model_error()` + `_wait_for_capacity()` + `_next_model()`; nhánh except của `_loop` gọi recovery trước khi bỏ cuộc; failover không tốn iteration budget; Stop khi đang park → INTERRUPTED.
- `coworker/events.py` — thêm `MODEL_FAILOVER`, `MODEL_WAITING`.
- `coworker/agent.py` — `build_engine(fallback_models=...)` truyền xuống engine.
- `coworker/server/manager.py` — tách `usable_models()` khỏi `get_settings()`, truyền vào cả 2 call site `build_engine` (session + scheduled task).
- `surfaces/gui/src/{App.tsx,types.ts}` — render 2 event mới; notice `waiting` tự thay thế và tự dọn.
- `tests/test_quota_failover.py` — file mới, 13 test.

### Provider Cloudflare Workers AI (2026-08-08)
Partner model (`google/gemini-3.6-flash`, …) qua endpoint OpenAI-compatible của Cloudflare. Không
dùng được `_compat()` vì URL scope theo account (`/accounts/<id>/ai/v1`) nên phải build động.
- `coworker/providers/registry.py` — `_cloudflare_base_url()` + `_build_cloudflare()` + descriptor
  `cloudflare` (3 field: api_key / account_id / base_url override → trỏ AI Gateway
  `…/v1/<acct>/<gateway>/compat`). Fallback env `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`,
  **không** mượn key OpenAI (cùng luật với `_openai_compat`).
- `coworker/providers/matrix.py` — `cloudflare:@cf/openai/gpt-oss-120b` (128k),
  `cloudflare:@cf/meta/llama-3.3-70b-instruct-fp8-fast` (24k),
  `cloudflare:google/gemini-3.6-flash` (1.048.576) — `_AGENTIC`, bỏ vision/pdf giống reseller vì
  compat surface không có inline file part. **Hai thế giới billing sau cùng 1 token**: `@cf/*` tiêu
  neuron allocation của account, partner model đòi tiền trong AI Gateway (chưa nạp → 402
  "Insufficient balance"). Mặc định picker chỉ lấy `@cf/*` cho khỏi có entry chết.
- `launch.py` — đọc 2 env, forward xuống runtime, seed profile qua `POST /v1/providers`, thêm model
  vào picker **chỉ khi có đủ cả 2** (thiếu 1 nửa thì model nằm chờ để fail lúc chạy). Danh sách
  model đổi bằng `CLOUDFLARE_MODEL` (ngăn cách dấu phẩy).
- `coworker/providers/registry.py` (`verify_provider_key`) — nhánh `cloudflare` riêng: compat
  surface trả **405** cho `GET /models` và descriptor không có endpoint prefill, nên nhánh generic
  sẽ bắn token Cloudflare vào `api.openai.com`. Giờ check `/accounts/<id>/ai/models/search` (đúng
  permission cần có), 401/403 trả message chỉ thẳng "cần ACCOUNT token có quyền Workers AI".
- `launch.py` — `_ow_post()` trả JSON (trước trả `None`); gate model Cloudflare bằng
  `POST /v1/providers/verify` trước khi add vào picker. Lý do: failover quota duyệt cả picker, một
  entry 401 ở cuối chuỗi biến "hết quota, thử tiếp" thành turn chết hẳn. Chỉ auth-reject mới ẩn
  model — 404/timeout không chứng minh được gì nên vẫn cho vào.
- `coworker/providers/openai_provider.py` — `default_max_tokens` ctor param +
  `_apply_output_budget()` gọi trong cả `complete()` và `stream()`. **Workers AI mặc định
  `max_tokens=256`** khi request không ghi (đo 2026-08-08: 256/256 completion token, `content`
  rỗng, `finish_reason=length`) — model reasoning như gpt-oss-120b đốt sạch vào phần nghĩ, turn
  chết câm giữa câu, không có tool call nào. Chỉ provider nào opt-in mới được điền; caller ghi
  rõ thì luôn thắng. `registry.CLOUDFLARE_MAX_TOKENS = 8192` (vừa context nhỏ nhất 24k của
  llama-3.3-70b-fp8-fast).
- `tests/test_providers.py` — `test_cloudflare_builder_composes_account_scoped_endpoint`,
  `test_output_budget_only_fills_a_gap_and_only_when_opted_in`,
  `test_cloudflare_provider_ships_an_output_budget`;
  `tests/test_provider_verify.py` — `test_verify_cloudflare_checks_the_workers_ai_catalog`.

### Artifact rail nhìn thấy `outputs/` (2026-08-08)
Tools ghi file vào `<project>/outputs/<kind>/` (patch "Browser tools hợp nhất" bên dưới), nhưng
artifact resolver của upstream chỉ tìm trong workspace của session (`~/OpenWorker/<session_id>`)
→ chip `[…](artifact:outputs/csv/x.csv)` trả `not found`, rail không list file nào.
- `coworker/server/manager.py` — thêm `_within()` + `_unified_output_root()` (mirror
  `crawl._output_root()`, tôn trọng `COWORKER_OUTPUT_DIR`); `_artifact_target()` fallback sang
  outputs tree khi workspace không có (nhận cả `outputs/csv/x.csv`, `csv/x.csv` lẫn path tuyệt
  đối tool trả về) — read/open/reveal dùng chung nên cả 3 cùng hoạt động; `list_artifacts()` quét
  thêm outputs root, prefix `outputs/`, dedupe theo abs path, workspace vẫn ưu tiên khi trùng tên.
  Guard path-escape giữ nguyên cho cả hai root.
- `tests/test_server.py` — thêm `test_artifacts_resolve_shared_outputs_tree`.

### Browser tools hợp nhất (2026-08-06)
- `coworker/tools/browser.py` — file mới: 13 tool Playwright persistent-Chromium (bộ duy nhất).
- `coworker/tools/crawl.py` — file mới: crawl/scrape tools (httpx + BeautifulSoup).
- `coworker/connectors/integration_tools.py` — gỡ `make_browser_automation_tools()` (bộ cũ retired).
- `coworker/connectors/tool_defs.py` — card Browser liệt kê 13 tool mới.
- `coworker/agent.py` — register browser/crawl tools, filter theo per-tool toggles.
- `tests/` — cập nhật tên tool mới.

### Cap số tool theo trần provider (2026-08-09)
- `coworker/providers/openai_provider.py` — thêm `_TOOL_CAPS` (keyed theo substring base_url,
  `api.groq.com → 128`), `_tool_cap_for`, `_tool_rank`, `_cap_tools`. `complete()` + `stream()`
  gọi `_cap_tools(tools, self._base_url)` trước khi gán `kwargs["tools"]`. Runtime ship ~200 tool
  mỗi request; Groq cap 128 → `400 "tools": maximum number of items is 128`. Provider tự trim khi
  vượt cap: giữ built-in (không prefix `mcp__`, rank 2) + bridge thường (rank 1) trước, hạ ưu tiên
  `telegram_bot`/`telegram_mtproto` (rank 0, cồng kềnh 91 tool) nên rớt đầu tiên. Stable-sort giữ
  nguyên thứ tự gốc trong survivors; log WARNING khi trim. Provider không cap (Cloudflare, OpenAI,
  Gemini…) đi thẳng, không đổi. Muốn thêm provider/đổi ngưỡng: sửa `_TOOL_CAPS`; đổi server bị hạ
  ưu tiên: sửa `_DEPRIORITIZED_SERVERS`.
- `tests/test_providers.py` — 3 test mới: groq trim về 128 giữ tool quan trọng + đúng thứ tự;
  provider uncapped gửi full; dưới cap là no-op.
- `launch.py` — re-enable `groq:llama-3.3-70b-versatile` trong picker (đặt CUỐI list để failover
  chỉ ghé sau các model free) + bỏ khỏi `hide` list. Trước đó groq bị gỡ mỗi boot vì lỗi 400 (nay
  đã fix ở tầng provider). Caveat vẫn còn: free tier 12k TPM → 413 với 1 lượt agent >14k token,
  cần Dev-tier key.

### Thêm provider Cerebras (2026-08-09)
- `coworker/providers/registry.py` — thêm `_compat("cerebras", "Cerebras",
  base_url="https://api.cerebras.ai/v1", recommended_model="gpt-oss-120b",
  env_key="CEREBRAS_API_KEY")`. OpenAI-compatible, key auto-resolve từ env. Model id 2026-08:
  `gpt-oss-120b` (tool-use tốt nhất), `gemma-4-31b`, `zai-glm-4.7` (sắp deprecate).
  `llama-3.3-70b`/`qwen-3-32b` đã retired 2026-02-16 — không dùng.
- `launch.py` — `CEREBRAS_KEY` từ env; seed `cerebras:gpt-oss-120b` vào picker khi có key; truyền
  `CEREBRAS_API_KEY` xuống runtime subprocess.
- `.env.example` — thêm block `CEREBRAS_API_KEY=`.

### Gemini 3 thought_signature — không 400 khi thiếu chữ ký (2026-08-09)
- `coworker/providers/gemini_provider.py` — Gemini 3 hard-reject (`400 INVALID_ARGUMENT
  "Function call is missing a thought_signature in functionCall parts"`) mọi function_call replay
  thiếu `thought_signature`. Xảy ra khi tool call trong history KHÔNG sinh từ Gemini 3 có chữ ký:
  quota-failover sang model khác, turn cũ trước khi có capture, hoặc Gemini tự sign thiếu ở
  parallel calls (preview quirk). Thêm hằng `_SKIP_SIGNATURE = base64("skip_thought_signature_validator")`
  (sentinel chính thức của Google — API bỏ qua validation cho part đó). `convert_messages` giờ gán
  `part["thought_signature"] = sig or _SKIP_SIGNATURE` cho mọi function_call: chữ ký thật khi có,
  không thì sentinel. Chữ ký thật KHÔNG bị ghi đè.
- `tests/test_gemini_provider.py` — cập nhật 2 assertion cũ (function_call không chữ ký giờ mang
  sentinel) + test mới `test_convert_unsigned_calls_get_skip_sentinel` (tool turn từ model khác,
  không sidecar → mọi call mang sentinel).

## AGENTS.md pin

`launch.py._seed_agents_md()` tự copy `AGENTS.md` (root repo) → `%APPDATA%\coworker\AGENTS.md` mỗi boot; `coworker/project.py` nạp vào system prompt của **mọi** persona. Sửa bản ở root, đừng sửa bản trong state dir (lần boot sau bị ghi đè). Pin tiếng Việt + timezone + rule scheduled task + thang bậc cào web.

> Trước 2026-08-08 bước copy là thủ công và **chưa từng được làm** — file không tồn tại trong state dir, nên mọi luật trong đó chưa bao giờ có hiệu lực. Hành vi tiếng Việt lâu nay đến từ persona prompt chứ không phải từ file này.
