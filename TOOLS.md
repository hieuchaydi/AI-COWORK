# Tools — agent làm được những gì

Danh mục tool agent gọi được, kèm prompt mẫu tiếng Việt paste thẳng vào chat.

**Luật duyệt xuyên suốt**: tool *đọc* chạy tự do, tool *ghi/xoá/gửi* hỏi bạn trước. Mỗi connector
có toggle bật/tắt từng tool riêng trong trang chi tiết của nó.

> **Cập nhật 2026-08-08**: runtime :8765 CÓ nạp MCP (log khởi động cho thấy cả 14 server trả lời
> `ListToolsRequest`), nên GUI web cũng thấy `mcp__*`. Hệ quả: mỗi request mang **~200 tool
> definition**. Groq chặn ở **128** (`400 "tools": maximum number of items is 128`);
> `cloudflare:@cf/openai/gpt-oss-120b` nhận 200 tool bình thường (đã test). Runtime không tự cắt
> bớt — muốn nhẹ thì tắt MCP server không dùng ở trang MCP.

---

## Built-in — agent nào cũng có

| Tool | Việc |
|---|---|
| `todo_write` / `todo_read` | panel tiến độ bên phải GUI được render từ đây |
| `read_file` / `write_file` / `list_files` / `grep_files` | file trong các thư mục bạn đã cấp |
| `run_shell` | chạy lệnh terminal (hỏi duyệt) |
| `search_web` | web search (DuckDuckGo mặc định, không cần key) |
| `send_message` | gửi tin qua connector đã kết nối — target `platform:chat_id[:thread_id]` |
| `subscribe` / `unsubscribe` / `list_subscriptions` | cho session nghe một kênh để nhận tin nhắn đến |
| `current_time` | giờ hiện tại theo múi giờ đã pin (UTC+7) — dùng cho mọi phép tính thời gian |
| `request_directory` | agent xin bạn cấp thêm một thư mục |
| `propose_plan` / `ask_user` | agent trình kế hoạch hoặc hỏi lại giữa chừng |

```
Lập kế hoạch 4 bước dựng một landing page HTML rồi bắt tay làm
Đọc ~/Desktop/notes.txt và tóm tắt 3 gạch đầu dòng
Chạy: git log --oneline -20
Gửi Telegram "xong rồi nhé" tới chat 6973629128
```

---

## Custom Tools — tự viết tool mới siêu tốc (30 giây)

Bạn có thể tự bổ sung công cụ mới cho agent bất cứ lúc nào bằng 2 cách:
1. **Thả file vào `custom_tools/`**: Viết hàm Python gắn decorator `@coworker_tool`, runtime sẽ tự quét và nạp khi khởi động. Xem hướng dẫn chi tiết tại [docs/CREATING_TOOLS.md](docs/CREATING_TOOLS.md).
2. **Sinh code tự động qua CLI**:
   ```bash
   python tools/scaffold_tool.py --name my_tool --desc "Mô tả công cụ"
   ```
   Lệnh sẽ tự động sinh code tool chuẩn và bộ test `pytest` đi kèm.

---

## Browser — 14 tool, Chromium thật, giữ đăng nhập

Chromium chạy với profile bền vững: **đăng nhập một lần, các lần sau vẫn còn session**. Khác hẳn
scraper — trang JS-heavy, trang cần login đều đọc được.

| Tool | Việc | Duyệt |
|---|---|---|
| `browser_read_url` | mở URL và trả text, một phát | tự do |
| `browser_open` | mở URL trong tab bền vững | tự do |
| `browser_read` | đọc text trang hiện tại | tự do |
| `browser_read_selector` | đọc theo CSS selector | tự do |
| `browser_get_links` / `browser_get_forms` | liệt kê link / form | tự do |
| `browser_current_url` | URL hiện tại | tự do |
| `browser_screenshot` | chụp màn hình → `outputs/screenshots/` | tự do |
| `browser_wait_for` | chờ selector xuất hiện | tự do |
| `browser_click` | click element | **hỏi** |
| `browser_fill` | điền input | **hỏi** |
| `browser_press` | gõ phím (Enter, Tab…) | **hỏi** |
| `browser_evaluate` | chạy JS trong trang | **hỏi** |
| `browser_close` | đóng session browser | **hỏi** |

