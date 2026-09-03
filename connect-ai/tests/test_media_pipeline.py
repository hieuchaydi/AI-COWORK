"""Unit tests for media_pipeline.crawl_and_export_bundle."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.crawl import make_crawl_tools
from coworker.tools.media_pipeline import crawl_and_export_bundle


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


