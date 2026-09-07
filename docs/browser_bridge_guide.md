# Hướng Dẫn Kỹ Thuật Browser Bridge Realtime (WebSocket & MV3 Extension)

Tài liệu thiết kế, giao thức và hướng dẫn tích hợp cho hệ thống Browser Bridge hai chiều giữa Codex/Local Gateway và Google Chrome thật (Manifest V3 Extension).

---

## 1. Bối cảnh & Mục tiêu Kiến trúc

Hệ thống điều khiển trình duyệt của `AI-COWORK` trước đây dựa vào:
1. **Playwright/CDP automation**: Thường xuyên bị các trang thương mại điện tử lớn (Shopee, Lazada, TikTok Shop) phát hiện và chặn bot ngay từ request đầu tiên (`/verify/traffic/error`).
2. **HTTP Long-Polling Ingest Queue (`:8766/ingest`)**: Cho phép Chrome thật của người dùng lấy job và cào dữ liệu mà không bị chặn fingerprinting. Tuy nhiên cơ chế này có độ trễ lớn (~1s-25s), tải tài nguyên do polling liên tục, và không hỗ trợ tương tác DOM theo thời gian thực.

**Browser Bridge Realtime** giải quyết triệt để vấn đề này bằng kiến trúc kết hợp:
- **WebSocket Gateway Loopback (`127.0.0.1:8766/browser/v1/ws` hoặc `:8767/browser-extension`)**: Kết nối hai chiều với độ trễ dưới 5ms.
- **Manifest V3 Extension (Chrome Profile Thật)**: Sử dụng phiên đăng nhập, cookie và phần cứng thật của người dùng.
- **Typed Action Registry**: 18 hành động điều khiển có kiểu dữ liệu rõ ràng, thực thi an toàn trong ngữ cảnh tab mà **tuyệt đối không sử dụng `eval` hoặc `new Function`**.
- **Human-in-the-Loop Verification**: Tự động nhận diện thử thách/CAPTCHA, tạm dừng công việc một cách an toàn và cung cấp nút bấm "Resume" cho người dùng tiếp tục mà không làm đứt gãy luồng cào dữ liệu.

```
Codex / MCP Client / Local Engine
    ↕ Python Typed Transport hoặc HTTP API của launcher
Browser Gateway (:8766 / :8767) (127.0.0.1 only)
    ↕ RFC 6455 WebSocket (Envelope v1 Protocol)
Chrome Extension MV3 (Chrome Profile thật)
    ↕ chrome.scripting.executeScript (Precompiled pure JS functions)
Web Pages (Shopee, Lazada, nội bộ...) [Đầy đủ cookie & session người dùng]
```

---

## 2. Mô hình Bảo mật (Security & Trust Boundaries)

Hệ thống tuân thủ các nguyên tắc an ninh nghiêm ngặt nhất nhằm bảo vệ thông tin đăng nhập và dữ liệu người dùng:

1. **Loopback-Only Binding**:
   - WebSocket Server chỉ chấp nhận bind vào các địa chỉ loopback cục bộ: `127.0.0.1`, `localhost`, `::1`.
   - Tuyệt đối từ chối bind trên `0.0.0.0` hoặc interface mạng công cộng.
2. **Origin & Extension ID Validation**:
   - Bắt buộc kiểm tra header `Origin` khi nâng cấp WebSocket và gọi `/browser/pair`.
   - Chỉ cho phép các request đến từ nguồn `chrome-extension://<extension-id>`.
   - Mọi truy vấn từ website thông thường (`http://`, `https://`, file) đều bị từ chối với mã lỗi `403 Forbidden` nhằm ngăn chặn tấn công CSRF / Confused Deputy.
3. **Cryptographic Pairing Token**:
   - Token ngẫu nhiên 256-bit được tạo tự động (`secrets.token_urlsafe(32)`).
   - Xác thực token bằng phép so sánh thời gian bất biến (`hmac.compare_digest`) để triệt tiêu nguy cơ tấn công kênh kề (timing attack).
4. **Strict Same-Origin Isolation trong Tab Context**:
   - Hành động `fetch.sameOrigin` chỉ cho phép gọi API có cùng Scheme, Host, và Port với tab đang duyệt.
   - Request thực hiện bên trong tab context với `credentials: "include"`, tận dụng cookie sẵn có của người dùng mà **không bao giờ để lộ raw cookie về server hoặc audit logs**.
