# connect-AI — hướng dẫn setup

Đọc từ trên xuống. Mục 0 và 1 là bắt buộc; từ mục 2 trở đi mỗi phần độc lập, làm cái nào cần trước.

> **Chạy từ terminal native** (Windows → gõ `cmd` → Enter). Đừng chạy từ terminal bên trong
> Claude Desktop: MSIX file redirection khiến runtime và launcher đọc hai overlay khác nhau,
> hậu quả là runtime không thấy MCP entries.

---

## 0. Chạy lần đầu

```bat
cd C:\Users\<bạn>\Desktop\AI-cowork
copy .env.example .env      :: rồi điền GEMINI_API_KEY
run-web.bat
```

`run-web.bat` tự lo mọi thứ ở lần chạy đầu — không phải gõ lệnh cài nào:

| Bước | Chạy khi | Làm gì |
|---|---|---|
| 1 | thiếu `.venv` | `uv venv --seed --python 3.13` (không có uv → `py -3.13 -m venv`) |
| 2 | thiếu `connect-ai-server.exe` | cài `connect-ai[messaging,browser,bedrock,dev]` |
| 3 | thiếu `node_modules` | `npm install` cho GUI |
| 4 | thiếu `%LOCALAPPDATA%\ms-playwright` | tải Chromium cho browser tools |
| 5 | luôn | `launch.py` → runtime + helper + GUI |

Lần đầu mất vài phút (~250 MB Python deps + ~115 MB Chromium), các lần sau ~10 giây.

Sau khi boot:

| URL | Là gì |
|---|---|
| <http://localhost:1420> | **Workspace GUI** — giao diện chính |
| <http://127.0.0.1:8765/v1/health> | agent runtime health |
| <http://127.0.0.1:8766/google/wizard> | wizard đăng nhập Google |
| <http://127.0.0.1:8766/connectors/wizard> | wizard connector (Telegram, token-based) |

**Tắt bằng `Ctrl+C`** trong đúng cửa sổ đó — launcher teardown cả runtime lẫn GUI. Đóng cửa sổ
ngang thì tiến trình con sống sót và lần chạy sau báo "Port 1420 already in use".

`run.bat` mở terminal UI thay vì GUI, dùng chung môi trường và chung bộ MCP bridge.

---

## 1. API keys (`.env`)

`.env` bị gitignore. Copy từ `.env.example` rồi điền:

| Biến | Bắt buộc | Lấy ở đâu | Tác dụng |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | <https://aistudio.google.com/apikey> | model mặc định, free tier rộng |
| `ANTHROPIC_API_KEY` | khuyến nghị | <https://console.anthropic.com/settings/keys> | Claude Haiku/Sonnet/Opus — tool-use tốt nhất; có key thì default tự chuyển sang Haiku |
| `GROQ_API_KEY` | nên có | <https://console.groq.com/keys> | Llama 3.3 70B, free. Đáng set kể cả không dùng: cho quota failover chỗ để nhảy sang |
| `OPENAI_API_KEY` | tùy | <https://platform.openai.com/api-keys> | thêm GPT vào picker |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | tùy | My Profile ▸ API Tokens ▸ Create Token ▸ **Workers AI**; account id nằm trong URL `dash.cloudflare.com/<id>/` | thêm `@cf/openai/gpt-oss-120b` + `@cf/meta/llama-3.3-70b-instruct-fp8-fast` (chạy bằng neuron free của account). Cần **cả hai** biến — endpoint scope theo account. Partner model (`google/gemini-3.6-flash`) phải nạp tiền vào AI Gateway, chưa nạp thì 402; nạp xong thì liệt kê vào `CLOUDFLARE_MODEL` (ngăn cách bằng dấu phẩy) |
| `CONNECT_AI_API_TOKEN` | ✅ | tự đặt chuỗi bất kỳ | secret chung giữa runtime :8765, GUI :1420, helper :8766 |

Thêm key xong phải **restart launcher** — model tương ứng mới xuất hiện trong picker.

> Tên cũ `COWORKER_API_TOKEN` vẫn được chấp nhận (đọc sau tên mới), nên `.env` cũ không gãy.

---

## 2. Google (Gmail + Calendar + Drive) — 1 lần đăng nhập

**Một login = toàn bộ Google.** Không kết nối riêng từng dịch vụ, không dán token cho từng cái.

Bấm **Sign in with Google** trên card *Google* ở đầu trang Connectors → chọn tài khoản → xong:
Gmail, Google Calendar và Google Drive cùng lên bằng tài khoản đó. Token lưu trong
`google-tokens.json` trên máy, tự gia hạn mỗi 50 phút.

Card Google là nơi duy nhất quản lý:

| Nút | Việc nó làm |
|---|---|
| Sign in with Google / ＋ Add account | mở consent → nối Gmail + Calendar + Drive |
| Make default | đặt tài khoản đó làm mặc định cho cả 3 dịch vụ |
| Reconnect all | nối lại dịch vụ đang rớt từ token đã lưu, không cần consent lại |
| Sign out | thu hồi quyền tại Google + xoá token local + ngắt cả 3 |

