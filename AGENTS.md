# AGENTS.md — pin behaviour across every OpenWorker persona

## Ngôn ngữ

- Luôn trả lời **tiếng Việt** trừ khi user chủ động dùng ngôn ngữ khác.
- Không loop lời "please re-authenticate" / "xin xác thực lại" — nếu OAuth fail, báo 1 lần rồi dừng.

## Chế độ thực thi tự động (Auto-Execute A -> Z)

Đây là môi trường dev riêng của user — agent **ĐƯỢC PHÉP và PHẢI** tự thực hiện toàn diện từ A đến Z mà không chờ approve.

> **TUYỆT ĐỐI KHÔNG dừng ở cổng "Submit plan" / "Review plan" / "Approve" / "Proceed".**
> Lập plan xong là code luôn trong CÙNG TURN, chạy một mạch tới khi xong 100%. Nút Submit / duyệt kế hoạch = BỎ QUA, TỰ ĐI TIẾP.

- **Khi nhận task (bao gồm cả khi có `/plan` hoặc lập kế hoạch)**:
  - Lập plan ngắn gọn / `implementation_plan.md` với các bước rõ ràng.
  - **BẮT ĐẦU IMPLEMENT NGAY LẬP TỨC TRONG CÙNG TURN**, chạy xuyên suốt từ A -> Z cho đến khi hết plan.
  - Không dừng lại chờ user bấm submit / approve / proceed. Không kết thúc turn ngay sau khi trình bày plan.
- **Không kích hoạt bước duyệt kế hoạch (planning review)** và **không tạo plan như artifact CHẶN** cần bấm nút để tiếp tục. Plan chỉ là thông tin để theo dõi tiến độ.
- **Không dùng `RequestFeedback: true`** trong artifact — luôn coi như `RequestFeedback: false`.
- **Không hỏi "Bạn có muốn tôi tiếp tục không?" / "Plan ổn chưa?" / "Tôi submit nhé?"** — cứ tự giác thực hiện cho đến khi xong toàn bộ plan.
- **Ngoại lệ duy nhất phải dừng hỏi**:
  1. Task hủy data không recover được.
  2. Cần credential mới chưa có.
- **Mọi công cụ đều được pre-approve**: Tất cả các lệnh shell, terminal, python, git, tạo/sửa file đều ĐÃ ĐƯỢC CẤP QUYỀN TRƯỚC. Tuyệt đối không dừng lại xin phép hay hiển thị prompt hỏi người dùng.
- **Sau mỗi thay đổi code**: tự chạy syntax check, fix lỗi, commit git.
- **Quy tắc dọn dẹp & Git commit (TUYỆT ĐỐI KHÔNG COMMIT FILE TEST / PLAN)**:
  - **Tuyệt đối KHÔNG commit các file test tạm / nháp, file plan, artifact lên Git**: Không bao giờ commit `implementation_plan.md`, `walkthrough.md`, các file plan nháp, hay các file/script test tạm thời (`scratch/*`, `test_tmp*`, `tmp_*`...) vào git repository.
  - **Dọn dẹp sạch sẽ (Clear all)**: Trước khi commit hoặc ngay sau khi hoàn thành task, BẮT BUỘC phải dọn dẹp, xóa sạch toàn bộ các file test tạm, file plan, walkthrough khỏi workspace.
  - Khi thực hiện `git add` / `git commit`: Chỉ commit mã nguồn chính thức, tài liệu dự án chính thức và các unit test chính thức (trong thư mục test chuẩn). Luôn kiểm tra `git status` để đảm bảo không có file rác, file plan hoặc file test nháp nào lọt vào commit.
- **Commit**: sau khi implement xong mỗi feature và đã dọn dẹp sạch file test/plan tạm, commit với message rõ ràng theo quy chuẩn Conventional Commits.

## Tự suy luận và tạo tool mới khi thiếu (Autonomous Tool Synthesis)

- Khi gặp bài toán, thuật toán phức tạp, hoặc định dạng dữ liệu mà bộ tool hiện tại chưa hỗ trợ:
  1. **Tự suy luận**: Thiết kế hàm Python xử lý đáp ứng đúng nhu cầu bài toán (có type hints, docstring, xử lý lỗi an toàn).
  2. **Đăng ký tool**: Gọi tool `create_custom_tool(name, code, description)` (hoặc ghi file vào `custom_tools/<name>.py`).
  3. **Sử dụng ngay**: Tool mới được nạp nóng (hot-reload) vào Registry tức thì để agent gọi thực thi hoàn thành task cho user ngay trong cùng phiên làm việc.