5. **No Eval / CSP Compliance**:
   - Toàn bộ 18 action handlers trong extension được định nghĩa dưới dạng hàm JS thuần tĩnh (`chrome.scripting.executeScript({ func: handler })`), hoàn toàn không dùng chuỗi code động hoặc `eval()`.
6. **Audit Log Secret Scrubbing**:
   - Module `AuditLogger` và hàm `redact_sensitive_data` tự động quét đệ quy các trường nhạy cảm (`token`, `auth`, `cookie`, `password`, `key`, `bearer`) và thay thế bằng `[REDACTED]` trước khi ghi ra console hoặc file log.

---

## 3. Quy chuẩn Giao thức Envelope v1

Tất cả các tin nhắn gửi qua WebSocket đều được đóng gói theo định dạng `MessageEnvelope v1`:

```json
{
  "v": 1,
  "type": "command | accepted | progress | result | error | cancel | ping | pong | verification.required | verification.resolved | hello | authenticate",
  "id": "uuid-v4-correlation-id",
  "sessionId": "optional-tab-session-id",
  "deadlineMs": 30000,
  "action": "page.navigate",
  "params": {
    "url": "https://shopee.vn"
  },
  "result": null,
  "error": null,
  "progress": null
}
```

### Các Mã Lỗi Chuẩn (`ErrorCode`)
- `AUTHENTICATION_FAILED`: Token không hợp lệ hoặc thiếu xác thực.
- `ORIGIN_FORBIDDEN`: Origin không thuộc extension được cấp quyền.
- `INVALID_MESSAGE`: Sai cú pháp JSON hoặc params không khớp schema.
- `ACTION_NOT_SUPPORTED`: Hành động không nằm trong danh mục hỗ trợ.
- `TAB_NOT_FOUND`: Tab được chỉ định không tồn tại hoặc đã bị đóng.
- `PERMISSION_DENIED`: Thiếu quyền thao tác.
- `TIMEOUT`: Hết thời gian chờ deadline thực thi.
- `CANCELLED`: Lệnh đã bị hủy bởi người dùng hoặc hệ thống.
- `PAYLOAD_TOO_LARGE`: Tin nhắn vượt quá giới hạn 8 MiB (`MAX_MESSAGE_BYTES`).
- `SAME_ORIGIN_VIOLATION`: Yêu cầu fetch vi phạm nguyên tắc same-origin với tab.
- `VERIFICATION_REQUIRED`: Phát hiện trang kiểm tra bảo mật / CAPTCHA.
- `INTERNAL_ERROR`: Lỗi nội bộ trong quá trình thực thi.

---

## 4. Danh Mục Action Registry (18 Actions)

| Nhóm | Tên Action | Tham Số Chính | Mô Tả |
| :--- | :--- | :--- | :--- |
| **Hệ thống** | `browser.health` | *Không* | Kiểm tra sức khỏe kết nối và extension |
| | `job.cancel` | `jobId` | Hủy một tác vụ hoặc lệnh đang chạy |
| **Quản lý Tab** | `tab.list` | *Không* | Lấy danh sách toàn bộ tab đang mở |
| | `tab.getActive` | *Không* | Lấy tab hiện đang kích hoạt (active) |
| | `tab.open` | `url`, `active` | Mở một tab mới với URL chỉ định |
| | `tab.focus` | `tabId` | Chuyển focus đến tab chỉ định |
| **Điều hướng** | `page.navigate` | `url`, `waitUntil`, `tabId` | Chuyển trang (hỗ trợ `load`, `domcontentloaded`, `networkidle`) |
| | `page.getUrl` | `tabId` | Lấy URL hiện tại của tab |
| | `page.getTitle` | `tabId` | Lấy tiêu đề trang web |
| | `page.waitFor` | `selector`, `timeoutMs`, `state` | Chờ đợi phần tử xuất hiện/biến mất trên DOM |
| **Tương tác DOM** | `dom.query` | `selector`, `tabId` | Kiểm tra sự tồn tại và tọa độ phần tử |
| | `dom.queryAll` | `selector`, `limit`, `tabId` | Tìm kiếm danh sách các phần tử khớp |
| | `dom.getText` | `selector`, `tabId` | Trích xuất text content hoặc innerText |
| | `dom.getAttribute`| `selector`, `attribute`, `tabId` | Đọc thuộc tính HTML (href, src, data-*) |
| | `dom.click` | `selector`, `tabId` | Giả lập click chuột thật |
| | `input.type` | `selector`, `text`, `clearFirst` | Điền văn bản vào ô nhập liệu |
| | `input.select` | `selector`, `value`, `tabId` | Lựa chọn option trong dropdown select |
| **Dữ liệu & Media**| `page.snapshot`| `tabId`, `quality` | Chụp ảnh màn hình hiển thị của tab |
| | `fetch.sameOrigin`| `pathOrUrl`, `method`, `headers`, `body` | Thực hiện HTTP request ngay trong ngữ cảnh tab |