```
Mở vnexpress.net, đọc 10 tiêu đề mới nhất mục Khoa học
Vào trang đăng nhập của X, tôi tự nhập mật khẩu, xong bảo tôi
Chụp màn hình trang hiện tại
```

## Crawl / scrape — 8 tool

| Tool | Việc |
|---|---|
| `crawl_urls` | bò nhiều trang từ một URL gốc (`same_domain`, `follow_pattern`, `max_pages`, `delay_ms`) |
| `parse_sitemap` | đọc sitemap.xml lấy danh sách URL |
| `extract_html` | bóc nội dung theo selector |
| `extract_table` | bóc `<table>` thành dữ liệu có cấu trúc |
| `save_page_snapshot` | lưu snapshot một trang |
| `save_csv` | ghi rows ra CSV |
| `save_artifact` | ghi file vào `artifacts/` (publish được qua helper 8766) |
| `download_file` | tải file về `outputs/` |

```
Crawl 30 bài mục Khoa học của vnexpress.net, tóm tắt từng bài rồi xuất CSV
Lấy bảng tỷ giá trên trang X và lưu thành CSV
```

---

## Connectors — 40 cái, credential nằm trên máy này

**Google (Gmail · Calendar · Drive)** — một lần đăng nhập phủ cả ba. Xem mục 2 của
[SETUP.md](SETUP.md).

```
Có email nào chưa đọc trong 24h qua? Tóm tắt giúp tôi
Tuần này tôi rảnh khung nào? Đặt họp 30 phút với A vào chỗ trống sớm nhất
Tìm trong Drive file nào có chữ "báo cáo quý 3"
```

**Đã có sẵn để kết nối**: Slack, GitHub, Notion, Telegram, Outlook, Jira, Linear, HubSpot,
Confluence, Zendesk, GitLab, Discord, Stripe, Asana, Dropbox, Box, WhatsApp, QuickBooks,
Salesforce, DocuSign, ClickUp, Canva, Figma, Descript, Clay, Close, Attio, PostHog, Mixpanel,
Amplitude, Apollo, Hunter, PagerDuty, Datadog, Monday, Email (IMAP/SMTP).

Kết nối bằng token dán tay hoặc OAuth chạy local; không có broker trung gian. Vài connector
(monday, asana, jira) có one-click OAuth qua MCP server của chính hãng — vẫn chạy local.

**Gmail còn có bộ lọc riêng tư**: trang Gmail có mục "Never show agents" — email khớp sender/label
bị lọc *trước khi* agent nhìn thấy. Agent không biết là có thứ bị giấu; bạn thấy số lượng bị ẩn
trên tool card.

---

## Ingest — lấy dữ liệu từ site chặn automation

Site nào fingerprint được Playwright/CDP (Shopee, TikTok Shop…) thì đừng đấu với nó. Luồng thay
thế **không có automation nào để bắt**: Chrome thường của bạn chạy JS của chính trang đó, dùng
cookie thật, rồi đẩy kết quả về agent.

**Cách 1 — extension, agent tự làm** (cài một lần):

1. `chrome://extensions` → bật Developer mode → **Load unpacked** → chọn [browser-extension/](browser-extension/)
2. Xong. Bảo agent *"cào đánh giá &lt;link&gt;"* — nó `web_fetch` tới `/ingest/job?url=…`,
   extension trong Chrome của bạn chạy job bằng cookie thật, POST kết quả về `/ingest`,
   agent poll `/ingest/result?id=…` rồi báo đường dẫn CSV. Không phải bấm gì.

**Cách 2 — bookmarklet** (không cài gì, đổi lại phải bấm tay mỗi sản phẩm):

