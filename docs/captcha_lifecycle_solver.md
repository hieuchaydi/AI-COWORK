# Tài Liệu Kỹ Thuật: Cơ Chế Quản Lý Vòng Đời & Giải CAPTCHA Shopee (Per-Image Deadline)

Tài liệu này mô tả chi tiết kiến trúc, công nghệ triển khai và bộ prompt mẫu để tái sử dụng hệ thống tự động phát hiện và giải CAPTCHA trượt của Shopee trên Chrome Extension MV3 và Python sidecar.

---

## 1. Mục Tiêu & Nguyên Lý Kiến Trúc

### Bối cảnh bài toán
Trên Shopee, thử thách CAPTCHA kéo ghép hình (slider puzzle) có những đặc tính khắt khe:
1. **Thời hạn từng ảnh (Image Lifetime)**: Mỗi ảnh thử thách chỉ có hiệu lực trong khoảng ~7 giây. Sau khoảng thời gian này, dù mảnh ghép được kéo chính xác vào ô thì server vẫn từ chối hoặc tự sinh ảnh mới.
2. **Ngân sách lượt thử giới hạn (Rate Limits / Attempt Budget)**: Shopee chỉ cho phép tối đa khoảng 3 lần thử sai trước khi khóa hoàn toàn (`/verify/traffic` hoặc thông báo "Vui lòng thử lại sau").
3. **Bẫy nhầm lẫn trạng thái (False Success Trap)**: Việc Chrome hoàn thành chuỗi sự kiện chuột (`Input.dispatchMouseEvent`) **chỉ là Chrome ACK việc gửi input**, hoàn toàn không đồng nghĩa với việc Shopee chấp nhận CAPTCHA.
4. **Đổi ảnh giữa chừng (Mid-flight Replacement)**: Khi ảnh cũ đổi sang ảnh mới, nếu luồng tính toán CV cũ vẫn tiếp tục nhả lệnh kéo chuột của ảnh cũ thì sẽ gây sai lệch và lãng phí một lượt thử.

### Giải pháp: Quản lý vòng đời theo thời hạn từng ảnh (Per-Image Deadline)
Hệ sinh thái kết hợp chặt chẽ giữa:
- **Chrome Extension Manifest V3**: Hoạt động trực tiếp trên Profile Chrome thật với đầy đủ cookie/phiên của người dùng.
- **Python OpenCV Solver cục bộ** (trong `browser_bridge/captcha_detector.py`): Không sử dụng bất kỳ API hay dịch vụ bên thứ 3 nào.
- **Không thêm dịch vụ hay chi phí phát sinh**.

---

## 2. Các Thành Phần Cốt Lõi

```
┌────────────────────────────────────────────────────────────────────────┐
│                          Shopee Web Page                               │
│  [DOM Canvas / Puzzle Img] ─── (MutationObserver / 250ms interval)    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│            captcha-observer.js (Content Script document_start)         │
│  - Downsample 16x10 pixel grid để theo dõi biến đổi ảnh (chống lag)    │
│  - Ghi nhận chính xác mốc thời gian xuất hiện: `seenAt`                │
│  - Phát hiện trạng thái: present | absent | locked | resolved          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ chrome.runtime.sendMessage
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│            captcha-lifecycle.js (CaptchaLifecycle.Ledger)             │
│  - LIFETIME_MS = 7000ms | INPUT_RESERVE_MS = 1000ms | MIN = 2800ms     │
│  - Ngân sách: MAX_JOB_ATTEMPTS = 3 | MAX_FRAME_ATTEMPTS = 2            │
│  - Hủy ngay lập tức (AbortController signal) khi ảnh đổi hoặc hết hạn  │
│  - Heartbeat không bao giờ cấp lại lượt thử đã dùng                    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   background.js (Service Worker)                       │
│  - Điều phối `captcha.solve` / `captcha.autoSolve`                    │
│  - Gửi screenshot sang Python sidecar (`solve_puzzle_cv`)              │
│  - CDP Debugger dispatch đường kéo bezier tự nhiên (có easing)         │
│  - Giải phóng chuột an toàn (`mouseReleased`) khi có lỗi hoặc abort    │
│  - Hai lớp xác nhận thành công: `slider_passed` hoặc preflight 200     │
└────────────────────────────────────────────────────────────────────────┘
```

### Chi tiết các module:

