"""Unit tests for media crawling and zip archiving tools (zip_folder & download_media_and_zip)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.crawl import _zip_folder, make_crawl_tools


def test_crawl_tools_factory_includes_zip_tools():
    """Verify that make_crawl_tools includes zip_folder and download_media_and_zip."""
    tools = make_crawl_tools()
    names = [t.__name__ for t in tools]
    assert "zip_folder" in names
    assert "download_media_and_zip" in names


def test_zip_folder_creates_valid_archive(tmp_path):
    """Verify that _zip_folder compresses an arbitrary folder into outputs/zips/."""
    # 1. Prepare sample folder with mock media files
    sample_dir = tmp_path / "scraped_product_123"
    sample_dir.mkdir()
    (sample_dir / "image_001.jpg").write_bytes(b"mock_jpg_data_12345")
    (sample_dir / "image_002.png").write_bytes(b"mock_png_data_67890")
    (sample_dir / "video_001.mp4").write_bytes(b"mock_mp4_data_abcde")
    (sample_dir / "reviews.csv").write_text("user,rating,comment\nAlice,5,Great product!", encoding="utf-8")

    # 2. Call _zip_folder
    res = _zip_folder(str(sample_dir), zip_filename="product_123_media.zip")

    assert res.get("ok") is True
    assert "zip_path" in res
    assert "zip_url" in res
    assert res["file_count"] == 4
    assert res["filename"] == "product_123_media.zip"
    assert "/outputs/zips/product_123_media.zip" in res["zip_url"]

    # 3. Validate actual zip file on disk
    zip_path = Path(res["zip_path"])
    assert zip_path.exists()
    assert zipfile.is_zipfile(zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "image_001.jpg" in namelist
        assert "image_002.png" in namelist
        assert "video_001.mp4" in namelist
        assert "reviews.csv" in namelist
        assert zf.read("image_001.jpg") == b"mock_jpg_data_12345"

    # Cleanup test zip
    zip_path.unlink(missing_ok=True)


def test_zip_folder_nonexistent():
    """Verify error handling when directory does not exist."""
    res = _zip_folder("C:/path/to/nonexistent/folder/12345")
    assert "error" in res
