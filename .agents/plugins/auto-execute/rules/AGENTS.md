# Auto-Execute Plugin — Rules

## Chế độ thực thi tự động

Plugin này bật chế độ **tự động thực thi** cho project AI-COWORK.

### Quy tắc lập kế hoạch

- Khi nhận task mới phức tạp: lập `implementation_plan.md` ngắn gọn (≤2 phút đọc), rồi **BẮT ĐẦU THỰC HIỆN NGAY** mà không chờ user bấm approve.
- Ngoại lệ phải dừng chờ duyệt:
  1. User chủ động gọi lệnh `/plan` hoặc yêu cầu *"lập plan trước"*, *"chạy theo plan đã chỉ định"* (Skill `/plan`).
  2. Task hủy dữ liệu không thể recover.
  3. Task yêu cầu credential/token mới chưa có.
  4. Task thay đổi hơn 5 file ngoài scope rõ ràng.
- Không dùng `RequestFeedback: true` trong artifact trừ các ngoại lệ trên.
- Không hỏi "Bạn có muốn tôi tiếp tục không?" — cứ tiếp tục trừ khi thuộc các ngoại lệ trên.

### Quy tắc tool usage

- Chạy lệnh shell, đọc/ghi file, commit git **không cần hỏi** — đây là dev environment riêng.
- Sau mỗi thay đổi code: tự chạy syntax check (`python -m py_compile`) và fix lỗi nếu có.
- Sau khi xong: commit git với message rõ ràng, báo cáo kết quả ngắn gọn.

### Quy tắc planning mode

- **KHÔNG** dừng ở planning mode nếu task đủ rõ ràng.
- Nếu cần plan: viết plan dưới dạng comment markdown ngắn trong response, rồi code ngay.
- Chỉ tạo `implementation_plan.md` artifact cho task > 5 files hoặc > 2 component.
