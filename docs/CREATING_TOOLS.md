# Hướng Dẫn Tạo & Cập Nhật Tool Trong AI-COWORK

Tài liệu này hướng dẫn 2 cách tạo công cụ (tool) mới:
1. **Cách nhanh nhất (30 giây)**: Tạo Custom Tool tự do qua thư mục `custom_tools/`.
2. **Cách tự động hóa qua CLI**: Dùng `tools/scaffold_tool.py` để sinh code chuẩn kèm unit test.

---

## Cách 1: Tạo Custom Tool tự do (Khuyên dùng)

Hệ thống đã hỗ trợ **Auto-Discovery**. Bất kỳ file Python nào đặt trong thư mục `custom_tools/` có hàm gắn decorator `@coworker_tool` sẽ được tự động nạp vào agent khi khởi động.

### Bước 1: Tạo file Python trong `custom_tools/`
Ví dụ tạo file `custom_tools/convert_currency.py`:

```python
from typing import Any
from coworker.tools.base import coworker_tool, tool_success, tool_error

@coworker_tool(
    name="convert_currency",
    category="custom",           # Thuộc tính danh mục: "custom", "core", "browser", v.v.
    requires_approval=False,     # True nếu cần người dùng xác nhận trước khi thực hiện
    description="Chuyển đổi tiền tệ giữa USD và VND."
)
def convert_currency(amount: float, to_vnd: bool = True) -> dict[str, Any]:
    """Chuyển đổi tiền tệ với tỷ giá tham khảo.

    Args:
        amount: Số tiền cần chuyển đổi.
        to_vnd: True nếu chuyển USD sang VND, False nếu ngược lại.
    """
    if amount <= 0:
        return tool_error("Số tiền phải lớn hơn 0.")

    rate = 25400.0
    converted = amount * rate if to_vnd else amount / rate
    return tool_success(
        data={
            "original_amount": amount,
            "converted_amount": round(converted, 2),
            "currency": "VND" if to_vnd else "USD",
        }
    )
```

### Bước 2: Sử dụng
Không cần cấu hình gì thêm! Agent sẽ tự động thấy tool `convert_currency` và có thể gọi khi cần.

---

## Cách 2: Sinh Tool Tự Động Qua CLI (`scaffold_tool.py`)

Nếu bạn muốn sinh nhanh code chuẩn kèm file unit test tự động, hãy dùng CLI:

```bash
# Tạo custom tool:
python tools/scaffold_tool.py --name translate_text --desc "Dịch văn bản sang tiếng Việt"

# Tạo core tool (nằm trong thư mục connect-ai/coworker/tools/):
python tools/scaffold_tool.py --name translate_text --type core --category dev_shell --desc "Dịch văn bản"
```

Lệnh trên sẽ tự động:
1. Tạo file code mẫu có sẵn docstring, type-hints, error handling.
2. Tạo file kiểm thử unit test tương ứng trong `connect-ai/tests/test_tool_<name>.py`.

### Chạy kiểm thử cho tool vừa tạo:
```bash
pytest connect-ai/tests/test_tool_translate_text.py
```

---

## Quy Chuẩn Viết Tool Chuẩn (Best Practices)

1. **Luôn có Type Hints**: Mọi tham số và giá trị trả về cần có kiểu dữ liệu (`str`, `int`, `float`, `bool`, `Optional[...]`) để AI engine tự sinh OpenAI JSON Schema chính xác.
2. **Luôn có Docstring đầy đủ**: Mô tả rõ mục đích của tool và từng tham số. LLM dựa hoàn toàn vào docstring để quyết định khi nào nên gọi tool.
3. **Trả về chuẩn qua `tool_success` / `tool_error`**:
   - Thành công: `return tool_success(data={...})`
   - Thất bại: `return tool_error("Lý do lỗi")`
   - Tuyệt đối không để văng Exception chưa bắt, làm gián đoạn hội thoại của agent.
4. **Phân quyền rõ ràng**:
   - `requires_approval=False`: Áp dụng cho các thao tác đọc, tra cứu, tính toán, crawl an toàn.
   - `requires_approval=True`: Áp dụng cho các thao tác ghi đè file, xoá dữ liệu, gửi tin nhắn ra bên ngoài, chạy shell command.