1. Mở <http://127.0.0.1:8766/ingest>, **kéo** nút bookmarklet lên thanh bookmark (một lần duy nhất).
2. Mở trang cần lấy trong Chrome thường (đã đăng nhập) → bấm bookmark → chờ ô đen góc phải.
3. **Xong** — helper tự ghi `outputs/csv/<tên>.csv` (có BOM, Excel đọc tiếng Việt đúng) và
   `outputs/inbox/<tên>.json` bản thô. Cả hai hiện luôn ở panel Artifacts, agent không phải
   làm gì. Muốn phân tích thêm thì bảo agent `web_fetch` cái link JSON đó.

CSV do helper ghi chứ không giao cho agent là có lý do: `outputs/` nằm ngoài workspace của
session nên `read_file` không với tới, còn `web_fetch` thì cắt ở 100k ký tự — vài trăm đánh giá
là mất một nửa. Một cú bấm ra thẳng file cần dùng.

CSP của site chặn `connect-src` tới localhost → bookmarklet tự đổi sang tải file JSON về máy,
bạn kéo thẳng file vào chat.

Endpoint dùng chung cho mọi nguồn, không riêng bookmarklet:

```
POST http://127.0.0.1:8766/ingest?name=ten_file
{"rows": [ ... ], "source": "tuỳ chọn"}
```

Endpoint của job queue:

| | |
|---|---|
| `GET /ingest/job?url=…` | agent xếp job (GET vì `web_fetch` không POST được) |
| `GET /ingest/jobs?wait=25` | extension long-poll, trả ngay khi có job |
| `POST /ingest?name=…` | extension nộp `{job, rows}` hoặc `{job, error}` |
| `GET /ingest/result?id=…` | agent hỏi xong chưa |

