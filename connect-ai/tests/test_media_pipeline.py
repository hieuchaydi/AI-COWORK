"""Unit tests for media_pipeline.crawl_and_export_bundle."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.crawl import make_crawl_tools
from coworker.tools.media_pipeline import crawl_and_export_bundle, download_media_from_csv
from coworker.tools.router import categorize_tool, ToolCategory


def test_crawl_and_export_bundle_no_media(tmp_path):
    """Test bundle export when rows contain no media URLs."""
    rows = [
        {"name": "Item A", "price": 100},
        {"name": "Item B", "price": 200},
    ]
    res = crawl_and_export_bundle(rows, job_name="test_no_media")
    assert res.get("ok") is True
    assert res["csv"]["row_count"] == 2
    assert res["zip"] is None

    # Cleanup CSV
    Path(res["csv"]["path"]).unlink(missing_ok=True)


def test_crawl_and_export_bundle_extracts_media_from_rows(tmp_path, monkeypatch):
    """Test bundle export extracting media from row columns."""
    rows = [
        {
            "id": 1,
            "title": "Review 1",
            "img": "https://example.com/photo1.jpg",
            "vid": "https://example.com/clip1.mp4",
        },
        {
            "id": 2,
            "title": "Review 2",
            "img": "https://example.com/photo2.png",
        },
    ]

    # Mock _download_media_and_zip to avoid actual network requests in unit test
    def mock_download_and_zip(urls, zip_filename, folder_name, **kwargs):
        assert len(urls) == 3
        return {
            "ok": True,
            "zip_path": f"/tmp/{zip_filename}",
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": len(urls),
            "zip_size_mb": 1.2,
            "media_folder": f"/tmp/{folder_name}",
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="test_with_media")
    assert res.get("ok") is True
    assert res["csv"]["row_count"] == 2
    assert res["zip"] is not None
    assert res["zip"]["file_count"] == 3
    assert "test_with_media_media.zip" in res["zip"]["filename"]

    # Cleanup CSV
    Path(res["csv"]["path"]).unlink(missing_ok=True)


def test_crawl_and_export_bundle_registered_as_crawl_tool(monkeypatch):
    """The one-shot CSV + media ZIP pipeline is exposed to agents as a crawl tool."""
    rows = [
        {
            "id": 1,
            "title": "Review 1",
            "image": "https://example.com/photo1.jpg",
            "video": "https://example.com/clip1.mp4",
        }
    ]

    def mock_download_and_zip(urls, zip_filename, folder_name, **kwargs):
        assert urls == ["https://example.com/photo1.jpg", "https://example.com/clip1.mp4"]
        return {
            "ok": True,
            "zip_path": f"/tmp/{zip_filename}",
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": len(urls),
            "zip_size_mb": 0.2,
            "media_folder": f"/tmp/{folder_name}",
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)
    tools = {t.__name__: t for t in make_crawl_tools()}

    assert "crawl_and_export_bundle" in tools

    res = tools["crawl_and_export_bundle"](rows=rows, job_name="registered_bundle")
    assert res["csv"]["row_count"] == 1
    assert res["zip"]["file_count"] == 2
    assert res["zip"]["url"].endswith("/registered_bundle_media.zip")

    Path(res["csv"]["path"]).unlink(missing_ok=True)


def test_crawl_and_export_bundle_custom_output_dir(tmp_path, monkeypatch):
    """Verify that specifying custom output_dir routes both CSV and ZIP to that folder."""
    custom_dir = tmp_path / "custom_workspace_export"
    rows = [{"title": "Item 1", "img": "https://example.com/test.jpg"}]

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        assert str(custom_dir) in output_dir
        zip_path = Path(output_dir) / "zips" / zip_filename
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"mock_zip")
        return {
            "ok": True,
            "zip_path": str(zip_path),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": 1,
            "zip_size_mb": 0.1,
            "media_folder": str(Path(output_dir) / "media" / folder_name),
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="custom_job", output_dir=str(custom_dir))
    assert res.get("ok") is True
    assert str(custom_dir) in res["csv"]["path"]
    assert Path(res["csv"]["path"]).exists()
    assert str(custom_dir) in res["zip"]["path"]
    assert Path(res["zip"]["path"]).exists()


def test_crawl_and_export_bundle_forwards_manifest_and_dedup_metadata(monkeypatch):
    """Verify that crawl_and_export_bundle exposes unique_count, duplicate_count, and manifest."""
    rows = [
        {"title": "Item 1", "img": "https://example.com/img1.jpg"},
        {"title": "Item 2", "img": "https://example.com/img2_dup.jpg"},
    ]

    mock_manifest = {
        "job_name": "test_job",
        "unique_count": 1,
        "duplicate_count": 1,
        "files": [
            {"url": "https://example.com/img1.jpg", "duplicate_of": None},
            {"url": "https://example.com/img2_dup.jpg", "duplicate_of": "001_img1.jpg"},
        ],
    }

    def mock_download_and_zip(urls, zip_filename, folder_name, **kwargs):
        return {
            "ok": True,
            "zip_path": f"/tmp/{zip_filename}",
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": 2,
            "zip_size_mb": 0.5,
            "media_folder": f"/tmp/{folder_name}",
            "unique_count": 1,
            "duplicate_count": 1,
            "manifest_path": f"/tmp/{folder_name}/manifest.json",
            "manifest": mock_manifest,
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="dedup_bundle_job")
    assert res.get("ok") is True
    assert res["zip"] is not None
    assert res["zip"]["unique_count"] == 1
    assert res["zip"]["duplicate_count"] == 1
    assert res["zip"]["manifest_path"] == "/tmp/dedup_bundle_job/manifest.json"
    assert res["zip"]["manifest"] == mock_manifest

    Path(res["csv"]["path"]).unlink(missing_ok=True)


def test_crawl_and_export_bundle_supports_split_zip_urls(monkeypatch):
    """Verify that crawl_and_export_bundle forwards max_zip_mb and returns zip_urls list."""
    rows = [
        {"title": "Item 1", "img": "https://example.com/item1.jpg"},
        {"title": "Item 2", "img": "https://example.com/item2.png"},
    ]

    captured_kwargs = {}

    def mock_download_and_zip(urls, zip_filename, folder_name, **kwargs):
        captured_kwargs.update(kwargs)
        urls_list = [
            f"http://localhost:8766/outputs/zips/{folder_name}_media_part01.zip",
            f"http://localhost:8766/outputs/zips/{folder_name}_media_part02.zip",
        ]
        return {
            "ok": True,
            "zip_path": f"/tmp/{folder_name}_media_part01.zip",
            "zip_url": urls_list[0],
            "zip_urls": urls_list,
            "part_count": 2,
            "file_count": 3,
            "zip_size_mb": 1.8,
            "media_folder": f"/tmp/{folder_name}",
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="split_job", max_zip_mb=5)
    assert res.get("ok") is True
    assert captured_kwargs.get("max_zip_mb") == 5

    assert res["zip"] is not None
    assert res["zip"]["part_count"] == 2
    assert isinstance(res["zip"]["zip_urls"], list)
    assert len(res["zip"]["zip_urls"]) == 2
    assert res["zip_urls"] == res["zip"]["zip_urls"]

    Path(res["csv"]["path"]).unlink(missing_ok=True)


def test_crawl_and_export_bundle_generates_markdown_report(tmp_path, monkeypatch):
    """Verify that crawl_and_export_bundle creates outputs/text/<job_name>_report.md
    with summary stats, CSV link, ZIP link, top errors, and returns report.url."""
    rows = [
        {"id": 1, "title": "Good Item", "image": "https://example.com/good.jpg"},
        {"id": 2, "title": "Bad Item", "image": "https://example.com/bad.jpg"},
    ]

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        return {
            "ok": True,
            "zip_path": f"{output_dir}/zips/{zip_filename}",
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "zip_urls": [f"http://localhost:8766/outputs/zips/{zip_filename}"],
            "part_count": 1,
            "file_count": 1,
            "unique_count": 1,
            "duplicate_count": 0,
            "failed_count": 1,
            "errors": [{"url": "https://example.com/bad.jpg", "error": "404 Not Found"}],
            "zip_size_mb": 0.3,
            "media_folder": f"{output_dir}/media/{folder_name}",
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="demo_report_job", output_dir=str(tmp_path))
    assert res.get("ok") is True

    # 1. Verify result contains report dict with url and report.url dot-access
    assert "report" in res
    assert "report_url" in res
    assert res["report"]["url"].endswith("/demo_report_job_report.md")
    assert res["report"].url == res["report"]["url"]
    assert "http://localhost:8766/outputs/text/" in res["report"]["url"]

    # 2. Verify file on disk
    report_file = Path(res["report"]["path"])
    assert report_file.exists()
    content = report_file.read_text(encoding="utf-8")

    # 3. Verify report sections: rows, media count, success/failed, links, top errors
    assert "Crawl Report: demo_report_job" in content
    assert "**Tổng số dòng (rows)**: 2" in content
    assert "**Số media tìm thấy**: 2" in content
    assert "**Số tải thành công**: 1" in content
    assert "**Số tải thất bại**: 1" in content
    assert res["csv"]["url"] in content
    assert res["zip"]["url"] in content
    assert "https://example.com/bad.jpg" in content
    assert "404 Not Found" in content


def test_crawl_and_export_bundle_custom_output_dir(tmp_path, monkeypatch):
    """Verify that specifying a custom output_dir stores CSV, media, ZIP and report
    in the designated custom folder, with is_in_outputs=False and accurate paths."""
    rows = [{"title": "Item Custom", "img": "https://example.com/custom.png"}]
    custom_target = tmp_path / "my_custom_crawl"

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        zpath = Path(output_dir) / "zips" / zip_filename
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(b"PK0304mockzip")
        return {
            "ok": True,
            "zip_path": str(zpath),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "zip_urls": [f"http://localhost:8766/outputs/zips/{zip_filename}"],
            "part_count": 1,
            "file_count": 1,
            "unique_count": 1,
            "duplicate_count": 0,
            "failed_count": 0,
            "media_folder": str(Path(output_dir) / "media" / folder_name),
            "is_in_outputs": False,
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name="custom_bundle_job", output_dir=str(custom_target))
    assert res.get("ok") is True
    assert res["is_in_outputs"] is False
    assert res["output_dir"] == str(custom_target.resolve())

    # CSV path inside custom folder
    csv_path = Path(res["csv"]["path"])
    assert csv_path.exists()
    assert csv_path.parent.resolve() == (custom_target / "csv").resolve()
    assert res["csv"]["is_in_outputs"] is False

    # ZIP path inside custom folder
    zip_path = Path(res["zip"]["path"])
    assert zip_path.exists()
    assert zip_path.parent.resolve() == (custom_target / "zips").resolve()
    assert res["zip"]["is_in_outputs"] is False

    # Report path inside custom folder
    report_path = Path(res["report"]["path"])
    assert report_path.exists()
    assert report_path.parent.resolve() == (custom_target / "text").resolve()
    assert res["report"]["is_in_outputs"] is False

    report_text = report_path.read_text(encoding="utf-8")
    assert str(custom_target.resolve()) in report_text
    assert str(csv_path) in report_text


def test_crawl_and_export_bundle_fallback_default_output_dir(monkeypatch):
    """Verify that omitting output_dir (or passing empty string) falls back to outputs/
    in project root with is_in_outputs=True."""
    from coworker.tools.crawl import _output_root

    rows = [{"title": "Item Default", "img": "https://example.com/default.png"}]
    job_name = "fallback_default_job"

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        out_root = _output_root()
        zpath = out_root / "zips" / zip_filename
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(b"PK0304mockzip")
        return {
            "ok": True,
            "zip_path": str(zpath),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "zip_urls": [f"http://localhost:8766/outputs/zips/{zip_filename}"],
            "part_count": 1,
            "file_count": 1,
            "unique_count": 1,
            "duplicate_count": 0,
            "failed_count": 0,
            "media_folder": str(out_root / "media" / folder_name),
            "is_in_outputs": True,
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = crawl_and_export_bundle(rows, job_name=job_name, output_dir="")
    try:
        assert res.get("ok") is True
        assert res["is_in_outputs"] is True
        assert res["csv"]["is_in_outputs"] is True
        assert res["zip"]["is_in_outputs"] is True
        assert res["report"]["is_in_outputs"] is True

        out_root = _output_root()
        assert Path(res["csv"]["path"]).exists()
        assert Path(res["csv"]["path"]).parent.resolve() == (out_root / "csv").resolve()

        assert Path(res["zip"]["path"]).exists()
        assert Path(res["zip"]["path"]).parent.resolve() == (out_root / "zips").resolve()

        assert Path(res["report"]["path"]).exists()
        assert Path(res["report"]["path"]).parent.resolve() == (out_root / "text").resolve()
    finally:
        # Cleanup project outputs/ created by this test
        Path(res["csv"]["path"]).unlink(missing_ok=True)
        Path(res["zip"]["path"]).unlink(missing_ok=True)
        Path(res["report"]["path"]).unlink(missing_ok=True)


def test_download_media_from_csv_explicit_columns(tmp_path, monkeypatch):
    """Verify download_media_from_csv extracts media URLs from specified columns,
    packages ZIP, generates report, and returns metadata."""
    csv_file = tmp_path / "products.csv"
    csv_content = (
        "\ufeffsku,title,image_url,video_url,notes\n"
        "SKU01,Laptop,https://example.com/laptop.jpg,https://example.com/laptop_demo.mp4,Note 1\n"
        "SKU02,Mouse,https://example.com/mouse.png,,Note 2\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    captured = {}

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        captured["urls"] = urls
        captured["folder_name"] = folder_name
        zpath = Path(output_dir) / "zips" / zip_filename
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(b"PK0304mockzip")
        return {
            "ok": True,
            "zip_path": str(zpath),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "zip_urls": [f"http://localhost:8766/outputs/zips/{zip_filename}"],
            "part_count": 1,
            "file_count": len(urls),
            "unique_count": len(urls),
            "duplicate_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "media_folder": str(Path(output_dir) / "media" / folder_name),
            "manifest_path": str(Path(output_dir) / "media" / folder_name / "manifest.json"),
            "manifest": {"job_name": folder_name, "files": []},
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = download_media_from_csv(
        csv_path=str(csv_file),
        url_columns=["image_url", "video_url"],
        job_name="products_export",
        output_dir=str(tmp_path),
    )

    assert res.get("ok") is True
    assert res["job_name"] == "products_export"
    assert res["row_count"] == 2
    assert res["media_found"] == 3
    assert captured["urls"] == [
        "https://example.com/laptop.jpg",
        "https://example.com/laptop_demo.mp4",
        "https://example.com/mouse.png",
    ]
    assert res["zip"]["file_count"] == 3
    assert "products_export_media.zip" in res["zip"]["filename"]
    assert Path(res["zip"]["path"]).exists()

    # Report verification
    report_file = Path(res["report"]["path"])
    assert report_file.exists()
    report_text = report_file.read_text(encoding="utf-8")
    assert "Media Extraction Report: products_export" in report_text
    assert "**Tổng số dòng CSV**: 2" in report_text
    assert "**Số media URL tìm thấy**: 3" in report_text


def test_download_media_from_csv_autodetect_columns(tmp_path, monkeypatch):
    """Verify download_media_from_csv automatically detects columns with media URLs."""
    csv_file = tmp_path / "reviews.csv"
    csv_content = (
        "user,comment,anh_urls,rating\n"
        "Alice,Tuyet voi,https://example.com/a1.jpg,5\n"
        "Bob,Tot,https://example.com/b1.png,4\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    captured_urls = []

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        captured_urls.extend(urls)
        zpath = Path(output_dir) / "zips" / zip_filename
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(b"PK0304mockzip")
        return {
            "ok": True,
            "zip_path": str(zpath),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": len(urls),
            "unique_count": len(urls),
            "duplicate_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "media_folder": str(Path(output_dir) / "media" / folder_name),
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = download_media_from_csv(csv_path=str(csv_file), output_dir=str(tmp_path))
    assert res.get("ok") is True
    assert res["media_found"] == 2
    assert captured_urls == ["https://example.com/a1.jpg", "https://example.com/b1.png"]


def test_download_media_from_csv_handles_pipe_and_json_urls(tmp_path, monkeypatch):
    """Verify multi-URL cells (pipe-separated and JSON arrays) are split and deduplicated."""
    csv_file = tmp_path / "multi.csv"
    csv_content = (
        "id,gallery\n"
        "1,\"https://example.com/pic1.jpg|https://example.com/pic2.png\"\n"
        "2,\"[\"\"https://example.com/pic2.png\"\", \"\"https://example.com/pic3.webp\"\"]\"\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    captured_urls = []

    def mock_download_and_zip(urls, zip_filename, folder_name, output_dir="", **kwargs):
        captured_urls.extend(urls)
        zpath = Path(output_dir) / "zips" / zip_filename
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(b"PK0304mockzip")
        return {
            "ok": True,
            "zip_path": str(zpath),
            "zip_url": f"http://localhost:8766/outputs/zips/{zip_filename}",
            "file_count": len(urls),
            "unique_count": len(urls),
            "duplicate_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "media_folder": str(Path(output_dir) / "media" / folder_name),
        }

    monkeypatch.setattr("coworker.tools.media_pipeline._download_media_and_zip", mock_download_and_zip)

    res = download_media_from_csv(csv_path=str(csv_file), output_dir=str(tmp_path))
    assert res.get("ok") is True
    # pic2.png was duplicated across row 1 and row 2 -> deduplicated to 3 unique URLs
    assert res["media_found"] == 3
    assert captured_urls == [
        "https://example.com/pic1.jpg",
        "https://example.com/pic2.png",
        "https://example.com/pic3.webp",
    ]


def test_download_media_from_csv_registered_as_crawl_tool():
    """Verify that download_media_from_csv is registered in make_crawl_tools and router."""
    tools = {t.__name__: t for t in make_crawl_tools()}
    assert "download_media_from_csv" in tools
    assert categorize_tool("download_media_from_csv") == ToolCategory.CRAWL


def test_download_media_from_csv_error_cases(tmp_path):
    """Verify error handling for nonexistent files, empty files, or missing media."""
    # 1. Nonexistent file
    res1 = download_media_from_csv("nonexistent_path_123.csv")
    assert res1.get("ok") is False
    assert "CSV file not found" in res1["error"]

    # 2. Empty CSV
    empty_file = tmp_path / "empty.csv"
    empty_file.write_text("", encoding="utf-8")
    res2 = download_media_from_csv(str(empty_file))
    assert res2.get("ok") is False
    assert "Failed to read CSV" in res2["error"] or "has no headers" in res2["error"]

    # 3. CSV with no media URLs
    no_media_file = tmp_path / "no_media.csv"
    no_media_file.write_text("name,price\nItem A,100\nItem B,200\n", encoding="utf-8")
    res3 = download_media_from_csv(str(no_media_file))
    assert res3.get("ok") is False
    assert "No image or video URLs found" in res3["error"]

    # 4. Invalid specified columns
    res4 = download_media_from_csv(str(no_media_file), url_columns=["nonexistent_col"])
    assert res4.get("ok") is False
    assert "None of the specified url_columns" in res4["error"]




