"""Unified pipeline for crawling data with media (images, videos),
exporting CSV and bundling all media into a downloadable .zip archive.
"""

from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path
from typing import Any

from .crawl import (
    _save_csv,
    _zip_folder,
    _download_media_and_zip,
    _output_subdir,
    _output_root,
    _is_in_outputs,
    _OUTPUTS_BASE_URL,
)


class ReportDict(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


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
    4. Generates a summary Markdown report in outputs/text/<job_name>_report.md
    5. Returns direct download URLs and local paths for CSV, ZIP(s), and Markdown report.
    """
    ts = int(time.time())
    clean_job = re.sub(r"[^a-zA-Z0-9_-]+", "_", (job_name or f"crawl_{ts}").strip())[:60]

    clean_output_dir = str(output_dir).strip() if output_dir else ""
    resolved_output_dir = str(Path(clean_output_dir).expanduser().resolve()) if clean_output_dir else str(_output_root().resolve())
    is_in_outputs = _is_in_outputs(resolved_output_dir)

    # 1. Export CSV
    csv_name = csv_filename.strip() or f"{clean_job}.csv"
    if not csv_name.lower().endswith(".csv"):
        csv_name += ".csv"

    csv_res = _save_csv(rows=rows, filename=csv_name, headers=headers, output_dir=clean_output_dir)
    if "error" in csv_res:
        return {"error": f"Failed to save CSV: {csv_res['error']}"}

    result: dict[str, Any] = {
        "ok": True,
        "job_name": clean_job,
        "output_dir": resolved_output_dir,
        "is_in_outputs": is_in_outputs,
        "csv": {
            "filename": csv_res.get("filename", csv_name),
            "path": csv_res.get("path"),
            "url": csv_res.get("url"),
            "row_count": csv_res.get("row_count", len(rows)),
            "is_in_outputs": _is_in_outputs(csv_res.get("path")),
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
                "is_in_outputs": _is_in_outputs(zip_res.get("zip_path")),
            }
            result["zip_urls"] = zip_urls_list
    else:
        result["zip"] = None
        result["zip_urls"] = []

    # 4. Generate Markdown report in outputs/text/<clean_job>_report.md
    text_dir = _output_subdir("text", base_dir=clean_output_dir)
    report_filename = f"{clean_job}_report.md"
    report_path = text_dir / report_filename

    row_count = len(rows)
    media_found = len(unique_media)
    csv_url = result["csv"].get("url", "")
    csv_path = result["csv"].get("path", "")
    zip_path = result.get("zip", {}).get("path", "") if result.get("zip") else ""

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
        f"- **Thư mục lưu (local)**: `{resolved_output_dir}`",
        f"- **Tổng số dòng (rows)**: {row_count}",
        f"- **Số media tìm thấy**: {media_found}",
        f"- **Số tải thành công**: {downloaded_count}",
        f"- **Số bỏ qua (đã có/resume)**: {skipped_count}",
        f"- **Số media trùng lặp (đã gộp)**: {duplicate_count}",
        f"- **Số tải thất bại**: {failed_count}",
        "",
        "## Liên kết dữ liệu",
        f"- 📄 [Tải file CSV]({csv_url})",
        f"  - Đường dẫn local: `{csv_path}`",
    ]

    if zip_urls:
        if len(zip_urls) == 1:
            report_lines.append(f"- 📦 [Tải trọn bộ ảnh/video .ZIP]({zip_urls[0]})")
            if zip_path:
                report_lines.append(f"  - Đường dẫn local: `{zip_path}`")
        else:
            report_lines.append("- 📦 **Tải trọn bộ ảnh/video .ZIP (nhiều phần)**:")
            for part_i, u in enumerate(zip_urls, start=1):
                part_p = result["zip"]["paths"][part_i - 1] if part_i - 1 < len(result["zip"].get("paths", [])) else ""
                report_lines.append(f"  - [Tải trọn bộ ảnh/video .ZIP (Part {part_i})]({u})")
                if part_p:
                    report_lines.append(f"    - Đường dẫn local: `{part_p}`")
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

    result["report"] = ReportDict({
        "filename": report_filename,
        "path": str(report_path),
        "url": report_url,
        "is_in_outputs": _is_in_outputs(report_path),
    })
    result["report_url"] = report_url

    return result


def download_media_from_csv(
    csv_path: str,
    url_columns: list[str] | None = None,
    job_name: str = "",
    zip_filename: str = "",
    output_dir: str = "",
    max_zip_mb: int | float = 0,
    max_files: int = 500,
    max_mb_per_file: int = 25,
) -> dict[str, Any]:
    """Read an existing CSV file, detect or filter image/video URL columns,
    download all media files, deduplicate identical files by SHA-256 hash,
    generate a manifest.json, package unique media into a .zip archive
    (splitting if exceeding max_zip_mb), and generate a summary Markdown report.
    """
    raw_path = str(csv_path or "").strip()
    if not raw_path:
        return {"ok": False, "error": "csv_path must not be empty"}

    p = Path(raw_path).expanduser()
    resolved_path: Path | None = None

    if p.is_file():
        resolved_path = p.resolve()
    else:
        candidates = [
            _output_subdir("csv", base_dir=output_dir) / raw_path,
            _output_subdir("csv", base_dir=output_dir) / f"{raw_path}.csv",
            _output_root() / "csv" / raw_path,
            _output_root() / "csv" / f"{raw_path}.csv",
            _output_root() / raw_path,
            Path.cwd() / raw_path,
        ]
        for c in candidates:
            if c.is_file():
                resolved_path = c.resolve()
                break

    if not resolved_path or not resolved_path.exists():
        return {"ok": False, "error": f"CSV file not found: {raw_path}"}

    rows: list[dict[str, Any]] = []
    fieldnames: list[str] = []
    encodings_to_try = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]
    read_success = False

    for enc in encodings_to_try:
        try:
            with open(resolved_path, "r", encoding=enc, errors="replace" if enc == "latin-1" else "strict") as f:
                reader = csv.DictReader(f)
                fieldnames = [str(fn).strip() for fn in (reader.fieldnames or []) if fn]
                rows = list(reader)
                read_success = True
                break
        except (UnicodeDecodeError, Exception):
            continue

    if not read_success or not fieldnames:
        return {"ok": False, "error": f"Failed to read CSV or CSV has no headers: {resolved_path}"}

    if not rows:
        return {"ok": False, "error": f"CSV file has no data rows: {resolved_path}", "columns": fieldnames}

    media_extensions = (
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v",
        ".svg", ".pdf", ".bmp"
    )

    selected_columns: list[str] = []
    if url_columns:
        fn_lower_map = {fn.lower(): fn for fn in fieldnames}
        for uc in url_columns:
            clean_uc = str(uc).strip()
            if clean_uc in fieldnames:
                selected_columns.append(clean_uc)
            elif clean_uc.lower() in fn_lower_map:
                selected_columns.append(fn_lower_map[clean_uc.lower()])
        if not selected_columns:
            return {
                "ok": False,
                "error": f"None of the specified url_columns {url_columns} exist in CSV. Available columns: {fieldnames}",
            }
    else:
        hint_kws = ("image", "img", "photo", "picture", "anh", "hinh", "video", "clip", "media", "thumb", "avatar", "url", "link", "attachment", "file")
        hint_cols = [fn for fn in fieldnames if any(kw in fn.lower() for kw in hint_kws)]
        selected_columns = hint_cols if hint_cols else list(fieldnames)

    url_pattern = re.compile(r'https?://[^\s",;\[\]<>\'\\|]+')
    extracted_urls: list[str] = []

    for r in rows:
        if not isinstance(r, dict):
            continue
        for col in selected_columns:
            val = r.get(col)
            if not val:
                continue
            if isinstance(val, (list, tuple)):
                items = [str(x) for x in val]
            else:
                items = [str(val)]

            for item_str in items:
                found = url_pattern.findall(item_str)
                for u in found:
                    u = u.rstrip(".,;)]")
                    u_lower = u.lower()
                    if url_columns:
                        extracted_urls.append(u)
                    else:
                        if any(ext in u_lower for ext in media_extensions) or any(
                            hint in u_lower for hint in ("susercontent.com", "/image", "/img", "/video", "/media", "/photo")
                        ):
                            extracted_urls.append(u)

    if not extracted_urls and not url_columns and selected_columns != fieldnames:
        for r in rows:
            if not isinstance(r, dict):
                continue
            for col, val in r.items():
                if not val:
                    continue
                for u in url_pattern.findall(str(val)):
                    u = u.rstrip(".,;)]")
                    if any(ext in u.lower() for ext in media_extensions):
                        extracted_urls.append(u)

    seen = set()
    unique_media: list[str] = []
    for u in extracted_urls:
        if u not in seen:
            seen.add(u)
            unique_media.append(u)

    if not unique_media:
        return {
            "ok": False,
            "error": f"No image or video URLs found in CSV '{resolved_path.name}'",
            "csv_path": str(resolved_path),
            "row_count": len(rows),
            "columns": fieldnames,
            "selected_columns": selected_columns,
        }

    clean_job = re.sub(r"[^a-zA-Z0-9_-]+", "_", (job_name or resolved_path.stem).strip())[:60]
    zip_name = zip_filename.strip() or f"{clean_job}_media.zip"
    if not zip_name.lower().endswith(".zip"):
        zip_name += ".zip"

    clean_output_dir = str(output_dir).strip() if output_dir else ""
    resolved_output_dir = str(Path(clean_output_dir).expanduser().resolve()) if clean_output_dir else str(_output_root().resolve())

    zip_res = _download_media_and_zip(
        urls=unique_media,
        zip_filename=zip_name,
        folder_name=clean_job,
        max_files=max_files,
        max_mb_per_file=max_mb_per_file,
        output_dir=clean_output_dir,
        max_zip_mb=max_zip_mb,
    )

    if "error" in zip_res:
        return {
            "ok": False,
            "error": f"Failed to download media or create zip: {zip_res['error']}",
            "job_name": clean_job,
            "csv_path": str(resolved_path),
            "media_found": len(unique_media),
        }

    text_dir = _output_subdir("text", base_dir=clean_output_dir)
    report_filename = f"{clean_job}_report.md"
    report_path = text_dir / report_filename

    zip_urls_list = zip_res.get("zip_urls", [zip_res.get("zip_url")] if zip_res.get("zip_url") else [])
    zip_path = zip_res.get("zip_path", "")
    downloaded_count = zip_res.get("unique_count", zip_res.get("file_count", 0))
    skipped_count = zip_res.get("skipped_count", 0)
    duplicate_count = zip_res.get("duplicate_count", 0)
    failed_count = zip_res.get("failed_count", 0)
    top_errors = zip_res.get("errors", [])

    report_lines = [
        f"# Media Extraction Report: {clean_job}",
        "",
        "## Thống kê tổng quan",
        f"- **Job Name**: `{clean_job}`",
        f"- **Thời gian tạo**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **File CSV nguồn**: `{resolved_path}`",
        f"- **Thư mục lưu (local)**: `{resolved_output_dir}`",
        f"- **Tổng số dòng CSV**: {len(rows)}",
        f"- **Cột đã quét URL**: {', '.join(f'`{c}`' for c in selected_columns)}",
        f"- **Số media URL tìm thấy**: {len(unique_media)}",
        f"- **Số file tải thành công (unique)**: {downloaded_count}",
        f"- **Số bỏ qua (đã có/resume)**: {skipped_count}",
        f"- **Số media trùng lặp (đã gộp SHA-256)**: {duplicate_count}",
        f"- **Số tải thất bại**: {failed_count}",
        "",
        "## Liên kết tải về",
    ]

    if zip_urls_list:
        if len(zip_urls_list) == 1:
            report_lines.append(f"- 📦 [Tải trọn bộ ảnh/video .ZIP]({zip_urls_list[0]})")
            if zip_path:
                report_lines.append(f"  - Đường dẫn local: `{zip_path}`")
        else:
            report_lines.append("- 📦 **Tải trọn bộ ảnh/video .ZIP (nhiều phần)**:")
            for part_i, u in enumerate(zip_urls_list, start=1):
                part_p = zip_res["zip_paths"][part_i - 1] if part_i - 1 < len(zip_res.get("zip_paths", [])) else ""
                report_lines.append(f"  - [Tải trọn bộ ảnh/video .ZIP (Part {part_i})]({u})")
                if part_p:
                    report_lines.append(f"    - Đường dẫn local: `{part_p}`")

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

    result = {
        "ok": True,
        "job_name": clean_job,
        "csv_path": str(resolved_path),
        "row_count": len(rows),
        "media_found": len(unique_media),
        "columns_used": selected_columns,
        "output_dir": resolved_output_dir,
        "is_in_outputs": _is_in_outputs(resolved_output_dir),
        "media_folder": zip_res.get("media_folder"),
        "zip": {
            "filename": zip_res.get("filename", zip_name),
            "filenames": zip_res.get("filenames", [zip_res.get("filename", zip_name)]),
            "path": zip_res.get("zip_path"),
            "paths": zip_res.get("zip_paths", [zip_res.get("zip_path")]),
            "url": zip_res.get("zip_url"),
            "zip_urls": zip_urls_list,
            "part_count": zip_res.get("part_count", len(zip_urls_list)),
            "file_count": zip_res.get("file_count", 0),
            "zip_size_mb": zip_res.get("zip_size_mb", 0.0),
            "downloaded_count": downloaded_count,
            "skipped_count": skipped_count,
            "unique_count": zip_res.get("unique_count", zip_res.get("file_count", 0)),
            "duplicate_count": duplicate_count,
            "failed_count": failed_count,
            "manifest_path": zip_res.get("manifest_path"),
            "manifest": zip_res.get("manifest"),
            "is_in_outputs": _is_in_outputs(zip_res.get("zip_path")),
        },
        "zip_urls": zip_urls_list,
        "report": ReportDict({
            "filename": report_filename,
            "path": str(report_path),
            "url": report_url,
            "is_in_outputs": _is_in_outputs(report_path),
        }),
        "report_url": report_url,
    }
    return result

