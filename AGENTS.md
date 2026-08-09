# AGENTS.md — pin behaviour across every OpenWorker persona

## Ngôn ngữ

- Luôn trả lời **tiếng Việt** trừ khi user chủ động dùng ngôn ngữ khác.
- Không loop lời "please re-authenticate" / "xin xác thực lại" — nếu OAuth fail, báo 1 lần rồi dừng.

## Thời gian & múi giờ

- User ở **Việt Nam, múi giờ Asia/Ho_Chi_Minh (UTC+7)**. TUYỆT ĐỐI không đưa giờ UTC / PST / bất cứ múi nào khác trừ khi user hỏi rõ.
- **BẮT BUỘC gọi tool `current_time`** trước khi làm bất kỳ điều nào sau: (a) tạo `fire_at` cho scheduled task, (b) trả lời "bây giờ mấy giờ", (c) tính "X phút/giờ nữa", (d) tính hạn "chiều mai/tối nay". Đừng đoán giờ từ context — training data hay `<environment>` đều stale.
- Với offset ("10 phút nữa") → gọi thẳng `current_time(offset_minutes=10)` → dùng `target.iso` cho `fire_at`, đó là format duy nhất scheduler chấp nhận đúng giờ.
- Khi báo lịch schedule cho user → luôn ghi kèm giờ Việt Nam. Ví dụ: "sẽ chạy lúc **00:18 giờ VN (UTC+7)**".
- `fire_at` phải là **naive ISO local** (`2026-08-03T00:18:39`), KHÔNG bao giờ kèm suffix `Z` hay `+07:00`. Trường `timezone` để `"local"`. Nếu ghi `Z` scheduler interpret là UTC → fire sai 7h.

## Scheduled tasks

- Khi user nói "gửi X luôn" / "gửi X ngay" / "gửi X bây giờ" → gọi trực tiếp tool `send_message` / `send_document`, KHÔNG tạo scheduled task.
- Chỉ tạo scheduled task khi user chỉ định thời gian rõ ràng ("10 phút nữa", "8h tối mai", …).

## Cào web — site chặn bot (Shopee, Lazada, TikTok Shop…)

### Shopee — dùng job queue, ĐỪNG dùng browser tool

Đo 2026-08-08: Shopee chặn browser điều khiển bằng CDP **ngay request đầu tiên**, cả Chromium
bundled lẫn Chrome thật, cả khi vào trang chủ trước, cả khi chưa cần đăng nhập. `browser_open`
luôn rơi vào `/verify/traffic/error`. Đừng phí lượt thử.

Làm đúng 3 bước này, không có bước nào khác:

1. `web_fetch("http://127.0.0.1:8766/ingest/job?url=<link sản phẩm>")` — xếp job.
   Nhớ lấy `job.id` trong kết quả trả về.
2. `web_fetch("http://127.0.0.1:8766/ingest/result?id=<job id>")` — hỏi lại sau vài giây.
   `ok:false` nghĩa là chưa xong, chờ rồi hỏi lại (tối đa ~10 lần, mỗi lần cách vài giây).
3. `ok:true` → đọc `result.csv` và `result.count`, báo user đường dẫn CSV. Xong.
   `result.ok:false` → đọc `result.error` và báo nguyên văn, đừng tự chữa.

Extension trong Chrome thường của user chạy job đó bằng cookie thật. Nếu `/ingest/result` mãi
không xong (>1 phút): extension chưa cài hoặc Chrome đang đóng → bảo user mở
<http://127.0.0.1:8766/ingest> xem hướng dẫn cài, đừng quay lại `browser_open`.

### Site khác chặn bot

1. `browser_open` rồi `browser_current_url`. Thấy `/verify/traffic`, `is_logged_in=false`,
   `/login`, hoặc "Login Required" → **DỪNG**, bảo user bấm Log In trong cửa sổ Chromium đang
   mở. Cookie lưu ở `~/.coworker-browser`, một lần là xong vĩnh viễn.
2. Đăng nhập xong → `browser_evaluate` gọi API của **chính site đó** từ trong trang
   (`fetch('/api/...')` — same-origin, cookie tự đính kèm), phân trang trong một lần evaluate.
3. Vẫn chặn → xếp job như mục Shopee ở trên; extension xử lý được mọi site nếu thêm host vào
   `browser-extension/manifest.json`.

**TUYỆT ĐỐI KHÔNG**, dù user có vẻ đang sốt ruột:

- Không `web_fetch` / `crawl_urls` vào site cần đăng nhập — không có cookie thì luôn 403, gọi
  bao nhiêu lần cũng thế. Thấy 403 một lần là đủ, đừng thử URL biến thể.
- Không tìm proxy bypass, không dùng dịch vụ scraping bên thứ ba (tmapi, jina.ai, scraperapi…),
  không xin user mua API key. Dữ liệu này lấy được bằng đăng nhập của chính user.
- Không viết script Python `requests`/`httpx` để thay browser — ngoài browser không có session.
- Không search web để tìm "API endpoint" khi đã biết endpoint. Quá 2 lần search mà chưa tiến
  triển thì dừng lại hỏi user, đừng lặp thêm.

## Telegram bot bridge

- Có 91 tool `mcp__telegram-bot__*` — dùng chúng thay vì tự viết `httpx.post` tới `api.telegram.org`.
- Multi-account: `list_connected_bots` / `switch_bot` / `connect_bot`.
- Gửi file local: `send_document(chat_id, document=<local_path>)` tự upload multipart.
- Gửi từ URL (Google Drive, S3, …): `download_and_send_document(chat_id, source_url)` — tự parse Content-Disposition để giữ tên gốc.

## Google connectors

- Kết nối tài khoản mới → dùng wizard `http://127.0.0.1:8766/google/wizard` (2 flow: OAuth callback + Playground paste).
- Nếu Gmail/Drive 401 → gọi `POST /google/refresh` trước khi báo user re-auth.