---

## 5. Xử Lý Thử Thách & CAPTCHA (Human-in-the-loop Verification)

Khi trình duyệt điều hướng đến các trang thử thách bảo mật của sàn (ví dụ `shopee.vn/verify/traffic`, trang đăng nhập `/login` hoặc xuất hiện slider CAPTCHA):

1. **Phát hiện tự động**: Extension nhận diện URL challenge hoặc DOM verification, không cố gắng bẻ khóa hoặc phá CAPTCHA tự động.
2. **Kích hoạt trạng thái tạm dừng**:
   Extension gửi về Gateway:
   ```json
   {
     "v": 1,
     "type": "verification.required",
     "params": {
       "reason": "Phát hiện trang kiểm tra bảo mật Shopee",
       "url": "https://shopee.vn/verify/traffic"
     }
   }
   ```
3. **Thông báo và Chờ Người Dùng**:
   - `WebSocketTransport` chuyển sang trạng thái `awaiting_user_verification`.
   - Giao diện Popup của Extension hiện cảnh báo màu vàng nổi bật kèm nút **"Tiếp tục chạy (Resume)"**.
   - Người dùng tự giải CAPTCHA hoặc đăng nhập ngay trên cửa sổ Chrome thật của mình.
4. **Tiếp tục Luồng Công Việc (Resume)**:
   - Người dùng bấm **Resume** trên Popup Extension, HOẶC Agent/Client gọi `GET http://127.0.0.1:8766/browser/resume?jobId=...`.
   - Gateway phát thông điệp `verification.resolved` xuống Extension để tiếp tục công việc ngay tại điểm dừng mà không cần chạy lại từ đầu.

---

## 6. Hướng Dẫn Cài Đặt & Khởi Động

### Khởi động Hệ thống
Chạy launcher chính của dự án:
```powershell
python launch.py
```
Lúc này Gateway sẽ tự động lắng nghe:
- HTTP API & WebSocket Upgrade: `http://127.0.0.1:8766`
- Raw WebSocket Endpoint: `ws://127.0.0.1:8767/browser-extension`
- Chrome mở profile riêng tại `chrome-profile/`. Lần đầu cần bật Developer mode và Load unpacked thư mục `browser-extension/` trong `chrome://extensions/`. Chrome chính thức từ bản 137 không hỗ trợ tự nạp bằng `--load-extension`; launcher không coi việc mở Chrome là đã kết nối extension.

### Cài đặt Extension thủ công trên Chrome thường (tùy chọn)
1. Mở Chrome, truy cập: `chrome://extensions/`
2. Bật công tắc **Developer mode** (Góc trên cùng bên phải).
3. Bấm **Load unpacked** và chọn thư mục: `C:\Users\ADMIN\Desktop\AI-COWORK\browser-extension`
4. Bấm vào icon extension trên thanh công cụ:
   - Nếu Gateway đang chạy, bấm **"Tự động lấy Token từ Gateway"**.
   - Bấm **"Kết nối lại"** để bắt đầu liên lạc qua WebSocket.

---

## 7. Extension dùng WebSocket cho toàn bộ luồng công việc

Sau khi ghép nối, extension chỉ dùng socket cho lệnh, job, tiến độ, kết quả,
báo lỗi, heartbeat và xác minh. Không còn HTTP polling `/ingest/jobs`, POST
`/ingest/progress`, POST `/ingest`, hay HTTP `/ping` trong extension.
HTTP `/browser/pair` chỉ bootstrap token trước WebSocket handshake; HTTP fetch
đến website để lấy dữ liệu vẫn là giao thức của chính website đó.