1. **`browser-extension/captcha-observer.js`**:
   - Chạy ở `document_start`, hoàn toàn thụ động (không can thiệp network, không hook script trang, không dispatch chuột).
   - Downsample canvas thử thách về kích thước 16x10 (`sample canvas`), lấy 160 giá trị màu để tạo "vân tay ảnh" (fingerprint).
   - Nếu tỉ lệ pixel khác biệt > 25% thì định danh là ảnh mới (`documentKey:sequence`), ngược lại chỉ là rung lắc/chuyển động sprite nhỏ thì giữ nguyên.
   - Báo cáo chính xác `knownStart: true/false`. Nếu script nạp muộn khi ảnh đã hiện sẵn thì `knownStart = false` để tránh đoán bừa deadline.
   - Nhận diện trạng thái bị khóa (`locked`): "vui lòng thử lại sau", "chưa thể hoàn tất xác thực", "please try again later", "too many attempts".

2. **`browser-extension/captcha-lifecycle.js`**:
   - Quản lý hạn giờ (`expiresAt = seenAt + 7000ms`).
   - Nếu thời gian còn lại không đủ cho thời gian tối thiểu một lượt kéo (`MIN_ATTEMPT_MS = 2800ms + 1000ms dự trữ`), nó từ chối ngay lập tức với lý do `insufficient_time` thay vì kéo dở dang làm hỏng lượt.
   - Kiểm soát số lượt thử tối đa cho từng ảnh (tối đa 2 lần/ảnh) và toàn bộ job (tối đa 3 lần).
   - Đồng bộ và khôi phục ngân sách qua `chrome.storage.local`, không để heartbeat reset ngân sách.

3. **`browser-extension/background.js`**:
   - Hàm `runCaptchaChallenge(tabId, key, request)` tích hợp `captchaLedger.run`.
   - Kết nối `AbortController` xuyên suốt: nếu ảnh đổi giữa chừng thì cờ abort lập tức ngắt lệnh đo ảnh và lệnh debugger.
   - Trong `tryAutoDragShopeeCaptcha`, khi có abort hoặc lỗi thì khối `finally` đảm bảo gửi sự kiện `mouseReleased` để không làm kẹt nút chuột ảo trên màn hình.
   - **Xác nhận thành công chặt chẽ**: Vòng lặp chờ xác nhận kiểm tra xem nút trượt có chuyển sang trạng thái thành công (`slider_passed`) hoặc gọi API `preflightRatingsInTab` trả về HTTP 200. Nếu ảnh biến mất mà không có xác nhận thì không được tính là thành công.

---

## 3. Quy Trình Kiểm Thử & Kết Quả Xác Minh

Hệ thống được bảo vệ bởi 92 bài test tự động chạy bằng Node.js test runner (`node --test`) và pytest:

1. **`tests/captcha_lifecycle.test.cjs`**:
   - Ca 1: Lần 1 thất bại, lần 2 hoàn thành và được xác nhận khi ảnh 7s vẫn còn hiệu lực.
   - Ca 2: Ảnh cũ không được kéo dài thời gian sống qua việc quan sát lặp lại.
   - Ca 3: Ảnh đổi giữa chừng thì lệnh solver chậm bị hủy và không có thao tác chuột thừa nào được gửi.
   - Ca 4: Stage deadline ngắt signal đúng lúc thay vì để stage tiếp theo chạy trôi.
   - Ca 5: Huỷ lệnh từ bên ngoài truyền sâu vào quá trình đang chạy.
   - Ca 6: Heartbeat khôi phục giữ nguyên số lượt thử đã tiêu tốn.
   - Ca 7: Ảnh xuất hiện từ trước không rõ tuổi (`knownStart = false`) thì nhường quyền (passive handover).
2. **`tests/captcha_observer.test.cjs`**:
   - Đo thời điểm bắt đầu vẽ ảnh ở `document_start`.
   - Bắt sự kiện thay đổi background canvas khi pixel đổi > 25%.
   - Bỏ qua chuyển động sprite nhỏ.
   - Đánh dấu `identityReliable: false` khi canvas bị tainted (CORS).
3. **`tests/browser_extension_websocket.test.cjs`**:
   - Toàn bộ 78 bài test tích hợp WebSocket, CDP drag, CV distance, phân loại lỗi Shopee.
4. **`tests/test_browser_command_api.py`**:
   - Kiểm thử endpoint HTTP API `:8766/browser/command`, correlation ID, và trạng thái abstain của solver.

---

## 4. Hướng Dẫn Sử Dụng & Prompt Mẫu (Reusable Prompts)

### Cách kích hoạt luồng giải CAPTCHA