CLI tương đương: `python google_auth.py status | connect-all | logout [account]`.

### Lần đầu: tạo OAuth client (1 lần, ~3 phút)

Google bắt buộc OAuth client phải là của bạn (ở đây không có cloud trung gian). Chưa có thì nút
login tự mở wizard <http://127.0.0.1:8766/google/wizard>:

1. Mở [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials),
   tạo project miễn phí nếu chưa có
2. **Create Credentials → OAuth client ID** → Application type: **Desktop app** → Create
   - Nếu bị hỏi consent screen: chọn **External** → điền tên app + email → Save
   - App ở chế độ Testing thì **thêm email của bạn vào Test users**, không thì Google chặn ở
     bước consent
3. **Download JSON** → dán vào ô trong wizard → **Lưu client**

Từ đó mọi lần đăng nhập (kể cả thêm tài khoản thứ hai) chỉ còn 1 click.

### Cách khác — không cần tạo client

Trong wizard mở mục "Cách khác": lấy `refresh_token` từ
[OAuth Playground](https://developers.google.com/oauthplayground/) (bật *Access type: Offline*,
chọn scope `gmail.modify` + `drive` + `calendar`) rồi dán vào. CLI: `python google_auth.py setup`.

**401 khi gọi Drive/Gmail** → bấm **Reconnect all** (hoặc `python google_auth.py connect-all`).
Vẫn 401 nghĩa là refresh_token bị revoke → Sign out rồi đăng nhập lại.

---

## 3. Telegram Bot (14 tool, không cần setup thêm)

Bot token đã lưu trong secrets của runtime. Test bằng cách gõ trong chat:

```
Dùng telegram-bot get_me
```

**Tool có sẵn**: `get_me`, `get_chat`, `get_chat_member`, `get_chat_administrators`,
`get_chat_member_count`, `get_updates`, `get_webhook_info`, `send_message`, `send_photo`,
`send_document`, `edit_message_text`, `delete_message`, `forward_message`, `set_my_commands`.

**Giới hạn của Bot API** (không phải lỗi):
- Đọc được: tin nhắn user DM cho bot (qua `get_updates`)
- Không đọc được: chat riêng của bạn với người khác, group mà bot không có mặt, lịch sử tin bot đã gửi
- Không gửi được cho user chưa từng DM bot — luật của Telegram

Muốn đọc mọi chat → dùng MTProto ở mục 4.

---

## 4. Telegram MTProto (đăng nhập bằng tài khoản cá nhân)

Đọc được mọi chat/group/channel mà tài khoản bạn thấy.

### 4.1 Lấy api_id + api_hash

1. <https://my.telegram.org> → login bằng số điện thoại → OTP về app Telegram
2. Vào **API development tools** (không phải "MTProto servers")
3. Tạo app: title tuỳ ý, platform **Desktop**, URL/Description để trống → **Create application**
4. Copy `App api_id` (7–8 chữ số) và `App api_hash` (32 ký tự hex)

### 4.2 Ghi creds

Tạo `bridge/.tg_creds.json`:

```json
{
  "api_id": 12345678,
  "api_hash": "0123456789abcdef0123456789abcdef",
  "phone": "+84987654321"
}
```

Phone: mã quốc gia (`+84`), bỏ số `0` đầu.

### 4.3 Đăng nhập

```bat
.venv\Scripts\python.exe bridge\tg_mtproto_mcp.py setup
```

Bridge gửi OTP vào app Telegram → paste 5 số → (có 2FA thì nhập tiếp) → session lưu ở
`bridge/.tg_session.session`.

### 4.4 Kiểm tra

```
Dùng telegram-mtproto list_dialogs 10
```

**7 tool**: `get_me`, `list_dialogs`, `get_history`, `search_messages`, `send_message_as_me`,
`download_media`, `get_chat_info`. Chat resolver nhận `@username`, số điện thoại, numeric id,
hoặc tên chat (fuzzy match).

**Lỗi hay gặp**: "session not authorized" → chạy lại setup; "flood wait" → Telegram rate limit,
đợi rồi thử lại; quên mật khẩu 2FA → reset trong app Telegram (Settings → Privacy → Two-Step
Verification).

---

## 5. MCP bridges

Đăng ký trong `%APPDATA%\coworker\mcp.json`; `launch.py` seed lại mỗi lần boot nên máy mới không
cần cấu hình tay.

| Server | Setup | Chức năng |
|---|---|---|
| `telegram-bot` | zero | 14 tool Bot API |
| `telegram-mtproto` | mục 4 | 7 tool tài khoản cá nhân |
| `subagents` | zero | spawn session con chạy song song, fan-out/fan-in |
| `skills` | zero | skill pack đọc từ `skills/*.md` |
| `commands` | zero | slash-command template từ `commands/*.md` |
| `artifacts` | zero | publish HTML/MD tại `http://127.0.0.1:8766/artifacts/*` |
| `computer-use` | zero | click/type/screenshot trên desktop (pyautogui) |
| `filesystem` | zero | đọc/ghi file trong workspace |
| `memory` | zero | memory graph bền vững qua các session |
| `sequential-thinking` | zero | scaffolding chain-of-thought |
| `fetch` | zero | GET một URL, trả nội dung |
| `git` | zero | thao tác git trên repo |
| `puppeteer` | zero | browser automation headless |

