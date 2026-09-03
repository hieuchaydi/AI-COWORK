"""Unit tests for media_pipeline.crawl_and_export_bundle."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
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
