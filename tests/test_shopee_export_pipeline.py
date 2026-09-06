"""Test suite auditing the entire Shopee review export pipeline end-to-end.

Covers:
1. UTF-8 BOM CSV with Excel compatibility.
2. Full review columns preservation and integrity.
3. Media downloading to outputs/media/<stem>/.
4. SHA-256 deduplication for both image and video while preserving review-media relations.
5. manifest.json schema with URL, local_file, type, hash, size, duplicate_of, error.
6. ZIP packaging containing CSV, media files, and manifest.json.
7. Multipart ZIP splitting when exceeding max_zip_mb.
8. Accurate Markdown statistics report generation.
9. Localhost HTTP serving of all generated outputs (CSV, ZIP, manifest, report).
10. Edge cases: duplicated images, duplicated videos, failed URLs, and reviews without media.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launch


@pytest.fixture()
def export_server(tmp_path, monkeypatch):
    """Helper bound to an ephemeral port writing into tmp_path."""
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), launch._HelperHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    finally:
        srv.shutdown()


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def _get_raw(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.headers, r.read()


def _post(base: str, path: str, body) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_shopee_export_pipeline_full_audit(export_server, monkeypatch):
    """Test full pipeline with duplicated images, duplicated videos, broken URLs, and no-media reviews."""
    base, outputs = export_server

    # Setup distinct payloads
    img_content_a = b"PNG_IMAGE_A_RAW_BYTES_12345"
    img_content_unique = b"PNG_IMAGE_UNIQUE_RAW_BYTES_67890"
    vid_content_a = b"MP4_VIDEO_A_RAW_BYTES_11111"

    img_sha_a = hashlib.sha256(img_content_a).hexdigest()
    img_sha_unique = hashlib.sha256(img_content_unique).hexdigest()
    vid_sha_a = hashlib.sha256(vid_content_a).hexdigest()

    def fake_download_media(url: str, target_without_ext: Path) -> str:
        if "broken" in url or "fail" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"Not Found"))
        if "vid" in url:
            target = target_without_ext.with_suffix(".mp4")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(vid_content_a)
        else:
            target = target_without_ext.with_suffix(".jpg")
            target.parent.mkdir(parents=True, exist_ok=True)
            if "pic_dup" in url or "pic1" in url:
                target.write_bytes(img_content_a)
            else:
                target.write_bytes(img_content_unique)
        return f"outputs/{target.relative_to(outputs).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download_media)

    test_rows = [
        {
            "user": "khach_01",
            "sao": 5,
            "noi_dung": "Đánh giá 1: hàng đẹp, có ảnh và video",
            "phan_loai": "Màu Đen, Size L",
            "thoi_gian": "2026-09-06 10:00:00",
            "anh_urls": "https://down-vn.img.susercontent.com/file/pic1|https://down-vn.img.susercontent.com/file/pic_dup",
            "video_urls": "https://deo.shopeemobile.com/file/vid1",
            "huu_ich": 10,
        },
        {
            "user": "khach_02",
            "sao": 4,
            "noi_dung": "Đánh giá 2: video trùng và có ảnh lỗi",
            "phan_loai": "Màu Trắng, Size M",
            "thoi_gian": "2026-09-05 09:00:00",
            "anh_urls": "https://down-vn.img.susercontent.com/file/broken_image_url",
            "video_urls": "https://deo.shopeemobile.com/file/vid_dup",
            "huu_ich": 2,
        },
        {
            "user": "khach_03",
            "sao": 3,
            "noi_dung": "Đánh giá 3: hoàn toàn không có ảnh hay video",
            "phan_loai": "Màu Xanh, Size S",
            "thoi_gian": "2026-09-04 08:00:00",
            "anh_urls": "",
            "video_urls": "",
            "huu_ich": 0,
        },
        {
            "user": "khach_04",
            "sao": 5,
            "noi_dung": "Đánh giá 4: ảnh độc nhất",
            "phan_loai": "Màu Đen, Size XL",
            "thoi_gian": "2026-09-03 07:00:00",
            "anh_urls": "https://down-vn.img.susercontent.com/file/pic_unique",
            "video_urls": "",
            "huu_ich": 5,
        },
    ]

    stem = "shopee_25018847315_reviews"
    status, res = _post(
        base,
        f"/ingest?name={stem}",
        {
            "source": "https://shopee.vn/product/123/25018847315",
            "rows": test_rows,
        },
    )

    assert status == 200
    assert res["ok"] is True
    assert res["count"] == 4

    # 1. VERIFY CSV UTF-8 BOM & COLUMNS
    csv_rel = res["csv"]
    assert csv_rel == f"outputs/csv/{stem}.csv"
    csv_file = outputs / "csv" / f"{stem}.csv"
    assert csv_file.is_file()

    raw_csv_bytes = csv_file.read_bytes()
    assert raw_csv_bytes.startswith(bytes([0xEF, 0xBB, 0xBF])), "CSV must start with UTF-8 BOM bytes"

    csv_text = raw_csv_bytes.decode("utf-8-sig")
    csv_lines = [line for line in csv_text.splitlines() if line.strip()]
    header = csv_lines[0].split(",")

    for col in [
        "thoi_gian", "sao", "noi_dung", "phan_loai", "user", "huu_ich",
        "anh", "so_anh", "anh_urls", "image_files", "image_names",
        "video", "so_video", "video_urls", "video_files", "video_names",
        "media_urls", "media_files", "media_names", "media_dir", "media_errors",
    ]:
        assert col in header, f"Missing required Shopee review column: {col}"

    inbox_json = json.loads((outputs / "inbox" / f"{stem}.json").read_text(encoding="utf-8"))
    saved_rows = inbox_json["rows"]
    assert len(saved_rows) == 4

    row_by_user = {r["user"]: r for r in saved_rows}

    # Review 3 (no media):
    r3 = row_by_user["khach_03"]
    assert r3.get("image_files", "") == ""
    assert r3.get("video_files", "") == ""
    assert r3.get("media_files", "") == ""

    # Review 1 (has pic1, pic_dup, vid1):
    r1 = row_by_user["khach_01"]
    assert r1["so_anh"] == 2
    assert r1["so_video"] == 1
    assert r1["image_files"] != ""
    assert r1["video_files"] != ""
    r1_img_files = r1["image_files"].split("|")
    assert len(r1_img_files) == 2
    assert r1_img_files[0] == r1_img_files[1], "Duplicate image URL in review 1 must resolve to primary local file"

    # Review 2 (vid_dup, broken image):
    r2 = row_by_user["khach_02"]
    assert r2["so_video"] == 1
    assert r2["video_files"] == r1["video_files"], "Duplicate video in review 2 must preserve relationship to primary video file"
    assert "broken_image_url" in r2["media_errors"]

    # Review 4 (pic_unique):
    r4 = row_by_user["khach_04"]
    assert r4["so_anh"] == 1
    assert r4["image_files"] != ""
    assert r4["image_files"] != r1_img_files[0]

    # 2. VERIFY SHA-256 DEDUPLICATION ON DISK
    media_dir = outputs / "media" / stem
    assert media_dir.is_dir()
    disk_files = {p.name for p in media_dir.iterdir() if p.is_file() and p.name != "manifest.json"}
    assert len(disk_files) == 3, f"Expected 3 unique media files on disk, found {disk_files}"

    # 3. VERIFY MANIFEST.JSON
    manifest_file = media_dir / "manifest.json"
    assert manifest_file.is_file()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    assert manifest["job_name"] == stem
    assert manifest["unique_count"] == 3
    assert manifest["duplicate_count"] == 2
    assert manifest["failed_count"] == 1

    files_list = manifest["files"]
    dup_entries = [f for f in files_list if f.get("duplicate_of")]
    assert len(dup_entries) == 2
    dup_types = {d["type"] for d in dup_entries}
    assert "image" in dup_types, "Must record image duplication"
    assert "video" in dup_types, "Must record video duplication"

    failed_entries = [f for f in files_list if f.get("error")]
    assert len(failed_entries) == 1
    assert "404" in failed_entries[0]["error"] or "Not Found" in failed_entries[0]["error"]
    assert failed_entries[0]["local_file"] is None

    for entry in files_list:
        assert "url" in entry
        assert "local_file" in entry
        assert "filename" in entry
        assert "local_path" in entry
        assert "type" in entry
        assert "loai_media" in entry
        assert "hash" in entry
        assert "sha256" in entry
        assert "size" in entry
        assert "duplicate_of" in entry
        assert "error" in entry

    # 4. VERIFY ZIP ARCHIVE CONTAINS CSV, MEDIA, AND MANIFEST
    zip_rel = res["zip"]
    assert zip_rel == f"outputs/zips/{stem}_media.zip"
    zip_path = outputs / "zips" / f"{stem}_media.zip"
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path, "r") as zf:
        zip_names = zf.namelist()
        assert f"{stem}.csv" in zip_names, "ZIP archive must include the CSV file"
        assert "manifest.json" in zip_names, "ZIP archive must include manifest.json"
        for df in disk_files:
            assert df in zip_names, f"ZIP archive must contain unique media file {df}"
        assert len(zip_names) == 5  # 1 CSV + 1 manifest + 3 unique media files

    # 5. VERIFY MARKDOWN REPORT STATISTICS
    report_rel = res["report"]
    assert report_rel == f"outputs/text/{stem}_report.md"
    report_file = outputs / "text" / f"{stem}_report.md"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")

    assert "- **Tổng số đánh giá**: 4" in report_text
    assert "- **Đánh giá có ảnh/video**: 3" in report_text
    assert "- **Đánh giá không có media**: 1" in report_text
    assert "- ⭐⭐⭐⭐⭐ (5 sao): 2" in report_text
    assert "- ⭐⭐⭐⭐ (4 sao): 1" in report_text
    assert "- ⭐⭐⭐ (3 sao): 1" in report_text
    assert "- **Số file duy nhất (unique)**: 3" in report_text
    assert "- **Số file trùng lặp (SHA-256 deduplicated)**: 2" in report_text
    assert "- **Số file lỗi tải**: 1" in report_text

    assert f"[Tải file CSV](http://localhost:8766/outputs/csv/{stem}.csv)" in report_text
    assert f"[Tải trọn bộ ảnh/video .ZIP](http://localhost:8766/outputs/zips/{stem}_media.zip)" in report_text
    assert f"[Xem manifest JSON](http://localhost:8766/outputs/media/{stem}/manifest.json)" in report_text
    assert f"[Xem báo cáo Markdown](http://localhost:8766/outputs/text/{stem}_report.md)" in report_text

    # 6. VERIFY LOCALHOST HTTP ENDPOINTS
    code, hdrs, body = _get_raw(base, f"/outputs/csv/{stem}.csv")
    assert code == 200
    assert "text/csv" in hdrs.get("Content-Type", "")
    assert body.startswith(bytes([0xEF, 0xBB, 0xBF]))

    code, hdrs, body = _get_raw(base, f"/outputs/zips/{stem}_media.zip")
    assert code == 200
    assert "application/zip" in hdrs.get("Content-Type", "")
    with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
        assert f"{stem}.csv" in zf.namelist()
        assert "manifest.json" in zf.namelist()

    code, hdrs, body = _get_raw(base, f"/outputs/media/{stem}/manifest.json")
    assert code == 200
    assert "application/json" in hdrs.get("Content-Type", "")
    man_json = json.loads(body.decode("utf-8"))
    assert man_json["job_name"] == stem

    code, hdrs, body = _get_raw(base, f"/outputs/text/{stem}_report.md")
    assert code == 200
    assert "text/markdown" in hdrs.get("Content-Type", "")
    assert f"# Báo cáo đánh giá Shopee: {stem}" in body.decode("utf-8")


def test_shopee_export_multipart_zip_splitting(export_server, monkeypatch):
    """Test that ZIP automatically splits into parts when exceeding max_zip_mb threshold."""
    base, outputs = export_server

    def fake_download(url: str, target_without_ext: Path) -> str:
        target = target_without_ext.with_suffix(".jpg")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(os.urandom(800 * 1024))
        return f"outputs/{target.relative_to(outputs).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download)

    rows = [
        {"user": "u1", "sao": 5, "noi_dung": "r1", "anh_urls": "https://down-vn.img.susercontent.com/file/f1"},
        {"user": "u2", "sao": 5, "noi_dung": "r2", "anh_urls": "https://down-vn.img.susercontent.com/file/f2"},
        {"user": "u3", "sao": 5, "noi_dung": "r3", "anh_urls": "https://down-vn.img.susercontent.com/file/f3"},
    ]

    stem = "shopee_split_test"
    status, res = _post(
        base,
        f"/ingest?name={stem}",
        {
            "source": "https://shopee.vn/product/1/2",
            "rows": rows,
            "max_zip_mb": 1,
        },
    )

    assert status == 200
    assert res["ok"] is True
    zip_parts = res.get("zip_parts") or []
    assert len(zip_parts) >= 2, f"Expected at least 2 zip parts, got {zip_parts}"

    part1_path = outputs / "zips" / f"{stem}_media_part01.zip"
    assert part1_path.is_file()
    with zipfile.ZipFile(part1_path, "r") as zf:
        zf_names = zf.namelist()
        assert f"{stem}.csv" in zf_names, "Part 1 must include CSV file"
        assert "manifest.json" in zf_names, "Part 1 must include manifest.json"

    part2_path = outputs / "zips" / f"{stem}_media_part02.zip"
    assert part2_path.is_file()
    with zipfile.ZipFile(part2_path, "r") as zf:
        zf_names = zf.namelist()
        assert "manifest.json" in zf_names, "Part 2 must include manifest.json"

    report_file = outputs / "text" / f"{stem}_report.md"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "Part 1" in report_text
    assert "Part 2" in report_text


def test_shopee_export_only_csv_when_no_media(export_server):
    """When reviews contain no media, only CSV must be produced without empty ZIP archives."""
    base, outputs = export_server

    rows = [
        {"user": "u1", "sao": 5, "noi_dung": "hàng rất tốt không cần chụp ảnh", "thoi_gian": "2026-09-01 10:00:00"},
        {"user": "u2", "sao": 4, "noi_dung": "giao nhanh đóng gói cẩn thận", "thoi_gian": "2026-09-02 11:00:00"},
    ]

    stem = "shopee_nomedia_99999_reviews"
    status, res = _post(
        base,
        f"/ingest?name={stem}",
        {
            "source": "https://shopee.vn/product/1/99999",
            "rows": rows,
        },
    )

    assert status == 200
    assert res["ok"] is True
    assert res["csv"] == f"outputs/csv/{stem}.csv"
    assert res["zip"] is None, "Must not create empty ZIP archive when no media exists"

    csv_file = outputs / "csv" / f"{stem}.csv"
    assert csv_file.is_file()
    raw_bytes = csv_file.read_bytes()
    assert raw_bytes.startswith(bytes([0xEF, 0xBB, 0xBF]))

    report_file = outputs / "text" / f"{stem}_report.md"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "Không có media nào được tải (chỉ xuất file CSV)" in report_text
