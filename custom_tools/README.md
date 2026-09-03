# Custom Tools (Thư mục Tool Tự Do)

Thư mục này cho phép bạn tạo và sử dụng tool mới **siêu nhanh** mà không cần sửa code lõi của server hay restart hệ thống phức tạp.

## Cách tạo tool mới trong 30 giây:

1. Tạo một file Python bất kỳ trong thư mục này, ví dụ `custom_tools/currency.py`.
2. Định nghĩa hàm, gán decorator `@coworker_tool`, có type hints và docstring:

```python
from typing import Any
from coworker.tools.base import coworker_tool, tool_success, tool_error

@coworker_tool(
    name="convert_currency",
    category="custom",           # "custom" | "core" | "browser" | "crawl"
    requires_approval=False,     # True nếu là hành động nguy hiểm cần hỏi user duyệt
    description="Chuyển đổi tiền tệ theo tỷ giá gần đúng."
)
def convert_currency(amount: float, from_curr: str, to_curr: str) -> dict[str, Any]:
    """Chuyển đổi số tiền giữa hai loại tiền tệ (USD, VND, EUR)."""
    # Logic xử lý của bạn ở đây...
    rates = {"USD_VND": 25400.0, "EUR_VND": 27200.0}
    pair = f"{from_curr.upper()}_{to_curr.upper()}"
    if pair not in rates:
        return tool_error(f"Chưa hỗ trợ cặp tỷ giá {pair}")
    
    result = amount * rates[pair]
    return tool_success(data={"result": result, "rate": rates[pair]})
```

3. Xong! Khi server khởi động hoặc tạo session mới, tool sẽ tự động được phát hiện và nạp vào danh sách công cụ mà Agent có thể gọi.