- Gateway → extension: `command`, `ingest.job`, `cancel`, `verification.resolved`.
- Extension → gateway: `accepted`, `result`, `error`, `ping`, sự kiện xác minh.
- Upload ingest: `ingest.rpc` chứa `params.operation` là `progress`, `chunk`
  hoặc `complete`; gateway trả `ingest.reply` với cùng `id`, `ok`, `result/error`.
- Kết quả lớn được chia thành chunk theo thứ tự, tối đa tổng 64 MiB/upload.
  Gói chưa được xác nhận được gửi lại cùng ID, tối đa 5 phút. Gateway nhớ
  request đã xử lý trong cache giới hạn 512 mục/10 phút để tránh lưu trùng.
- Công việc lưu file và tải media chạy ngoài vòng đọc socket, giữ heartbeat
  và lệnh điều khiển hoạt động trong lúc lưu kết quả.
- Mất kết nối: extension reconnect bằng backoff; gateway giữ job đang chạy
  để gửi lại khi kết nối phục hồi. Extension đang sống nhận diện job trùng.
  Nếu worker đã khởi động lại, job chưa hoàn tất có thể chạy lại từ đầu.
  Queue/upload chưa hoàn tất nằm trong RAM, không sống qua restart gateway.

API HTTP `/ingest/job`, `/ingest/result` của launcher vẫn dành cho client cũ:
client xếp job và đọc kết quả qua HTTP, nhưng gateway trao đổi với extension
qua WebSocket. Reload extension tại `chrome://extensions` sau khi cập nhật.

## 8. Gọi typed action từ client bên ngoài launcher

Khi `python launch.py` đang chạy và extension đã kết nối, client gửi
`POST http://127.0.0.1:8766/browser/command` với header
`Authorization: Bearer <API_TOKEN>` (hoặc `X-Bridge-Token` chứa pairing token).
Không đưa token vào URL. API_TOKEN là token của launcher đang chạy.

```python
import json
import os
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8766/browser/command",
    data=json.dumps({
        "v": 1, "type": "command", "id": "list-tabs-1",
        "action": "tab.list", "params": {}, "deadlineMs": 10000,
    }).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["API_TOKEN"],
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(json.load(response))
```

Kết quả có `ok`, `id`, `result`, `error`. Deadline cho phép 1–120000 ms;
body tối đa 8 MiB. Sai xác thực trả HTTP 401, website origin không được phép
trả 403, envelope/params không hợp lệ trả 400. Lỗi thực thi trả `ok: false`
và error có cấu trúc. API chỉ gửi typed action qua WebSocket; extension chưa
kết nối sẽ trả lỗi, không chuyển thao tác DOM thành job cào dữ liệu HTTP.
HTTP ingest cũ vẫn dùng cho job cào dữ liệu. Extension tự lấy lại pairing token
của gateway mặc định sau khi launcher khởi động lại.


## 9. Chẩn đoán kết nối

- Chạy `run-web.bat` hoặc `.venv\Scripts\python.exe launch.py`. Chỉ chạy GUI,
  backend hoặc `run.bat` (TUI) sẽ không tự tạo helper WebSocket.
- Mở `http://127.0.0.1:8766/browser/status`: nếu không truy cập được, launcher
  chưa chạy hoặc helper lỗi khởi động. `connected: false` nghĩa là gateway
  đang chạy nhưng chưa có extension kết nối.
- Trong đúng cửa sổ/profile Chrome đang dùng: mở `chrome://extensions`, bật
  Developer mode, Load unpacked thư mục `browser-extension` của checkout này.
  Sau khi cập nhật code, bấm Reload extension.
- Popup hiển thị lỗi ghép nối/socket gần nhất. Connect lưu cấu hình trước khi
  thử kết nối. Disconnect được ghi nhớ qua lần worker khởi động lại.
- Ghép nối giới hạn 8 giây, handshake 10 giây; nếu gateway không gửi dữ liệu
  trong hơn 45 giây, watchdog heartbeat đóng socket và reconnect theo backoff.
  Các callback của lần kết nối cũ không được thay đổi kết nối mới.
- Chrome 137+ đã bỏ cờ `--load-extension` trong bản Chrome chính thức:
  https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY
