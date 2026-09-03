# Auto-Execute Plugin — Rules

## Chế độ thực thi tự động (Toàn diện từ A -> Z)

Plugin này bật chế độ **tự động thực thi toàn diện** cho project AI-COWORK.

### Quy tắc lập kế hoạch & Thực thi (/plan và mọi task)

- Khi nhận task (kể cả khi user gõ `/plan` hoặc yêu cầu lập plan):
  1. Lập kế hoạch / `implementation_plan.md` rõ ràng với các milestone và checkbox `[ ]`.
  2. **BẮT ĐẦU THỰC HIỆN NGAY LẬP TỨC TỪ A -> Z**, không dừng lại chờ user bấm submit / approve / proceed.
  3. Chạy liên tục qua từng bước, tick `[x]` hoàn thành từng mục, kiểm tra cú pháp/test sau mỗi bước cho đến khi **HẾT TOÀN BỘ PLAN**.
- **KHÔNG** dùng `RequestFeedback: true` trong artifact.
- **KHÔNG** hỏi "Bạn có muốn tôi tiếp tục không?" hoặc yêu cầu user duyệt để bắt đầu — cứ tự động triển khai hết plan.
- Ngoại lệ DUY NHẤT phải dừng hỏi:
  1. Task hủy dữ liệu không thể recover (data loss vĩnh viễn).
  2. Cần credential/token/key mới từ bên thứ 3 mà hệ thống chưa có.

### Quy tắc tool usage

- Chạy lệnh shell, đọc/ghi file, commit git **không cần hỏi** — đây là dev environment riêng.
- Sau mỗi thay đổi code: tự chạy syntax check (`python -m py_compile`) và fix lỗi nếu có.
- Sau khi xong: commit git với message rõ ràng, báo cáo kết quả hoàn tất.

### Quy tắc planning mode

- Lập plan xong là **thực thi ngay trong cùng turn**, không dừng lại ở planning mode.
- Cập nhật tiến độ xuyên suốt quá trình thực thi từ A đến Z.
