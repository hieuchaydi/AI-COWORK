"""Unit tests for media crawling and zip archiving tools (zip_folder & download_media_and_zip)."""

from __future__ import annotations

import sys
import hashlib
import json
import zipfile
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from coworker.tools.crawl import _download_media_and_zip, _zip_folder, make_crawl_tools


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


def test_download_media_and_zip_deduplicates_by_hash(tmp_path, monkeypatch):
    """Verify that _download_media_and_zip calculates SHA-256 hashes, deduplicates
    identical files across different URLs (keeping only 1 copy), records duplicate_of
    in manifest.json, and produces a ZIP with only unique files + manifest."""
    content_dup = b"\xff\xd8\xff\xe0\x00\x10JFIF_identical_binary_image_content_12345"
    content_uniq = b"\x89PNG\r\n\x1a\n_different_binary_image_content_67890"

    sha_dup = hashlib.sha256(content_dup).hexdigest()
    sha_uniq = hashlib.sha256(content_uniq).hexdigest()

    u1 = "https://cdn1.example.com/photos/item_alpha.jpg"
    u2 = "https://mirror2.example.com/photos/item_alpha_duplicate.jpg"
    u3 = "https://cdn1.example.com/photos/item_beta_unique.png"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "item_alpha" in url_str:
            return httpx.Response(200, content=content_dup, headers={"content-type": "image/jpeg"})
        elif "item_beta" in url_str:
            return httpx.Response(200, content=content_uniq, headers={"content-type": "image/png"})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("coworker.tools.crawl._client", lambda: mock_client)

    res = _download_media_and_zip(
        urls=[u1, u2, u3],
        zip_filename="test_dedup_bundle.zip",
        folder_name="test_dedup_job",
        output_dir=str(tmp_path),
    )

    # 1. Check returned metadata
    assert res.get("ok") is True
    assert res["downloaded_count"] == 2
    assert res["unique_count"] == 2
    assert res["duplicate_count"] == 1
    assert res["failed_count"] == 0
    # ZIP contains 2 unique media files + 1 manifest.json = 3 files
    assert res["file_count"] == 3

    # 2. Check files on disk in media_folder
    media_dir = Path(res["media_folder"])
    assert media_dir.exists()
    disk_files = {p.name for p in media_dir.iterdir() if p.is_file()}
    assert "manifest.json" in disk_files
    # Only 2 unique media files + 1 manifest on disk (duplicate file was unlinked)
    assert len(disk_files) == 3

    # 3. Check manifest.json contents
    manifest_path = Path(res["manifest_path"])
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest_data["total_urls"] == 3
    assert manifest_data["unique_count"] == 2
    assert manifest_data["duplicate_count"] == 1
    assert len(manifest_data["files"]) == 3

    file_0 = manifest_data["files"][0]
    file_1 = manifest_data["files"][1]
    file_2 = manifest_data["files"][2]

    # Primary file (kept)
    assert file_0["url"] == u1
    assert file_0["sha256"] == sha_dup
    assert file_0["duplicate_of"] is None
    primary_name = file_0["filename"]
    assert (media_dir / primary_name).exists()

    # Duplicate file (different URL, identical sha256 -> deleted, duplicate_of set)
    assert file_1["url"] == u2
    assert file_1["sha256"] == sha_dup
    assert file_1["duplicate_of"] == primary_name
    assert not (media_dir / file_1["filename"]).exists()

    # Unique second file (kept)
    assert file_2["url"] == u3
    assert file_2["sha256"] == sha_uniq
    assert file_2["duplicate_of"] is None
    assert (media_dir / file_2["filename"]).exists()

    # 4. Check ZIP archive contents: must only contain unique files + manifest.json
    zip_path = Path(res["zip_path"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert primary_name in namelist
        assert file_2["filename"] in namelist
        # Duplicate file must NOT be inside the ZIP
        assert file_1["filename"] not in namelist
        assert len(namelist) == 3

        # Validate manifest inside ZIP matches disk manifest
        zip_manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert zip_manifest["duplicate_count"] == 1
        assert zip_manifest["files"][1]["duplicate_of"] == primary_name


def test_download_media_and_zip_split_when_exceeding_max_zip_mb(tmp_path, monkeypatch):
    """Verify that _download_media_and_zip splits media into multiple ZIP parts
    (<job_name>_media_part01.zip, part02.zip...) when total size exceeds max_zip_mb,
    and returns zip_urls as a list of all download links."""
    data_1 = b"A" * (600 * 1024)
    data_2 = b"B" * (600 * 1024)

    u1 = "https://example.com/large_image_01.jpg"
    u2 = "https://example.com/large_image_02.png"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "large_image_01" in url_str:
            return httpx.Response(200, content=data_1, headers={"content-type": "image/jpeg"})
        elif "large_image_02" in url_str:
            return httpx.Response(200, content=data_2, headers={"content-type": "image/png"})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("coworker.tools.crawl._client", lambda: mock_client)

    res = _download_media_and_zip(
        urls=[u1, u2],
        folder_name="large_media_job",
        output_dir=str(tmp_path),
        max_zip_mb=1,  # 1 MB threshold -> should split into 2 parts
    )

    assert res.get("ok") is True
    assert res["part_count"] == 2
    assert isinstance(res["zip_urls"], list)
    assert len(res["zip_urls"]) == 2
    assert isinstance(res["zip_paths"], list)
    assert len(res["zip_paths"]) == 2

    # Check naming conventions: <job_name>_media_part01.zip, part02.zip...
    assert res["zip_urls"][0].endswith("/large_media_job_media_part01.zip")
    assert res["zip_urls"][1].endswith("/large_media_job_media_part02.zip")

    # Verify each part on disk
    part1_path = Path(res["zip_paths"][0])
    part2_path = Path(res["zip_paths"][1])

    assert part1_path.exists()
    assert part2_path.exists()
    assert zipfile.is_zipfile(part1_path)
    assert zipfile.is_zipfile(part2_path)

    with zipfile.ZipFile(part1_path, "r") as zf1:
        names1 = zf1.namelist()
        assert "manifest.json" in names1
        assert any("large_image_01" in n for n in names1)
        assert not any("large_image_02" in n for n in names1)

    with zipfile.ZipFile(part2_path, "r") as zf2:
        names2 = zf2.namelist()
        assert "manifest.json" in names2
        assert any("large_image_02" in n for n in names2)
        assert not any("large_image_01" in n for n in names2)


def test_download_media_and_zip_resume_skips_existing_files(tmp_path, monkeypatch):
    """Verify that _download_media_and_zip skips downloading existing files and reuses
    manifest status on resume, only re-attempting missing/failed files."""
    u1 = "https://example.com/item1.jpg"
    u2 = "https://example.com/item2.jpg"
    data_1 = b"Item 1 image content"
    data_2 = b"Item 2 image content"

    requested_urls: list[str] = []
    run2_mode = False

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested_urls.append(url_str)
        if "item1" in url_str:
            return httpx.Response(200, content=data_1, headers={"content-type": "image/jpeg"})
        elif "item2" in url_str:
            if not run2_mode:
                # First run fails on item2
                return httpx.Response(500, content=b"Internal Server Error")
            # Second run succeeds on item2
            return httpx.Response(200, content=data_2, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("coworker.tools.crawl._client", lambda: httpx.Client(transport=transport))

    # ── Run 1: u1 succeeds, u2 fails ──────────────────────────────────────────
    res1 = _download_media_and_zip(
        urls=[u1, u2],
        folder_name="resume_test_job",
        output_dir=str(tmp_path),
    )
    assert res1.get("ok") is True
    assert res1["downloaded_count"] == 1
    assert res1["skipped_count"] == 0
    assert res1["failed_count"] == 1
    assert res1["unique_count"] == 1
    assert len(res1["errors"]) == 1

    # Verify u1 is on disk and was requested
    assert u1 in requested_urls
    assert u2 in requested_urls

    # Reset request tracker for Run 2
    requested_urls.clear()
    run2_mode = True

    # ── Run 2: Resume ─────────────────────────────────────────────────────────
    # u1 already exists on disk and in manifest.json -> must be SKIPPED without HTTP request
    # u2 failed in Run 1 -> must be downloaded
    res2 = _download_media_and_zip(
        urls=[u1, u2],
        folder_name="resume_test_job",
        output_dir=str(tmp_path),
    )
    assert res2.get("ok") is True
    assert res2["downloaded_count"] == 1  # Only u2 was downloaded
    assert res2["skipped_count"] == 1     # u1 was skipped/reused
    assert res2["failed_count"] == 0
    assert res2["unique_count"] == 2

    # CRITICAL: u1 was NOT requested via HTTP during resume!
    assert u1 not in requested_urls
    assert u2 in requested_urls

    # Check that final ZIP contains both files + manifest
    zip_path = Path(res2["zip_path"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert any("item1" in n for n in namelist)
        assert any("item2" in n for n in namelist)

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["downloaded_count"] == 1
        assert manifest["skipped_count"] == 1
        assert manifest["failed_count"] == 0
        assert manifest["unique_count"] == 2


def test_download_media_and_zip_resume_from_disk_without_manifest(tmp_path, monkeypatch):
    """Verify that even without a manifest.json, pre-existing media files on disk with size > 0
    are recognized, hashed, and skipped from being downloaded again."""
    u1 = "https://example.com/disk_item1.jpg"
    u2 = "https://example.com/disk_item2.jpg"
    data_1 = b"Disk item 1 content"
    data_2 = b"Disk item 2 content"

    # Pre-populate disk file for u1 (001_disk_item1.jpg)
    job_dir = tmp_path / "media" / "disk_resume_job"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "001_disk_item1.jpg").write_bytes(data_1)

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested.append(url_str)
        if "disk_item2" in url_str:
            return httpx.Response(200, content=data_2, headers={"content-type": "image/jpeg"})
        elif "disk_item1" in url_str:
            return httpx.Response(200, content=data_1, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    monkeypatch.setattr("coworker.tools.crawl._client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    res = _download_media_and_zip(
        urls=[u1, u2],
        folder_name="disk_resume_job",
        output_dir=str(tmp_path),
    )
    assert res.get("ok") is True
    assert res["skipped_count"] == 1      # Pre-existing file skipped
    assert res["downloaded_count"] == 1   # Only u2 downloaded
    assert res["failed_count"] == 0
    assert res["unique_count"] == 2
    assert u1 not in requested
    assert u2 in requested


def test_download_media_and_zip_custom_output_dir(tmp_path, monkeypatch):
    """Verify that download_media_and_zip writes media and zips to custom output_dir
    with is_in_outputs=False."""
    u1 = "https://example.com/custom_photo.jpg"
    data_1 = b"Custom photo payload"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data_1, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("coworker.tools.crawl._client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    custom_dir = tmp_path / "custom_download_store"
    res = _download_media_and_zip(
        urls=[u1],
        folder_name="custom_photo_job",
        output_dir=str(custom_dir),
    )
    assert res.get("ok") is True
    assert res["is_in_outputs"] is False

    media_dir = Path(res["media_folder"])
    assert media_dir.exists()
    assert media_dir.parent.resolve() == (custom_dir / "media").resolve()

    zip_file = Path(res["zip_path"])
    assert zip_file.exists()
    assert zip_file.parent.resolve() == (custom_dir / "zips").resolve()


def test_download_media_and_zip_fallback_default_output_dir(monkeypatch):
    """Verify that omitting output_dir falls back to project outputs/ with is_in_outputs=True."""
    from coworker.tools.crawl import _output_root
    u1 = "https://example.com/default_photo.jpg"
    data_1 = b"Default photo payload"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data_1, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("coworker.tools.crawl._client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))

    job_name = "default_dl_job_test"
    res = _download_media_and_zip(
        urls=[u1],
        folder_name=job_name,
        output_dir="",
    )
    try:
        assert res.get("ok") is True
        assert res["is_in_outputs"] is True

        out_root = _output_root()
        media_dir = Path(res["media_folder"])
        assert media_dir.exists()
        assert media_dir.parent.resolve() == (out_root / "media").resolve()

        zip_file = Path(res["zip_path"])
        assert zip_file.exists()
        assert zip_file.parent.resolve() == (out_root / "zips").resolve()
    finally:
        # Cleanup created files in project outputs/
        import shutil
        shutil.rmtree(res["media_folder"], ignore_errors=True)
        Path(res["zip_path"]).unlink(missing_ok=True)





