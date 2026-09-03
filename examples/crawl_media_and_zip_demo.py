"""Example demo: Scraping data with images & videos, saving to CSV,
downloading media into a dedicated folder, and archiving to .ZIP.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Fix Windows console cp1252 encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add project root and connect-ai to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connect-ai"))
sys.path.insert(0, str(ROOT))

from coworker.tools.crawl import _save_csv, _zip_folder, _output_subdir, _OUTPUTS_BASE_URL
from coworker.tools.media_pipeline import crawl_and_export_bundle


def run_demo():
    print("=" * 70)
    print("DEMO: CÀO DỮ LIỆU CÓ HÌNH ẢNH & VIDEO -> XUẤT CSV & ĐÓNG GÓI .ZIP")
    print("=" * 70)

    # 1. Giả lập tập dữ liệu cào được từ sản phẩm Thương mại điện tử / Đánh giá
    job_id = "shopee_item_8829103"
    scraped_reviews = [
        {
            "review_id": "rev_001",
            "username": "nguyen_van_a",
            "rating": 5,
            "comment": "Sản phẩm đóng gói rất cẩn thận, dùng thử thấy rất êm!",
            "media_image": "https://raw.githubusercontent.com/github/explore/main/topics/python/python.png",
            "date": "2026-09-01 14:20:00",
        },
        {
            "review_id": "rev_002",
            "username": "tran_thi_b",
            "rating": 5,
            "comment": "Giao hàng siêu nhanh, có kèm cả video mở hộp cho mọi người xem",
            "media_image": "https://raw.githubusercontent.com/github/explore/main/topics/javascript/javascript.png",
            "date": "2026-09-02 09:15:30",
        },
        {
            "review_id": "rev_003",
            "username": "le_hoang_c",
            "rating": 4,
            "comment": "Chất lượng ổn áp trong tầm giá, nên mua nha",
            "media_image": "",
            "date": "2026-09-03 11:45:10",
        },
    ]

    print(f"\n[1] Đang xử lý {len(scraped_reviews)} đánh giá...")

    # 2. Xuất dữ liệu bảng ra file CSV chuẩn UTF-8 BOM
    csv_filename = f"{job_id}_reviews.csv"
    csv_result = _save_csv(scraped_reviews, filename=csv_filename)
    print(f" -> Đã lưu file CSV: {csv_result.get('path')}")
    print(f" -> Link tải CSV:   {csv_result.get('url')}")

    # 3. Tạo thư mục media riêng biệt để tải hình ảnh/video về
    media_folder = _output_subdir("media") / job_id
    media_folder.mkdir(parents=True, exist_ok=True)

    # Tạo mẫu ảnh và video trong folder media
    (media_folder / "image_review_001.jpg").write_bytes(b"[MOCK_IMAGE_DATA_1: San pham dep]")
    (media_folder / "image_review_002.png").write_bytes(b"[MOCK_IMAGE_DATA_2: Hop dong goi]")
    (media_folder / "video_review_002_unboxing.mp4").write_bytes(b"[MOCK_VIDEO_DATA: Video mo hop 1080p 60fps]")

    # Đính kèm luôn file README / manifest vào folder
    (media_folder / "manifest.txt").write_text(
        f"Gói media cho sản phẩm {job_id}\nBao gồm 2 hình ảnh và 1 video đánh giá mở hộp.\n",
        encoding="utf-8"
    )

    print(f"\n[2] Đã thu thập media vào thư mục: {media_folder}")

    # 4. Tự động nén thư mục media thành file .zip
    zip_filename = f"{job_id}_media.zip"
    zip_result = _zip_folder(str(media_folder), zip_filename=zip_filename)
    print(f" -> Đã nén thành công file .ZIP: {zip_result.get('zip_path')}")
    print(f" -> Tổng số file bên trong:     {zip_result.get('file_count')}")
    print(f" -> Link tải trực tiếp .ZIP:    {zip_result.get('zip_url')}")

    # 5. Mẫu báo cáo gửi cho người dùng
    print("\n" + "=" * 70)
    print("KẾT QUẢ BÁO CHO USER:")
    print("=" * 70)
    report_message = f"""\
Đã cào xong dữ liệu sản phẩm `{job_id}`:
- **Tổng số đánh giá**: {len(scraped_reviews)}
- **Đánh giá trung bình**: 4.7 sao
- **Hình ảnh & Video**: 3 file đa phương tiện đã tải về đầy đủ

Link tải một chạm dành cho bạn:
- 📄 [Tải file CSV đánh giá]({csv_result.get('url')})
- 📦 [Tải toàn bộ hình ảnh & video .ZIP]({zip_result.get('zip_url')})
"""
    print(report_message)


if __name__ == "__main__":
    run_demo()