> **Đã đo, đừng thử lại**: Shopee chặn browser điều khiển bằng CDP ngay request đầu tiên — cả
> Chromium bundled lẫn Chrome thật (`channel="chrome"`), cả khi vào trang chủ trước để lấy cookie,
> cả khi chưa cần đăng nhập. Đều rơi vào `/verify/traffic/error`. Attach vào Chrome thật qua
> `--remote-debugging-port` cũng không cứu được: từ **Chrome 136** cờ đó bị bỏ qua trên profile
> mặc định ([blog Chrome](https://developer.chrome.com/blog/remote-debugging-port)), nên vẫn là
> một profile trắng đúng bằng cái Playwright tự tạo.

---

## Dynamic Tools — agent tự viết và gọi tool mới

Không cần deploy hay restart. Agent sinh code Python, đăng ký qua helper, gọi ngay trong cùng turn.

**Luồng hoạt động:**

```
1. Agent viết hàm Python run(args) → dict
2. web_fetch /tools/register?name=…&desc=…&code=<url-encoded>
3. web_fetch /tools/call?name=…&args={"key":"value"}
4. Helper chạy code trong subprocess riêng (timeout 30s), trả JSON
```

**Endpoints (tất cả GET — web_fetch được):**

| Endpoint | Việc |
|---|---|
| `GET /tools/list` | Liệt kê tools đã đăng ký (name, desc, số dòng, thời gian) |
| `GET /tools/register?name=N&desc=D&code=C` | Đăng ký tool mới (code phải URL-encode) |
| `GET /tools/call?name=N&args={...}&timeout=30` | Gọi tool, args là JSON URL-encoded |
| `GET /tools/delete?name=N` | Xoá tool khỏi registry + disk |

**Quy tắc viết code:**
- Code **phải** định nghĩa hàm `run(args: dict) -> dict`
- Có thể `import` bất kỳ thư viện nào đã cài trong venv
- Trả về `dict` — helper serialize thành JSON cho agent đọc
- Timeout mặc định 30s, tối đa 120s (truyền `&timeout=60`)
- Tool **persist** sang `outputs/dynamic_tools/` — sống qua restart

**Ví dụ — tool đọc tỷ giá USD/VND:**

```python
import urllib.request, json

def run(args: dict) -> dict:
    url = "https://open.er-api.com/v6/latest/USD"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    vnd = data["rates"].get("VND", "N/A")
    return {"usd_to_vnd": vnd, "updated": data.get("time_last_update_utc")}
```

```
# Đăng ký (code phải urllib.parse.quote)
web_fetch("http://127.0.0.1:8766/tools/register?name=usd_vnd&desc=Tỷ+giá+USD/VND&code=<encoded>")

# Gọi ngay
web_fetch("http://127.0.0.1:8766/tools/call?name=usd_vnd&args={}")
```

> **Lưu ý bảo mật**: Code chạy trong subprocess Python riêng biệt với timeout —
> cùng quyền với process launcher, không bị sandbox OS. Dùng cho internal tools,
> không expose ra internet.

---

## MCP bridges

### `telegram-bot` — 91 tool

Bridge lớn nhất, **một mình chiếm 91/119 tool của toàn bộ bridge nội bộ** — đủ để đẩy request
vượt trần 128 của Groq. Tắt nó là cách nhanh nhất để gọn lại nếu bạn không dùng Telegram.

Hay dùng nhất: `get_me`, `get_chat`, `get_chat_member`, `get_chat_administrators`,
`get_chat_member_count`, `get_updates`, `get_webhook_info`, `send_message`, `send_photo`,
`send_document`, `edit_message_text`, `delete_message`, `forward_message`, `set_my_commands`.
Phần còn lại wrap gần trọn Bot API, cộng `raw_api()` cho method chưa wrap.

Giới hạn Bot API: chỉ đọc được tin DM cho bot; không gửi được cho user chưa từng DM bot.

```
Dùng telegram-bot get_me
Gửi ảnh outputs/screenshots/latest.png vào chat 6973629128
```

### `telegram-mtproto` — 7 tool (tài khoản cá nhân)

`get_me`, `list_dialogs`, `get_history`, `search_messages`, `send_message_as_me`,
`download_media`, `get_chat_info`. Cần setup ở mục 4 [SETUP.md](SETUP.md).

```
Liệt kê 10 chat gần nhất
Đọc 50 tin cuối trong group "Team Dev" rồi tóm tắt ai đang vướng gì
```

### `subagents` — chạy song song

Spawn nhiều session con làm việc đồng thời rồi gom kết quả. Hợp với fan-out: đọc 30 trang, mỗi
subagent một nhóm.

### `skills` / `commands` — tái sử dụng prompt

Skill pack đọc từ `skills/*.md`, slash-command từ `commands/*.md`. Thêm cái mới = drop file, không
cần restart.

### `artifacts` — publish HTML/MD

Ghi file rồi mở tại `http://127.0.0.1:8766/artifacts/<tên>`. Tiện cho báo cáo/dashboard xem bằng
trình duyệt.

### `computer-use` — điều khiển desktop

Click, gõ phím, chụp màn hình toàn máy (pyautogui). Mạnh và nguy hiểm — mọi thao tác đều hỏi duyệt.

### Bên thứ ba

`filesystem`, `memory` (memory graph bền vững), `sequential-thinking`, `fetch`, `git`, `puppeteer`.

Có sẵn nhưng tắt (cần token riêng): `postgres`, `slack`, `gitlab`, `brave-search`, `google-maps`,
`notion`, `obsidian`, `twitter`.

---

## Tự động hoá

Mọi tool trên đều gọi được từ scheduled task — xem [AUTOMATIONS.md](AUTOMATIONS.md). Tool nằm
ngoài phạm vi đã uỷ quyền sẽ park vào Inbox chờ duyệt thay vì tự chạy.

## Xem agent đã làm gì

Tab **Activity** ghi mọi tool call: tham số, kết quả, luật nào cho phép nó chạy, và số item bị
bộ lọc riêng tư giấu đi.
