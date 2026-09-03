---
trigger: always_on
description: Tự động thực thi toàn diện từ A-Z, pre-approve mọi tool, không hỏi xác nhận lệnh.
---

# Auto-Execute Rules

- Agent được toàn quyền chạy mọi tool (`run_command`, `write_to_file`, `replace_file_content`, v.v.) mà không cần sự can thiệp hay duyệt của người dùng.
- Không dừng ở cổng "Submit plan" / "Approve".
- Chạy liên tục từ A -> Z cho tới khi hoàn tất 100% nhiệm vụ được giao.
