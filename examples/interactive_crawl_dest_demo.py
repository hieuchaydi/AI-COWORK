"""Demo: Interactive destination folder selection when scraping data.
Demonstrates custom output_dir routing vs fallback to default outputs/ directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connect-ai"))
sys.path.insert(0, str(ROOT))

from coworker.tools.media_pipeline import crawl_and_export_bundle


def simulate_interactive_crawl(user_selected_dir: str = ""):
    print("=" * 70)
    print("DEMO: TÙY CHỌN THƯ MỤC LƯU TRỮ KHI CÀO DỮ LIỆU")
    print(f"Người dùng chọn: {'[BỎ QUA / MẶC ĐỊNH]' if not user_selected_dir else user_selected_dir}")
    print("=" * 70)

    # Dữ liệu cào mẫu
    sample_items = [
        {
            "id": "prod_101",
            "name": "Bàn phím cơ không dây",
            "price": 1250000,
            "rating": 4.9,
            "image": "https://raw.githubusercontent.com/github/explore/main/topics/python/python.png",
        }
    ]

    # Thực thi pipeline cào và đóng gói
    res = crawl_and_export_bundle(
        rows=sample_items,
        job_name="ban_phim_co",
        output_dir=user_selected_dir.strip(),
    )

    csv_path = Path(res["csv"]["path"])
    print("\nKết quả xuất:")
    print(f" -> File CSV:  {csv_path} (Tồn tại: {csv_path.exists()})")
    print(f" -> Link CSV:  {res['csv']['url']}")

    if res.get("zip"):
        zip_path = Path(res["zip"]["path"])
        print(f" -> File ZIP:  {zip_path} (Tồn tại: {zip_path.exists()})")
        print(f" -> Link ZIP:  {res['zip']['url']}")

    # Kiểm tra xem có đúng thư mục chỉ định hay không
    if user_selected_dir:
        assert str(Path(user_selected_dir).resolve()) in str(csv_path.resolve())
        print(f"\n[Xác nhận]: Dữ liệu đã lưu chính xác vào thư mục tùy chỉnh: {user_selected_dir}")
    else:
        assert "outputs" in str(csv_path)
        print("\n[Xác nhận]: Người dùng bỏ qua -> Dữ liệu tự động lưu vào thư mục mặc định 'outputs/' của dự án.")


if __name__ == "__main__":
    # 1. Thử nghiệm khi người dùng bỏ qua (dùng mặc định)
    print("\n--- TEST CASE 1: NGƯỜI DÙNG BẤM 'SKIP' HOẶC CHỌN MẶC ĐỊNH ---")
    simulate_interactive_crawl(user_selected_dir="")

    # 2. Thử nghiệm khi người dùng nhập thư mục tùy chỉnh
    test_custom = str(ROOT / "scratch" / "my_custom_folder")
    print("\n--- TEST CASE 2: NGƯỜI DÙNG NHẬP THƯ MỤC TÙY CHỈNH ---")
    simulate_interactive_crawl(user_selected_dir=test_custom)