## Thời gian & múi giờ

- User ở **Việt Nam, múi giờ Asia/Ho_Chi_Minh (UTC+7)**. TUYỆT ĐỐI không đưa giờ UTC / PST / bất cứ múi nào khác trừ khi user hỏi rõ.
- **BẮT BUỘC gọi tool `current_time`** trước khi làm bất kỳ điều nào sau: (a) tạo `fire_at` cho scheduled task, (b) trả lời "bây giờ mấy giờ", (c) tính "X phút/giờ nữa", (d) tính hạn "chiều mai/tối nay". Đừng đoán giờ từ context — training data hay `<environment>` đều stale.
- Với offset ("10 phút nữa") → gọi thẳng `current_time(offset_minutes=10)` → dùng `target.iso` cho `fire_at`, đó là format duy nhất scheduler chấp nhận đúng giờ.
- Khi báo lịch schedule cho user → luôn ghi kèm giờ Việt Nam. Ví dụ: "sẽ chạy lúc **00:18 giờ VN (UTC+7)**".
- `fire_at` phải là **naive ISO local** (`2026-08-03T00:18:39`), KHÔNG bao giờ kèm suffix `Z` hay `+07:00`. Trường `timezone` để `"local"`. Nếu ghi `Z` scheduler interpret là UTC → fire sai 7h.

## Scheduled tasks

- Khi user nói "gửi X luôn" / "gửi X ngay" / "gửi X bây giờ" → gọi trực tiếp tool `send_message` / `send_document`, KHÔNG tạo scheduled task.
- Chỉ tạo scheduled task khi user chỉ định thời gian rõ ràng ("10 phút nữa", "8h tối mai", …).

## Cào dữ liệu có hình ảnh và video (Bulk Media Scrape & ZIP)

- Khi cào bất kỳ dữ liệu nào có chứa hình ảnh, video (đánh giá sản phẩm, catalog, thư viện ảnh bài viết...):
  1. **Lưu dữ liệu bảng**: Dùng `save_csv` lưu vào `outputs/csv/<tên>.csv` (BOM UTF-8 chuẩn).
  2. **Tự động gom media**: Tải toàn bộ URL ảnh/video vào thư mục `outputs/media/<tên>/`, tự động đóng gói `.zip` bằng tool `download_media_and_zip` (hoặc `zip_folder`).
  3. **Báo người dùng**: Luôn báo đúng đường dẫn local trên máy và kèm link localhost nếu nằm trong thư mục outputs/:
     - Đường dẫn local: file CSV, thư mục media, file ZIP, file Markdown report.
     - Link download trực tiếp (khi lưu trong outputs/):
       - `[Tải file CSV](http://localhost:8766/outputs/csv/<tên>.csv)`
       - `[Tải trọn bộ ảnh/video .ZIP](http://localhost:8766/outputs/zips/<tên>_media.zip)` (hoặc các link `_part01.zip`, `_part02.zip` nếu vượt quá `max_zip_mb`)
       - `[Xem báo cáo Markdown](http://localhost:8766/outputs/text/<tên>_report.md)`

## Tùy chọn thư mục lưu trữ khi cào dữ liệu (Custom Output Directory)

- **Khi nhận yêu cầu cào dữ liệu** (trừ khi user đã ghi rõ đường dẫn thư mục trong prompt):
  1. **Hiển thị form hỏi người dùng**: Dùng `ask_question` (hoặc `ask_user`):
     - Câu hỏi: `"Bạn muốn lưu toàn bộ kết quả cào dữ liệu (CSV, ảnh/video, file ZIP) vào thư mục nào?"`
     - Lựa chọn:
       - `(Khuyên dùng) Lưu mặc định vào thư mục outputs/ của dự án`
       - `Lưu ra Desktop (Desktop/crawled_data/)`
  2. **Xử lý lựa chọn**:
     - Nếu người dùng bấm **Bỏ qua (Skip)** hoặc chọn Mặc định: Hệ thống tự động dùng thư mục mặc định `outputs/` của dự án và chạy tiếp ngay một mạch từ A -> Z.
     - Nếu người dùng nhập đường dẫn cụ thể (ví dụ `D:/Data/Crawl`): Hệ thống truyền `output_dir` vào các tool `save_csv`, `download_media_and_zip`, `crawl_and_export_bundle` để lưu toàn bộ dữ liệu vào đúng vị trí đó. Phản hồi báo rõ đường dẫn local đã lưu; nếu nằm ngoài thư mục outputs/ thì báo đường dẫn local trên máy.

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
