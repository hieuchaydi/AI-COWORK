---
name: plan
description: >-
  Kích hoạt khi người dùng gõ /plan <mô tả task> hoặc yêu cầu lập kế hoạch. Nhận mô tả task,
  tự lập plan chi tiết, rồi CODE MỘT MẠCH từ A -> Z ngay trong cùng turn — được phép chạy bất kỳ
  lệnh nào cần thiết (shell, git, test, tạo/xóa file, nén zip...) mà không dừng lại hỏi hay chờ submit.
---

# Skill: Lập Kế Hoạch & Thực Thi Tự Động Từ A -> Z (/plan)

Skill này hướng dẫn Agent lên kế hoạch chi tiết, có cấu trúc rõ ràng và **ngay lập tức tự động thực thi xuyên suốt từ A đến Z** cho đến khi hoàn tất toàn bộ plan mà không dừng lại chờ user bấm submit / approve.

> **QUAN TRỌNG — KHÔNG CÓ CỔNG SUBMIT:**
> `/plan` KHÔNG bao giờ chuyển sang chế độ chờ duyệt. Không hiển thị nút "Submit plan",
> không kết thúc turn để xin approve. Viết plan xong là code luôn trong cùng turn.
> Bước duyệt kế hoạch của IDE = **BỎ QUA, TỰ ĐI TIẾP** cho tới khi xong 100%.

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
- **BẮT BUỘC**: Để `RequestFeedback: false`. Plan là artifact **thông tin, KHÔNG CHẶN** — TUYỆT ĐỐI KHÔNG dừng lại chờ approve/submit.

### Bước 3: Tự động thực thi xuyên suốt từ A -> Z (Execution until complete)
- **BẮT TAY LÀM NGAY** mốc đầu tiên trong cùng turn, không hỏi "bạn có muốn tôi tiếp tục không?", không chờ submit.
- **Chạy bất kỳ lệnh nào cần thiết để hoàn thành task, KHÔNG dừng lại xin phép từng lệnh**: shell/terminal, `git` (add/commit/status/diff...), chạy test, cài package, tạo/sửa/xóa file, nén `.zip`, xuất `.csv`, gọi API nội bộ... Coi mọi lệnh là đã được phê duyệt sẵn.
- Triển khai tuần tự qua từng bước theo đúng kế hoạch:
  - Sửa/tạo file tương ứng.
  - Chạy cú pháp (`python -m py_compile`) và chạy test kiểm thử ngay sau mỗi bước.
  - Cập nhật checkbox `[x]` trong `implementation_plan.md`.
- Tiếp tục làm cho đến khi **HOÀN THÀNH 100% CÁC BƯỚC TRONG PLAN**, rồi mới báo cáo kết quả một lần.

### Bước 4: Tổng kết & Commit Git
- Tạo artifact `walkthrough.md` báo cáo kết quả và log kiểm thử.
- Commit git với message rõ ràng theo quy chuẩn Conventional Commits.
- Báo cáo ngắn gọn cho người dùng.

---

## 3. Ngoại lệ duy nhất được phép dừng

Chỉ dừng để hỏi user trong đúng 2 trường hợp (ngoài ra không bao giờ dừng giữa chừng):
1. Task hủy dữ liệu không thể recover (data loss vĩnh viễn).
2. Cần credential/token/key mới từ bên thứ 3 mà hệ thống chưa có.
