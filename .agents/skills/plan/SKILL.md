---
name: plan
description: >-
  Kích hoạt khi người dùng gõ /plan hoặc yêu cầu lập kế hoạch, làm theo kế hoạch đã chỉ định, hoặc thực thi theo roadmap từng bước có kiểm soát.
---

# Skill: Lập Kế Hoạch & Thực Thi Tự Động Từ A -> Z (/plan)

Skill này hướng dẫn Agent lên kế hoạch chi tiết, có cấu trúc rõ ràng và **ngay lập tức tự động thực thi xuyên suốt từ A đến Z** cho đến khi hoàn tất toàn bộ plan mà không dừng lại chờ user bấm submit / approve.

---

## 1. Khi nào áp dụng (/plan)

- Người dùng gõ `/plan` hoặc `/plan <nội dung task>`.
- Người dùng yêu cầu: *"lập plan trước"*, *"chạy theo plan đã chỉ định"*, *"làm theo PLAN.md"*.

---

## 2. Quy trình thực hiện từ A -> Z

### Bước 1: Khảo sát & Phân tích (Research)
- Đọc kỹ codebase, tìm các file liên quan trước khi lên danh sách thay đổi.
- Nếu người dùng chỉ định một file plan có sẵn (ví dụ `CRAWLER_LAYER_PLAN.md`, `BROWSER_CONTROL_PLANE_PLAN.md`), đọc kỹ file đó để lấy ngữ cảnh.

### Bước 2: Lập Implementation Plan (Kế hoạch hành động)
Tạo hoặc cập nhật artifact `implementation_plan.md` (hoặc trình bày rõ trong response) với cấu trúc:
1. **Mục tiêu (Goal)**: Mô tả ngắn gọn task cần làm.
2. **Danh sách các bước thực hiện (Milestones & Tasks)**: Đánh số thứ tự kèm checkbox `[ ]`.
   - Ghi rõ từng file `[NEW]`, `[MODIFY]`, `[DELETE]`.
3. **Kế hoạch kiểm thử (Verification Plan)**: Các lệnh test cụ thể sau mỗi bước.
- **Lưu ý**: Để `RequestFeedback: false`, **KHÔNG DỪNG LẠI CHỜ APPROVE**.

### Bước 3: Tự động thực thi xuyên suốt từ A -> Z (Execution until complete)
- **BẮT TAY LÀM NGAY** mốc đầu tiên trong cùng turn, không hỏi "bạn có muốn tiếp tục không?".
- Triển khai tuần tự qua từng bước theo đúng kế hoạch:
  - Sửa/tạo file tương ứng.
  - Chạy cú pháp (`python -m py_compile`) và chạy test kiểm thử ngay sau mỗi bước.
  - Cập nhật checkbox `[x]` trong `implementation_plan.md`.
- Tiếp tục làm cho đến khi **HOÀN THÀNH 100% CÁC BƯỚC TRONG PLAN**.

### Bước 4: Tổng kết & Commit Git
- Tạo artifact `walkthrough.md` báo cáo kết quả và log kiểm thử.
- Commit git với message rõ ràng theo quy chuẩn Conventional Commits.
- Báo cáo ngắn gọn cho người dùng.
