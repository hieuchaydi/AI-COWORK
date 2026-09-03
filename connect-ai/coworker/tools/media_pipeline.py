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
) -> dict[str, Any]:
    """Process structured crawl rows and optional media URLs into a complete bundle:
    1. Saves rows to Excel-safe CSV in outputs/csv/ (or custom output_dir/csv/)
    2. Downloads all images and videos to outputs/media/<job_name>/ (or custom output_dir/media/<job_name>/)
    3. Compresses all media into outputs/zips/<zip_filename>.zip (or custom output_dir/zips/<zip_filename>.zip)
    4. Returns direct download URLs for both CSV and ZIP.
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
        )
        if "error" in zip_res:
            result["zip_error"] = zip_res["error"]
        else:
            result["zip"] = {
                "filename": zip_res.get("filename", zip_name),
                "path": zip_res.get("zip_path"),
                "url": zip_res.get("zip_url"),
                "file_count": zip_res.get("file_count", 0),
                "zip_size_mb": zip_res.get("zip_size_mb", 0.0),
                "media_folder": zip_res.get("media_folder"),
                "unique_count": zip_res.get("unique_count", zip_res.get("file_count", 0)),
                "duplicate_count": zip_res.get("duplicate_count", 0),
                "manifest_path": zip_res.get("manifest_path"),
                "manifest": zip_res.get("manifest"),
            }
    else:
        result["zip"] = None

    return result