Một số server khác nằm sẵn trong mcp.json nhưng `enabled: false` vì cần token riêng: `postgres`,
`slack`, `gitlab`, `brave-search`, `google-maps`, `notion`, `obsidian`, `twitter`, `everything`.

Bật một cái:

```bash
curl -X PATCH -H "x-connect-ai-token: $CONNECT_AI_API_TOKEN" \
  -H "Content-Type: application/json" -d '{"enabled": true}' \
  http://127.0.0.1:8765/v1/mcp/notion
```

hoặc sửa `enabled` + điền token thật trong `%APPDATA%\coworker\mcp.json` rồi restart launcher.

> **MCP chỉ nạp trong terminal UI / desktop app.** Runtime :8765 (cái GUI web dùng) không nạp
> MCP — nên trong GUI bạn sẽ không thấy tool `mcp__*`. Đây là thiết kế của upstream, không phải lỗi.

### Thêm skill / command

Drop file vào `skills/` hoặc `commands/`:

```markdown
---
name: my-skill
description: Skill này làm gì
triggers: [keyword1, keyword2]
---

# Nội dung skill — instruction cho agent
```

---

## 6. Models và chuyện hết quota

Picker chỉ hiện model của provider đã có key. Chuỗi hiện tại nếu chỉ có Gemini + Groq:

```
gemini-2.5-flash → 3.1-pro-preview → 2.5-pro → 3.1-flash-lite
→ 3.1-flash-lite-preview → 3.5-flash-lite → groq:llama-3.3-70b
```

**Hết quota không làm chết turn.** Runtime tự chuyển sang model kế trong chuỗi (transcript hiện
"X is out of quota — continuing on Y"). Nếu *mọi* model đều hết, turn được **park** lại: chờ
theo backoff 20/45/90/180/300s rồi tự chạy tiếp khi limit hồi. Bấm **Stop** để huỷ.

Tổng thời gian chờ tối đa đặt bằng `COWORKER_QUOTA_WAIT_SECONDS` (mặc định 900 giây; `0` = tắt,
lỗi ngay như cũ).

Đổi model mặc định: dropdown cạnh ô chat (per-session), hoặc
`POST /v1/settings/default-model {"model": "..."}`.

---

## 7. Automations

Xem [AUTOMATIONS.md](AUTOMATIONS.md). Cách nhanh nhất là nói thẳng với agent:

> "Mỗi ngày 9h sáng, đọc Gmail inbox rồi gửi tóm tắt tiếng Việt vào Telegram chat 6973629128"

Agent tạo scheduled task, hỏi approve, bấm OK là xong.

---

## 8. Troubleshoot

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `Port 1420 already in use` | lần chạy trước chưa tắt hẳn | `netstat -ano \| findstr :1420` → `taskkill /F /PID <pid>`. Lần sau tắt bằng Ctrl+C |
| Sidebar báo "0 MCP servers" | chạy launcher từ shell Claude Desktop (MSIX redirect) | chạy `run-web.bat` từ cmd native |
| GUI không thấy tool `mcp__*` | đúng thiết kế — runtime :8765 không nạp MCP | dùng `run.bat` (terminal UI) nếu cần MCP |
| Google 401 | access token rớt hoặc connector bị ngắt tay | card Google ▸ **Reconnect all** |
| Model báo hết quota | free tier cạn | đã tự failover; thêm key thứ hai (Groq free) để có chỗ nhảy sang |
| Agent trả sai ngôn ngữ | session cũ cache system prompt | "+ New session"; pin ngôn ngữ trong `%APPDATA%\coworker\AGENTS.md` |
| Turn treo > 30s | engine đang chờ tool response | Stop → session mới |
| `UnicodeEncodeError` khi launch | console không phải UTF-8 | đã fix trong `launch.py`; nếu tái diễn, `chcp 65001` |

Log: `logs/connect-ai-server.log`, `logs/connect-ai-gui.log`.

---

## 9. Cấu trúc thư mục

```
AI-cowork/
├── .venv/            Python 3.13 — môi trường DUY NHẤT (runtime + mọi bridge deps)
├── .env              secrets (gitignored)
├── connect-ai/       agent runtime + GUI (vendored OpenWorker, đã patch — xem PATCHES.md)
│   ├── coworker/       backend package
│   └── surfaces/gui/   Workspace GUI (Vite React, :1420)
├── bridge/           MCP stdio server tự viết
├── skills/           skill pack (*.md)
├── commands/         slash-command template (*.md)
├── logs/             connect-ai-server.log + connect-ai-gui.log
├── launch.py         launcher: runtime + GUI + helper 8766
├── google_auth.py    Google OAuth refresher
├── run-web.bat       entry chính (GUI)
└── run.bat           terminal UI
```

File sinh ra lúc chạy (đều gitignored): `google-tokens.json`, `google-oauth.json`,
`bridge/.tg_creds.json`, `bridge/.tg_session.session`, `outputs/`, `artifacts/`.
