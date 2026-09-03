"""Unified pipeline for crawling data with media (images, videos),
exporting CSV and bundling all media into a downloadable .zip archive.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .crawl import _save_csv, _zip_folder, _download_media_and_zip, _output_subdir, _OUTPUTS_BASE_URL


def crawl_and_export_bundle(
    rows: list[dict[str, Any]],
    media_urls: list[str] | None = None,
    job_name: str = "",
    csv_filename: str = "",
    zip_filename: str = "",
    headers: list[str] | None = None,
    output_dir: str = "",
    max_zip_mb: int | float = 0,
) -> dict[str, Any]:
    """Process structured crawl rows and optional media URLs into a complete bundle:
    1. Saves rows to Excel-safe CSV in outputs/csv/ (or custom output_dir/csv/)
    2. Downloads all images and videos to outputs/media/<job_name>/ (or custom output_dir/media/<job_name>/)
    3. Compresses all media into outputs/zips/<zip_filename>.zip (or custom output_dir/zips/<zip_filename>.zip,
       split into parts if exceeding max_zip_mb)
    4. Returns direct download URLs for CSV and ZIP(s).
    """
    ts = int(time.time())
    clean_job = re.sub(r"[^a-zA-Z0-9_-]+", "_", (job_name or f"crawl_{ts}").strip())[:60]

    # 1. Export CSV
    csv_name = csv_filename.strip() or f"{clean_job}.csv"
    if not csv_name.lower().endswith(".csv"):
        csv_name += ".csv"

    csv_res = _save_csv(rows=rows, filename=csv_name, headers=headers, output_dir=output_dir)
    if "error" in csv_res:
        return {"error": f"Failed to save CSV: {csv_res['error']}"}

    result: dict[str, Any] = {
        "ok": True,
        "job_name": clean_job,
        "csv": {
            "filename": csv_res.get("filename", csv_name),
            "path": csv_res.get("path"),
            "url": csv_res.get("url"),
            "row_count": csv_res.get("row_count", len(rows)),
        },
    }

    # 2. Extract media URLs from rows if not explicitly provided
    all_media: list[str] = list(media_urls or [])
    if not media_urls:
        for r in rows:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    if any(ext in v.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm")):
                        all_media.append(v)
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        if isinstance(item, str) and item.startswith(("http://", "https://")):
                            if any(ext in item.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm")):
                                all_media.append(item)

    # De-duplicate media URLs while preserving order
    seen = set()
    unique_media = []
    for u in all_media:
        if u not in seen:
            seen.add(u)
            unique_media.append(u)

    # 3. If media exists, download and pack into .zip
    if unique_media:
        zip_name = zip_filename.strip() or f"{clean_job}_media.zip"
        if not zip_name.lower().endswith(".zip"):
            zip_name += ".zip"

        zip_res = _download_media_and_zip(
            urls=unique_media,
            zip_filename=zip_name,
            folder_name=clean_job,
            output_dir=output_dir,
            max_zip_mb=max_zip_mb,
        )
        if "error" in zip_res:
            result["zip_error"] = zip_res["error"]
            result["zip_urls"] = []
        else:
            zip_urls_list = zip_res.get("zip_urls", [zip_res.get("zip_url")] if zip_res.get("zip_url") else [])
            result["zip"] = {
                "filename": zip_res.get("filename", zip_name),
                "filenames": zip_res.get("filenames", [zip_res.get("filename", zip_name)]),
                "path": zip_res.get("zip_path"),
                "paths": zip_res.get("zip_paths", [zip_res.get("zip_path")]),
                "url": zip_res.get("zip_url"),
                "zip_urls": zip_urls_list,
                "part_count": zip_res.get("part_count", len(zip_urls_list)),
                "file_count": zip_res.get("file_count", 0),
                "zip_size_mb": zip_res.get("zip_size_mb", 0.0),
                "media_folder": zip_res.get("media_folder"),
                "downloaded_count": zip_res.get("downloaded_count", 0),
                "skipped_count": zip_res.get("skipped_count", 0),
                "unique_count": zip_res.get("unique_count", zip_res.get("file_count", 0)),
                "duplicate_count": zip_res.get("duplicate_count", 0),
                "manifest_path": zip_res.get("manifest_path"),
                "manifest": zip_res.get("manifest"),
            }
            result["zip_urls"] = zip_urls_list
    else:
        result["zip"] = None
        result["zip_urls"] = []

    # 4. Generate Markdown report in outputs/text/<clean_job>_report.md
    text_dir = _output_subdir("text", base_dir=output_dir)
    report_filename = f"{clean_job}_report.md"
    report_path = text_dir / report_filename

    row_count = len(rows)
    media_found = len(unique_media)
    csv_url = result["csv"].get("url", "")

    zip_info = result.get("zip")
    if zip_info:
        downloaded_count = zip_info.get("unique_count", zip_info.get("file_count", 0))
        skipped_count = zip_info.get("skipped_count", 0)
        duplicate_count = zip_info.get("duplicate_count", 0)
        failed_count = zip_res.get("failed_count", 0) if "zip_res" in locals() else 0
        zip_urls = zip_info.get("zip_urls", [])
        top_errors = zip_res.get("errors", []) if "zip_res" in locals() else []
    else:
        downloaded_count = 0
        skipped_count = 0
        duplicate_count = 0
        failed_count = len(unique_media) if result.get("zip_error") else 0
        zip_urls = []
        top_errors = [{"url": "zip_process", "error": result["zip_error"]}] if result.get("zip_error") else []

    report_lines = [
        f"# Crawl Report: {clean_job}",
        "",
        "## Thống kê tổng quan",
        f"- **Job Name**: `{clean_job}`",
        f"- **Thời gian tạo**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}",
        f"- **Tổng số dòng (rows)**: {row_count}",
        f"- **Số media tìm thấy**: {media_found}",
        f"- **Số tải thành công**: {downloaded_count}",
        f"- **Số bỏ qua (đã có/resume)**: {skipped_count}",
        f"- **Số media trùng lặp (đã gộp)**: {duplicate_count}",
        f"- **Số tải thất bại**: {failed_count}",
        "",
        "## Liên kết dữ liệu",
        f"- 📄 [Tải file CSV]({csv_url})",
    ]

    if zip_urls:
        if len(zip_urls) == 1:
            report_lines.append(f"- 📦 [Tải trọn bộ ảnh/video .ZIP]({zip_urls[0]})")
        else:
            report_lines.append("- 📦 **Tải trọn bộ ảnh/video .ZIP (nhiều phần)**:")
            for part_i, u in enumerate(zip_urls, start=1):
                report_lines.append(f"  - [Tải trọn bộ ảnh/video .ZIP (Part {part_i})]({u})")
    else:
        report_lines.append("- 📦 [Tải trọn bộ ảnh/video .ZIP]: *Không có media hoặc chưa nén*")

    report_lines.extend(["", "## Top lỗi (Errors)"])
    if top_errors:
        for err in top_errors[:10]:
            e_url = err.get("url", "unknown")
            e_msg = err.get("error", "unknown error")
            report_lines.append(f"- `{e_url}`: {e_msg}")
    elif failed_count > 0:
        report_lines.append(f"- {failed_count} file tải thất bại (xem log chi tiết).")
    else:
        report_lines.append("- Không có lỗi nào phát sinh.")

    report_content = "\n".join(report_lines) + "\n"
    report_path.write_text(report_content, encoding="utf-8")

    report_url = f"{_OUTPUTS_BASE_URL}/text/{report_filename}"

    class ReportDict(dict):
        def __getattr__(self, name: str) -> Any:
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name)

    result["report"] = ReportDict({
        "filename": report_filename,
        "path": str(report_path),
        "url": report_url,
    })
    result["report_url"] = report_url

    return result
