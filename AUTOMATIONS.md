# Automations — agent chạy theo lịch

connect-AI có scheduler built-in: đặt lịch cho agent tự chạy một prompt (lặp lại theo cron hoặc
một lần duy nhất). Không cần setup gì thêm — chỉ cần yêu cầu trong chat.

Task chỉ chạy khi launcher đang chạy. Lỡ một lần fire thì scheduler bù **một** lần ngay lần boot
kế tiếp, không bù dồn.

---

## Cách 1 — bảo agent tạo (nhanh nhất)

Gõ một câu tự nhiên trong chat. Agent gọi tool `create_scheduled_task` và hỏi approve trước khi
lưu (đây là action có side-effect).

Ví dụ:

- `Mỗi ngày 9h sáng, đọc Gmail inbox rồi gửi tóm tắt tiếng Việt vào Telegram chat 6973629128`
- `Every Monday at 8am, list my GitHub PRs opened last week and email me the count`
- `Chạy một lần lúc 2026-09-01 14:00, lấy top 10 Hacker News rồi lưu title+URL vào top-hn.md`
- `Mỗi giờ, kiểm tra Google Drive có file mới trong 1 giờ qua và ghi vào memory entity "drive-activity"`

Bấm **Approve** trên card → task được lưu, scheduler chạy đúng lịch.

Quản lý task đã tạo: sidebar **Scheduled** trong GUI — xem lần chạy gần nhất, bật/tắt, chạy thử
ngay, hoặc xoá.

---

## Cách 2 — REST API

Mọi request cần header `x-connect-ai-token` (giá trị = `CONNECT_AI_API_TOKEN` trong `.env`).

```bash
TOKEN=$(grep CONNECT_AI_API_TOKEN .env | cut -d= -f2)

# Liệt kê
curl -s -H "x-connect-ai-token: $TOKEN" http://127.0.0.1:8765/v1/automations

# Tạo
curl -s -X POST -H "Content-Type: application/json" \
  -H "x-connect-ai-token: $TOKEN" \
  -d '{
    "title": "Daily inbox summary",
    "instructions": "Đọc email chưa đọc trong 24h qua và tóm tắt gạch đầu dòng bằng tiếng Việt.",
    "cron": "0 9 * * *",
    "timezone": "Asia/Ho_Chi_Minh"
  }' \
  http://127.0.0.1:8765/v1/automations

# Tắt tạm
curl -X PATCH -H "Content-Type: application/json" \
  -H "x-connect-ai-token: $TOKEN" \
  -d '{"enabled": false}' \
  http://127.0.0.1:8765/v1/automations/<task_id>

# Xoá
curl -X DELETE -H "x-connect-ai-token: $TOKEN" \
  http://127.0.0.1:8765/v1/automations/<task_id>
```

> Header cũ `x-openworker-token` vẫn được chấp nhận, nên script cũ không gãy.

---

## Cú pháp cron

Chuẩn POSIX 5 trường:

```
┌─ phút (0-59)
│  ┌─ giờ (0-23)
│  │  ┌─ ngày trong tháng (1-31)
│  │  │  ┌─ tháng (1-12)
│  │  │  │  ┌─ thứ (0-6, 0 = Chủ nhật)
│  │  │  │  │
0  9  *  *  1     # 9:00 mỗi thứ Hai
*/15 * * * *      # mỗi 15 phút
0 */2 * * *       # mỗi 2 giờ
0 8 1 * *         # 8:00 ngày 1 hằng tháng
```

## Timezone

Mặc định là giờ máy. Ép giờ Việt Nam:

```json
{"timezone": "Asia/Ho_Chi_Minh"}
```

`tzdata` đã nằm trong deps (Windows không có sẵn IANA tz database), nên mọi timezone IANA đều
dùng được.

---

## Permissions — đọc kỹ trước khi Approve

Khi tạo task, agent đề xuất danh sách tool được phép chạy **không hỏi lại** lúc task fire. Đây là
uỷ quyền trước: task chạy lúc bạn không có mặt nên không thể approve từng bước.

- Tương đối an toàn: `gmail:read`, `drive:read`, `notion:write:page/<id>` (ghi vào đúng một trang)
- Cần cân nhắc: `shell:*`, `github:write:*`, bất kỳ quyền write không giới hạn target

Task nào gặp việc ngoài phạm vi đã uỷ quyền sẽ **park vào Inbox** chờ bạn duyệt, chứ không tự ý
làm.

## Nơi lưu

```
%APPDATA%\coworker\automations.json
```

File JSON thường — đọc/sửa tay được, runtime nạp lại khi restart.

---

## Model dùng cho task chạy nền

Task dùng model mặc định trừ khi bạn chỉ định riêng. Hết quota giữa chừng thì task **không chết**:
runtime tự chuyển sang model kế trong picker, và nếu mọi model đều hết thì park lại chờ limit hồi
(xem mục 6 trong [SETUP.md](SETUP.md)). Có thêm một key free (Groq) làm chuỗi failover đáng tin
hơn nhiều cho task chạy đêm.
