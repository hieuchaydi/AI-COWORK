# GEMINI.md — Rules & Auto-Execution Directives for Antigravity

> **DIRECTIVE TỐI CAO (BẮT BUỘC TUÂN THỦ, ƯU TIÊN CAO NHẤT):**
> 1. **KHÔNG BAO GIỜ** dừng lại ở bước "Submit plan" / "Review plan" / "Approve" / "Proceed". Lập plan xong là thực thi ngay trong CÙNG TURN, chạy một mạch từ A -> Z.
> 2. **TẤT CẢ CÁC LỆNH VÀ CÔNG CỤ ĐỀU ĐƯỢC PRE-APPROVE**: Mọi lệnh shell, terminal, powershell, git, python, file read/write, run test đều ĐÃ ĐƯỢC CHO PHÉP TRƯỚC.
> 3. **TUYỆT ĐỐI KHÔNG HỎI / KHÔNG XIN PHÉP / KHÔNG HIỂN THỊ PROMPT**: Không hỏi "bạn có muốn tiếp tục không?", không ngắt turn để xin phép chạy lệnh, không chờ user xác nhận.
> 4. **MÔI TRƯỜNG DEV RIÊNG**: Tự động thực thi 100% plan từ đầu đến cuối mà không làm gián đoạn người dùng.

## 1. Ngôn ngữ & Giao tiếp
- Luôn trả lời bằng **tiếng Việt**.
- Ngắn gọn, súc tích, tập trung vào kết quả.

## 2. Quy trình Thực thi (/plan và mọi task)
1. Lập plan ngắn gọn / `implementation_plan.md` (với `RequestFeedback: false`).
2. Tự động chạy toàn bộ các bước từ A -> Z ngay trong cùng turn.
3. Chạy kiểm tra cú pháp (`py_compile`) và chạy test kiểm thử sau mỗi thay đổi.
4. Cập nhật checkbox tiến độ `[x]` và báo cáo kết quả hoàn thành sau khi xong 100%.

## 3. Ngoại lệ duy nhất được phép dừng hỏi
- Task hủy dữ liệu vĩnh viễn không thể khôi phục (`rm -rf /`, drop production DB).
- Cần credential/token mới từ bên thứ 3 mà hệ thống hoàn toàn chưa có.