#### Cách 1: Tự động hoàn toàn qua Ingest Job Queue (Khuyên Dùng)
Khi xếp job cào Shopee, hệ thống tự kích hoạt watcher và auto-solve khi gặp CAPTCHA:
```bash
curl -s "http://127.0.0.1:8766/ingest/job?url=https://shopee.vn/product/95736611/23376538777"
```
Khi phát hiện CAPTCHA:
- Extension tự động phát hiện bằng `captcha-observer.js`.
- Bật `runCaptchaChallenge`.
- Chụp ảnh màn hình bằng CDP, gửi về endpoint `:8766/browser/solve_puzzle_cv`.
- Kéo thanh trượt theo đường cong sinh học.
- Xác nhận bằng preflight ratings.

#### Cách 2: Gọi trực tiếp qua Action API
Nếu muốn giải CAPTCHA trên một tab cụ thể (ví dụ `tabId = 1834415038`):
```bash
curl -s "http://127.0.0.1:8766/browser/action?action=captcha.solve&params={\"tabId\":1834415038,\"attempt\":1}"
```
Phản hồi trả về:
```json
{
  "ok": true,
  "action": "captcha.solve",
  "result": {
    "ok": true,
    "solved": true,
    "status": "confirmed",
    "challengeId": "178918234-abc:1",
    "attempted": true
  },
  "error": null
}
```

---

### Mẫu Prompt Tái Sử Dụng Cho AI Agent (Agent Prompt Template)

Khi làm việc với các agent AI (DeepSeek, Claude, ChatGPT, Gemini), bạn có thể dùng đoạn prompt chuẩn dưới đây để hướng dẫn agent vận hành luồng này một cách chuẩn xác:

````markdown
Bạn là trợ lý tự động hóa làm việc với hệ thống AI-COWORK Browser Bridge.
Nhiệm vụ của bạn là kiểm tra, phát hiện và giải quyết thử thách CAPTCHA trượt trên Shopee theo đúng quy tắc Per-Image Deadline:

1. KIẾN TRÚC & CÔNG CỤ:
   - Sử dụng Chrome Extension MV3 và cổng HTTP helper tại `http://127.0.0.1:8766`.
   - Tuyệt đối không dùng thư viện cào ngoài browser (requests, httpx) đối với Shopee vì sẽ bị 403 Forbidden.
   - Sử dụng lệnh `curl.exe` hoặc HTTP client gọi `/browser/action` với các action precompiled: `tab.list`, `tab.open`, `captcha.solve`, `page.scroll`.

2. QUY TẮC QUẢN LÝ VÒNG ĐỜI (PER-IMAGE DEADLINE):
   - Mỗi ảnh CAPTCHA có hạn sống tối đa 7000ms kể từ khi `captcha-observer.js` phát hiện (`seenAt`).
   - Nếu ảnh thay đổi giữa chừng, hệ thống tự động abort tác vụ cũ. Không cố gửi thêm lệnh chuột cũ.
   - Giới hạn tối đa 2 lần thử trên một ảnh, và tối đa 3 lần thử trên toàn bộ phiên làm việc.
   - Nếu xuất hiện thông báo "Vui lòng thử lại sau" hoặc "Chưa thể hoàn tất xác thực", hệ thống rơi vào trạng thái `locked` -> Phải dừng ngay và kích hoạt passive handover cho người dùng, tuyệt đối không tiếp tục kéo làm cháy tài khoản.

3. XÁC MINH KẾT QUẢ:
   - Không được coi việc gửi input chuột thành công là đã giải xong CAPTCHA.
   - Chỉ kết luận thành công khi `status == "confirmed"`, nút trượt có class `.shopee-captcha-slider__btn--success` hoặc API preflight ratings trả về HTTP 200.

4. CÁC BƯỚC THỰC THI CHUẨN:
   Bước 1: Kiểm tra trạng thái bridge:
     `curl.exe -s http://127.0.0.1:8766/browser/status` (đảm bảo "connected": true).
   Bước 2: Lấy tabId của trang Shopee cần xử lý:
     `curl.exe -s http://127.0.0.1:8766/browser/action?action=tab.list`
   Bước 3: Gọi lệnh giải CAPTCHA:
     `curl.exe -s "http://127.0.0.1:8766/browser/action?action=captcha.solve&params={\"tabId\":<TAB_ID>,\"attempt\":1}"`
   Bước 4: Đọc kết quả từ JSON trả về và tiến hành các bước tiếp theo.
````
