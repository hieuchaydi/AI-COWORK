---
name: plan
description: >-
  Kích hoạt khi người dùng gõ /plan hoặc yêu cầu lập kế hoạch, làm theo kế hoạch đã chỉ định, hoặc thực thi theo roadmap từng bước có kiểm soát.
---

# Skill: Lập Kế Hoạch & Thực Thi Theo Plan (/plan)

Skill này hướng dẫn Agent tạm dừng chế độ tự động thực thi tự do (Auto-Execute), tập trung lập kế hoạch chi tiết, trình bày cho người dùng duyệt và thực thi tuần tự theo từng mốc đã cam kết.

---

## 1. Khi nào áp dụng (/plan)

- Người dùng gõ `/plan` hoặc `/plan <nội dung task>`.
- Người dùng yêu cầu: *"lập plan trước"*, *"chạy theo plan đã chỉ định"*, *"làm theo PLAN.md"*, hoặc *"không được tự ý sửa code khi chưa duyệt plan"*.

---

## 2. Quy trình 4 bước

### Bước 1: Khảo sát & Phân tích (Research)
- Đọc kỹ codebase, tìm các file liên quan trước khi lên danh sách thay đổi.
- Nếu người dùng chỉ định một file plan có sẵn (ví dụ `CRAWLER_LAYER_PLAN.md`, `BROWSER_CONTROL_PLANE_PLAN.md`), đọc kỹ file đó để lấy ngữ cảnh.
- **TUYỆT ĐỐI KHÔNG** sửa code hoặc chạy lệnh làm biến đổi dữ liệu trong bước này.

### Bước 2: Lập Implementation Plan Artifact
Tạo hoặc cập nhật artifact `implementation_plan.md` (hoặc hiển thị rõ trong response) với cấu trúc:
1. **Mục tiêu (Goal)**: Mô tả ngắn gọn task cần làm.
2. **User Review Required / Open Questions**: Những điểm cần người dùng quyết định trước.
3. **Danh sách các bước thực hiện (Milestones & Tasks)**: Đánh số thứ tự kèm checkbox `[ ]`.
   - Ghi rõ từng file `[NEW]`, `[MODIFY]`, `[DELETE]`.
4. **Kế hoạch kiểm thử (Verification Plan)**: Các lệnh test cụ thể sau mỗi bước.

### Bước 3: Đợi người dùng duyệt (Alignment)
- Trình bày tóm tắt plan ngắn gọn cho người dùng.
- Bật `RequestFeedback: true` trên artifact `implementation_plan.md`.
- Đặt câu hỏi hoặc chờ người dùng phản hồi: *"Proceed"*, *"Chấp thuận"*, *"OK làm đi"*, hoặc điều chỉnh các bước nếu cần.

### Bước 4: Thực thi tuần tự theo Plan (Step-by-Step Execution)
- Khi người dùng đã duyệt:
  - Chỉ làm **đúng phạm vi** của từng bước theo thứ tự.
  - Sau mỗi bước: chạy cú pháp / test kiểm thử ngay.
  - Cập nhật checkbox `[x]` trong `implementation_plan.md` hoặc báo cáo mốc vừa hoàn thành.
  - Kết thúc task: báo cáo tóm tắt bằng `walkthrough.md` và commit git với message chuẩn.
