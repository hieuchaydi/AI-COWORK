"""Built-in web crawling / scraping tools.

Complements web_fetch (single page) and browser_* (interactive Chromium) with
higher-level bulk primitives an agent can dispatch in one call instead of
looping shot-by-shot:

    crawl_urls          — BFS crawl N pages from a seed URL, same-domain rule
    extract_html        — CSS selector extraction over given URL / raw HTML
    extract_table       — pull an HTML <table> as list-of-dicts JSON
    parse_sitemap       — discover URLs from sitemap.xml (+ sitemapindex)
    save_page_snapshot  — download HTML + inline referenced assets to a folder
    download_file       — save any URL to disk

All use httpx (async in-loop, no MCP), respect optional path/domain filters,
and truncate large responses so a single tool call can't flood the LLM context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aisuite as ai
import httpx


UA = os.environ.get("CRAWL_USER_AGENT", "coworker-crawler/0.1 (+desktop)")
DEFAULT_TIMEOUT = 20.0
MAX_HTML_KEEP = 200_000  # cap per-page memory
MAX_PAGES_HARD = 200     # ceiling regardless of caller ask
MAX_TABLE_ROWS = 500

# ─── Unified output directory ────────────────────────────────────────────────
# Every user-visible output (CSV/JSON/PDF/image/HTML snapshot/…) lands under
# ONE folder in the project root, split into subdirs by kind. Served by
# launch.py helper HTTP 8766 at /outputs/<kind>/<name>. Override the whole
# tree with env COWORKER_OUTPUT_DIR.
#
# Layout:
#   <project>/outputs/
#     csv/          → save_csv
#     text/         → save_artifact (md/json/txt/html/…)
#     downloads/    → download_file (PDF/ZIP/binary)
#     screenshots/  → browser_screenshot
#     snapshots/    → save_page_snapshot (HTML + assets)

def _output_root() -> Path:
    """Base directory for all generated outputs. Auto-created on first use."""
    env = os.environ.get("COWORKER_OUTPUT_DIR")
    if env:
        base = Path(env).expanduser().resolve()
    else:
        # crawl.py sits at openworker/coworker/tools/, so parents[3] = project root
        base = Path(__file__).resolve().parents[3] / "outputs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _is_in_outputs(path: Path | str | None) -> bool:
    """Check whether a given path is inside the project outputs/ directory."""
    if not path:
        return False
    try:
        p = Path(path).expanduser().resolve()
        out_root = _output_root().resolve()
        p.relative_to(out_root)
        return True
    except Exception:
        return False


def _output_subdir(kind: str, base_dir: str | Path | None = None) -> Path:
    """Get (and auto-create) a subdirectory under base_dir (or default output root)."""
    kind = re.sub(r"[^a-z0-9_-]+", "", (kind or "misc").lower()) or "misc"
    clean_base = str(base_dir).strip() if base_dir else ""
    root = Path(clean_base).expanduser().resolve() if clean_base else _output_root()
    d = root / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


_OUTPUTS_BASE_URL = os.environ.get(
    "OUTPUTS_BASE_URL", "http://localhost:8766/outputs"
)

# Backwards compat: old code still writes to /artifacts. Keep that path working
# so already-created files still resolve, but new writes go to /outputs/*.
_ARTIFACTS_DIR = Path(
    os.environ.get("COWORKER_ARTIFACTS_DIR")
    or Path(__file__).resolve().parents[3] / "artifacts"
)
_ARTIFACTS_BASE_URL = os.environ.get(
    "ARTIFACTS_BASE_URL", "http://localhost:8766/artifacts"
)

# Optional BeautifulSoup — stdlib parser suffices; lxml is a nice-to-have.
try:
    from bs4 import BeautifulSoup
    _PARSER = "html.parser"
except Exception as exc:  # noqa: BLE001
    BeautifulSoup = None
    _bs_err = exc
else:
    _bs_err = None


def _attach(func: Callable, schema: dict, risk: str = "medium") -> Callable:
    func.__doc__ = schema["function"]["description"]
    func.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=schema["function"]["name"],
        category="crawl",
        risk_level=risk,
        capabilities=["crawl"],
        requires_approval=False,
    )
    func.__coworker_schema__ = schema
    return func


def _schema(name: str, description: str, props: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept": "*/*"},
        follow_redirects=True,
        timeout=DEFAULT_TIMEOUT,
    )


def _same_domain(u1: str, u2: str) -> bool:
    try:
        return urllib.parse.urlparse(u1).netloc == urllib.parse.urlparse(u2).netloc
    except Exception:
        return False


def _clean_text(html: str, max_chars: int) -> str:
    """Extract visible text via bs4 if available, else strip tags cheaply."""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, _PARSER)
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    else:
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r" {2,}", " ", text).strip()
    return text[:max_chars]


# ─── Tool implementations ────────────────────────────────────────────────────

def _crawl_urls(
    start_url: str,
    max_pages: int = 20,
    same_domain: bool = True,
    follow_pattern: str = "",
    include_text: bool = True,
    max_text_chars: int = 3000,
    delay_ms: int = 300,
) -> dict[str, Any]:
    if not start_url.startswith(("http://", "https://")):
        return {"error": "start_url must be http(s)://"}
    max_pages = max(1, min(int(max_pages or 20), MAX_PAGES_HARD))
    pattern = re.compile(follow_pattern) if follow_pattern else None
    seen, queue, pages, errors = set(), [start_url], [], []
    with _client() as c:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                r = c.get(url)
                r.raise_for_status()
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)[:200]})
                continue
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype.lower():
                # skip non-HTML but record it — often useful in reports
                pages.append({"url": str(r.url), "status": r.status_code, "content_type": ctype, "skipped": True})
                continue
            html = r.text[:MAX_HTML_KEEP]
            entry = {
                "url": str(r.url),
                "status": r.status_code,
                "title": _extract_title(html),
            }
            if include_text:
                entry["text"] = _clean_text(html, max_text_chars)
            pages.append(entry)
            # discover links (respect same_domain + pattern)
            if len(pages) + len(queue) < max_pages * 3:  # cap discovery breadth
                for link in _extract_links(html, base=str(r.url)):
                    if link in seen:
                        continue
                    if same_domain and not _same_domain(start_url, link):
                        continue
                    if pattern and not pattern.search(link):
                        continue
                    queue.append(link)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
    return {
        "start_url": start_url,
        "pages_crawled": len(pages),
        "queue_remaining": len(queue),
        "errors": errors[:20],
        "pages": pages,
    }


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]{1,300})</title>", html, re.I | re.S)
    return (m.group(1).strip() if m else "")


def _extract_links(html: str, base: str) -> list[str]:
    if BeautifulSoup is None:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    else:
        soup = BeautifulSoup(html, _PARSER)
        hrefs = [a.get("href") for a in soup.find_all("a", href=True)]
    out = []
    seen = set()
    for h in hrefs:
        if not h or h.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urllib.parse.urljoin(base, h)
        full = full.split("#", 1)[0]
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _extract_html(
    source: str,
    selector: str,
    attribute: str = "",
    max_matches: int = 100,
) -> dict[str, Any]:
    """CSS-selector extraction. `source` is either a URL or raw HTML."""
    if BeautifulSoup is None:
        return {"error": f"beautifulsoup4 not available: {_bs_err}"}
    if source.startswith(("http://", "https://")):
        try:
            with _client() as c:
                r = c.get(source); r.raise_for_status()
                html = r.text
                origin = str(r.url)
        except Exception as exc:
            return {"error": f"fetch failed: {exc}"}
    else:
        html = source
        origin = "raw"
    soup = BeautifulSoup(html, _PARSER)
    try:
        elements = soup.select(selector)
    except Exception as exc:
        return {"error": f"selector parse failed: {exc}"}
    max_matches = max(1, min(int(max_matches or 100), 1000))
    out = []
    for el in elements[:max_matches]:
        if attribute:
            v = el.get(attribute)
            out.append({"value": v})
        else:
            out.append({
                "text": el.get_text(" ", strip=True)[:2000],
                "html": str(el)[:2000],
                "attrs": {k: v for k, v in el.attrs.items() if isinstance(v, (str, list))},
            })
    return {
        "source": origin,
        "selector": selector,
        "count": len(elements),
        "returned": len(out),
        "matches": out,
    }


def _extract_table(
    source: str,
    table_index: int = 0,
    header_row: int = 0,
) -> dict[str, Any]:
    """Extract an HTML <table> into a list of dicts (headers → cell values)."""
    if BeautifulSoup is None:
        return {"error": f"beautifulsoup4 not available: {_bs_err}"}
    if source.startswith(("http://", "https://")):
        try:
            with _client() as c:
                r = c.get(source); r.raise_for_status()
                html = r.text
        except Exception as exc:
            return {"error": f"fetch failed: {exc}"}
    else:
        html = source
    soup = BeautifulSoup(html, _PARSER)
    tables = soup.find_all("table")
    if not tables:
        return {"error": "no <table> found"}
    if table_index < 0 or table_index >= len(tables):
        return {"error": f"table_index out of range (found {len(tables)} tables)"}
    table = tables[table_index]
    rows = [
        [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        for tr in table.find_all("tr")
    ]
    rows = [r for r in rows if r]
    if not rows:
        return {"error": "table is empty"}
    if header_row < 0 or header_row >= len(rows):
        header_row = 0
    headers = rows[header_row]
    data_rows = rows[header_row + 1:][:MAX_TABLE_ROWS]
    records = []
    for r in data_rows:
        rec = {}
        for i, val in enumerate(r):
            key = headers[i] if i < len(headers) else f"col_{i}"
            rec[key] = val
        records.append(rec)
    return {
        "table_count": len(tables),
        "table_index": table_index,
        "headers": headers,
        "row_count": len(records),
        "rows": records,
    }


def _parse_sitemap(url: str, max_urls: int = 500) -> dict[str, Any]:
    """Fetch sitemap.xml (or sitemapindex) → return all discovered URLs."""
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be http(s)://"}
    try:
        with _client() as c:
            r = c.get(url); r.raise_for_status()
            text = r.text
    except Exception as exc:
        return {"error": f"fetch failed: {exc}"}
    try:
        root = ET.fromstring(re.sub(r"\sxmlns=\"[^\"]+\"", "", text, count=1))
    except Exception as exc:
        return {"error": f"xml parse failed: {exc}"}
    urls = []
    max_urls = max(1, min(int(max_urls or 500), 5000))
    if root.tag.endswith("sitemapindex"):
        # nested sitemaps → fetch children recursively (1 level)
        with _client() as c:
            for sm in root.findall("sitemap"):
                loc = sm.findtext("loc")
                if not loc:
                    continue
                try:
                    rr = c.get(loc); rr.raise_for_status()
                    child = ET.fromstring(re.sub(r"\sxmlns=\"[^\"]+\"", "", rr.text, count=1))
                    for u in child.findall("url"):
                        u_loc = u.findtext("loc")
                        if u_loc:
                            urls.append({
                                "loc": u_loc,
                                "lastmod": u.findtext("lastmod"),
                                "changefreq": u.findtext("changefreq"),
                            })
                        if len(urls) >= max_urls:
                            break
                except Exception:
                    pass
                if len(urls) >= max_urls:
                    break
    else:
        for u in root.findall("url"):
            loc = u.findtext("loc")
            if loc:
                urls.append({
                    "loc": loc,
                    "lastmod": u.findtext("lastmod"),
                    "changefreq": u.findtext("changefreq"),
                })
            if len(urls) >= max_urls:
                break
    return {"sitemap_url": url, "url_count": len(urls), "urls": urls}


def _save_page_snapshot(url: str, save_dir: str = "") -> dict[str, Any]:
    """Download HTML of a page + all referenced <img>/<link>/<script> assets
    into a folder for offline analysis. Default lands in outputs/snapshots/."""
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be http(s)://"}
    if BeautifulSoup is None:
        return {"error": f"beautifulsoup4 not available: {_bs_err}"}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", urllib.parse.urlparse(url).netloc)[:40]
    folder_name = f"{slug}_{stamp}"
    if save_dir:
        base = Path(save_dir).expanduser().resolve()
    else:
        base = _output_subdir("snapshots") / folder_name
    base.mkdir(parents=True, exist_ok=True)
    try:
        with _client() as c:
            r = c.get(url); r.raise_for_status()
            html = r.text
            final = str(r.url)
    except Exception as exc:
        return {"error": f"fetch failed: {exc}"}
    (base / "index.html").write_text(html, encoding="utf-8")
    assets_dir = base / "assets"
    assets_dir.mkdir(exist_ok=True)
    soup = BeautifulSoup(html, _PARSER)
    saved, failed = 0, 0
    with _client() as c:
        for tag, attr in (("img", "src"), ("link", "href"), ("script", "src")):
            for el in soup.find_all(tag):
                src = el.get(attr)
                if not src:
                    continue
                full = urllib.parse.urljoin(final, src)
                try:
                    ar = c.get(full, timeout=10.0)
                    ar.raise_for_status()
                    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", urllib.parse.urlparse(full).path.strip("/"))[-100:]
                    if not name:
                        name = f"asset_{saved}"
                    (assets_dir / name).write_bytes(ar.content)
                    saved += 1
                except Exception:
                    failed += 1
                if saved >= 200:
                    break
    # Public URL: serves index.html via /outputs/snapshots/<folder>/index.html
    public_url = (
        f"{_OUTPUTS_BASE_URL}/snapshots/{folder_name}/index.html"
        if not save_dir
        else None
    )
    return {
        "source_url": final,
        "dir": str(base),
        "public_url": public_url,
        "html_size": len(html),
        "assets_saved": saved,
        "assets_failed": failed,
    }


def _kind_from_ext(filename: str) -> str:
    """Map file extension → subdir. CSV → csv/, PDF/zip/binary → downloads/,
    text-y → text/. Everything unknown → text/."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "csv":
        return "csv"
    if ext in ("pdf", "zip", "tar", "gz", "7z", "rar", "docx", "xlsx", "pptx", "exe", "bin", "iso"):
        return "downloads"
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"):
        return "downloads"
    return "text"  # md, txt, json, html, xml, yaml, csv fallback, etc.


def _save_artifact(
    content: str,
    filename: str,
    encoding: str = "utf-8",
    add_utf8_bom: bool = True,
    output_dir: str = "",
) -> dict[str, Any]:
    """Save a text file into the project's outputs folder (subdir chosen by
    extension: `.csv` → outputs/csv/, `.pdf/.zip` → outputs/downloads/, else
    outputs/text/). Returns the public URL the user can click.

    * `filename` must be a simple name (no path components, no `..`).
    * `add_utf8_bom=True` (default) — CRITICAL for CSV opened in Excel with
      Vietnamese; without the BOM Excel guesses ANSI and shows garbled text.
    * `encoding` = "utf-8" default; use "utf-16" / "cp1258" for legacy readers.
    """
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return {"error": "filename must be a simple name (no path, no '..')"}
    kind = _kind_from_ext(filename)
    target = _output_subdir(kind, base_dir=output_dir) / filename
    is_csv = filename.lower().endswith(".csv")
    payload = content or ""
    try:
        if encoding.lower() in ("utf-8", "utf8") and (add_utf8_bom or is_csv):
            data = "﻿" + payload  # UTF-8 BOM (Excel-safe)
        else:
            data = payload
        target.write_text(data, encoding=encoding, newline="")
    except Exception as exc:
        return {"error": f"write failed: {exc}"}
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "url": f"{_OUTPUTS_BASE_URL}/{kind}/{filename}",
        "filename": filename,
        "kind": kind,
        "is_in_outputs": _is_in_outputs(target),
        "note": (
            "Send THIS url to the user (localhost:8766, not localhost:1420 — "
            "1420 is Vite GUI, returns index.html for unknown paths)."
        ),
    }


def _save_artifact_binary(data: bytes, filename: str) -> dict[str, Any]:
    """Same as save_artifact but for bytes (PNG/PDF/ZIP/...)."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return {"error": "filename must be a simple name (no path, no '..')"}
    kind = _kind_from_ext(filename)
    target = _output_subdir(kind) / filename
    try:
        target.write_bytes(data if isinstance(data, (bytes, bytearray)) else str(data).encode())
    except Exception as exc:
        return {"error": f"write failed: {exc}"}
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "url": f"{_OUTPUTS_BASE_URL}/{kind}/{filename}",
        "kind": kind,
    }


def _rows_to_csv(rows: list, headers: list = None) -> str:
    """Convert list-of-dicts (or list-of-lists) to CSV string with proper quoting.
    Cell values get escaped so commas, quotes, and newlines inside cells don't
    break the file when Excel parses it."""
    import csv, io
    if not rows:
        return ""
    buf = io.StringIO()
    if isinstance(rows[0], dict):
        if not headers:
            headers = list(rows[0].keys())
        w = csv.DictWriter(buf, fieldnames=headers, quoting=csv.QUOTE_MINIMAL,
                           extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)
    else:
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        if headers:
            w.writerow(headers)
        w.writerows(rows)
    return buf.getvalue()


def _save_csv(
    rows: list,
    filename: str,
    headers: list = None,
    output_dir: str = "",
) -> dict[str, Any]:
    """One-shot: convert list of dicts / rows → CSV → save to artifacts folder
    with UTF-8 BOM + CRLF line endings. Returns the public URL. Use this
    instead of hand-building a CSV string and calling save_artifact — the
    encoding + quoting are Excel-safe."""
    if not isinstance(rows, list):
        return {"error": "rows must be a list"}
    csv_text = _rows_to_csv(rows, headers=headers)
    if not filename.lower().endswith(".csv"):
        filename += ".csv"
    r = _save_artifact(csv_text, filename, encoding="utf-8", add_utf8_bom=True, output_dir=output_dir)
    if "url" in r:
        r["row_count"] = len(rows)
        r["headers"] = headers or (list(rows[0].keys()) if rows and isinstance(rows[0], dict) else [])
    return r


def _download_file(url: str, save_to: str = "", max_mb: int = 50) -> dict[str, Any]:
    """Save any URL response to disk. Default target = outputs/downloads/<basename>.
    Streams so large files don't blow memory."""
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be http(s)://"}
    under_outputs = False
    if save_to:
        target = Path(save_to).expanduser().resolve()
    else:
        name = Path(urllib.parse.urlparse(url).path).name or "download.bin"
        # Sanitize: strip query/fragment leftovers, replace unsafe chars
        name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:120] or "download.bin"
        target = _output_subdir("downloads") / name
        under_outputs = True
    target.parent.mkdir(parents=True, exist_ok=True)
    cap = max(1, int(max_mb or 50)) * 1024 * 1024
    try:
        with _client() as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                total = 0
                with open(target, "wb") as f:
                    for chunk in r.iter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > cap:
                            return {"error": f"exceeds max_mb={max_mb}"}
                        f.write(chunk)
                ctype = r.headers.get("content-type", "")
    except Exception as exc:
        try:
            target.unlink()
        except Exception:
            pass
        return {"error": f"download failed: {exc}"}
    result = {
        "path": str(target),
        "size": target.stat().st_size,
        "content_type": ctype,
    }
    if under_outputs:
        result["url"] = f"{_OUTPUTS_BASE_URL}/downloads/{target.name}"
    return result


def _zip_folder(
    folder_path: str,
    zip_filename: str = "",
    output_dir: str = "",
    max_zip_mb: int | float = 0,
) -> dict[str, Any]:
    """Compress an entire directory into a .zip archive (or multiple parts if exceeding max_zip_mb)
    under outputs/zips/ and return the local path(s) and download URL(s)."""
    p = Path(folder_path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        return {"error": f"folder not found: {folder_path}"}

    zips_dir = _output_subdir("zips", base_dir=output_dir)
    base_name = zip_filename.strip() if zip_filename else f"{p.name}.zip"
    if not base_name.lower().endswith(".zip"):
        base_name += ".zip"
    base_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", base_name)

    # Collect all files
    all_files: list[tuple[Path, int]] = []
    manifest_path: Path | None = None
    total_uncompressed = 0
    for root, _, files in os.walk(p):
        for f in files:
            full_path = Path(root) / f
            size = full_path.stat().st_size
            total_uncompressed += size
            if full_path.name == "manifest.json" and full_path.parent == p:
                manifest_path = full_path
            else:
                all_files.append((full_path, size))

    all_files.sort(key=lambda x: str(x[0]))
    max_bytes = int(float(max_zip_mb or 0) * 1024 * 1024)
    should_split = bool(max_bytes > 0 and total_uncompressed > max_bytes and len(all_files) > 1)

    if should_split:
        stem = re.sub(r"\.zip$", "", base_name, flags=re.I)
        prefix = stem if stem.endswith("_media") else f"{stem}_media"

        parts: list[list[Path]] = []
        current_part: list[Path] = []
        current_bytes = 0

        for f_path, f_size in all_files:
            if current_part and (current_bytes + f_size > max_bytes):
                parts.append(current_part)
                current_part = [f_path]
                current_bytes = f_size
            else:
                current_part.append(f_path)
                current_bytes += f_size

        if current_part:
            parts.append(current_part)

        zip_paths: list[str] = []
        zip_urls: list[str] = []
        filenames: list[str] = []
        total_files = 0
        total_zip_size = 0

        try:
            for part_idx, part_files in enumerate(parts, start=1):
                part_name = f"{prefix}_part{part_idx:02d}.zip"
                part_zip = zips_dir / part_name
                with zipfile.ZipFile(part_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    if manifest_path and manifest_path.exists():
                        zf.write(manifest_path, manifest_path.relative_to(p))
                        total_files += 1
                    for f_path in part_files:
                        zf.write(f_path, f_path.relative_to(p))
                        total_files += 1

                total_zip_size += part_zip.stat().st_size
                zip_paths.append(str(part_zip))
                zip_urls.append(f"{_OUTPUTS_BASE_URL}/zips/{part_name}")
                filenames.append(part_name)
        except Exception as exc:
            return {"error": f"failed to create split zip: {exc}"}

        return {
            "ok": True,
            "zip_path": zip_paths[0],
            "zip_url": zip_urls[0],
            "zip_paths": zip_paths,
            "zip_urls": zip_urls,
            "filename": filenames[0],
            "filenames": filenames,
            "part_count": len(zip_urls),
            "file_count": total_files,
            "uncompressed_mb": round(total_uncompressed / (1024 * 1024), 2),
            "zip_size_mb": round(total_zip_size / (1024 * 1024), 2),
            "is_in_outputs": _is_in_outputs(zip_paths[0]),
            "note": f"Split into {len(zip_urls)} parts: {', '.join(zip_urls)}",
        }

    # Standard single zip
    target_zip = zips_dir / base_name
    file_count = 0
    try:
        with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if manifest_path and manifest_path.exists():
                zf.write(manifest_path, manifest_path.relative_to(p))
                file_count += 1
            for f_path, _ in all_files:
                zf.write(f_path, f_path.relative_to(p))
                file_count += 1
    except Exception as exc:
        return {"error": f"failed to create zip: {exc}"}

    zip_size = target_zip.stat().st_size
    single_url = f"{_OUTPUTS_BASE_URL}/zips/{target_zip.name}"
    return {
        "ok": True,
        "zip_path": str(target_zip),
        "zip_url": single_url,
        "zip_paths": [str(target_zip)],
        "zip_urls": [single_url],
        "filename": target_zip.name,
        "filenames": [target_zip.name],
        "part_count": 1,
        "file_count": file_count,
        "uncompressed_mb": round(total_uncompressed / (1024 * 1024), 2),
        "zip_size_mb": round(zip_size / (1024 * 1024), 2),
        "is_in_outputs": _is_in_outputs(target_zip),
        "note": f"Send THIS zip_url to the user for one-click download: {single_url}",
    }


def _download_media_and_zip(
    urls: list[str],
    zip_filename: str = "",
    folder_name: str = "",
    max_files: int = 100,
    max_mb_per_file: int = 25,
    output_dir: str = "",
    max_zip_mb: int | float = 0,
) -> dict[str, Any]:
    """Download a batch of media URLs (images, videos, attachments) into a dedicated folder,
    deduplicating identical files by SHA-256 hash, generating a manifest.json with duplicate_of
    mappings, and packing unique files + manifest into a .zip archive."""
    if not isinstance(urls, list) or not urls:
        return {"error": "urls must be a non-empty list of URLs"}

    job_name = folder_name.strip() or zip_filename.replace(".zip", "").strip() or f"media_{int(time.time())}"
    job_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", job_name)[:80]
    media_dir = _output_subdir("media", base_dir=output_dir) / job_name
    media_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    errors = []
    seen_hashes: dict[str, str] = {}
    manifest_entries: list[dict[str, Any]] = []
    unique_files: list[dict[str, Any]] = []
    duplicate_files: list[dict[str, Any]] = []
    skipped_count = 0

    # Read previous manifest if exists to reuse successful statuses
    old_manifest_path = media_dir / "manifest.json"
    old_manifest_by_url: dict[str, dict[str, Any]] = {}
    if old_manifest_path.exists():
        try:
            old_data = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            for entry in old_data.get("files", []):
                if isinstance(entry, dict) and "url" in entry:
                    old_manifest_by_url[entry["url"]] = entry
        except Exception:
            pass

    cap_per_file = max(1, int(max_mb_per_file or 25)) * 1024 * 1024
    valid_urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))][:max_files]

    with _client() as c:
        for idx, u in enumerate(valid_urls, start=1):
            parsed_path = urllib.parse.urlparse(u).path
            ext = Path(parsed_path).suffix.lower()
            if not ext or len(ext) > 5 or ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm", ".pdf", ".svg"):
                ext = ".bin"

            orig_name = Path(parsed_path).stem
            orig_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", orig_name)[:30]
            fname = f"{idx:03d}_{orig_name}{ext}" if orig_name else f"media_{idx:03d}{ext}"
            dest = media_dir / fname

            # ── Check Resume / Existing Files ──────────────────────────────────
            # 1. Reuse from old manifest if status was successful and file is present
            old_entry = old_manifest_by_url.get(u)
            if old_entry:
                old_dup_of = old_entry.get("duplicate_of")
                old_fname = old_entry.get("filename") or dest.name
                old_hash = old_entry.get("sha256")
                old_size = old_entry.get("size", 0)

                if old_dup_of:
                    primary_path = media_dir / old_dup_of
                    if primary_path.exists() and primary_path.stat().st_size > 0:
                        entry = {
                            "url": u,
                            "filename": old_fname,
                            "sha256": old_hash,
                            "size": old_size,
                            "duplicate_of": old_dup_of,
                        }
                        manifest_entries.append(entry)
                        duplicate_files.append(entry)
                        skipped_count += 1
                        continue
                else:
                    target_file = media_dir / old_fname
                    if target_file.exists() and target_file.stat().st_size > 0:
                        entry = {
                            "url": u,
                            "filename": target_file.name,
                            "sha256": old_hash,
                            "size": target_file.stat().st_size,
                            "duplicate_of": None,
                        }
                        manifest_entries.append(entry)
                        unique_files.append(entry)
                        if old_hash:
                            seen_hashes[old_hash] = target_file.name
                        skipped_count += 1
                        continue

            # 2. Check if dest (or candidate with matching index prefix) exists with size > 0
            candidate_files = []
            if dest.exists() and dest.stat().st_size > 0:
                candidate_files.append(dest)
            else:
                for candidate in media_dir.glob(f"{idx:03d}_*"):
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        candidate_files.append(candidate)
                        break

            if candidate_files:
                existing_file = candidate_files[0]
                hasher = hashlib.sha256()
                with open(existing_file, "rb") as ef:
                    while chunk := ef.read(64 * 1024):
                        hasher.update(chunk)
                file_hash = hasher.hexdigest()
                file_size = existing_file.stat().st_size

                if file_hash in seen_hashes:
                    primary_filename = seen_hashes[file_hash]
                    existing_file.unlink(missing_ok=True)
                    entry = {
                        "url": u,
                        "filename": existing_file.name,
                        "sha256": file_hash,
                        "size": file_size,
                        "duplicate_of": primary_filename,
                    }
                    manifest_entries.append(entry)
                    duplicate_files.append(entry)
                else:
                    seen_hashes[file_hash] = existing_file.name
                    entry = {
                        "url": u,
                        "filename": existing_file.name,
                        "sha256": file_hash,
                        "size": file_size,
                        "duplicate_of": None,
                    }
                    manifest_entries.append(entry)
                    unique_files.append(entry)
                skipped_count += 1
                continue

            # ── Missing or previously failed: download via HTTP ───────────────
            try:
                with c.stream("GET", u) as r:
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "").lower()
                    if ext == ".bin":
                        if "jpeg" in ctype or "jpg" in ctype:
                            dest = dest.with_suffix(".jpg")
                        elif "png" in ctype:
                            dest = dest.with_suffix(".png")
                        elif "webp" in ctype:
                            dest = dest.with_suffix(".webp")
                        elif "mp4" in ctype:
                            dest = dest.with_suffix(".mp4")
                        elif "webm" in ctype:
                            dest = dest.with_suffix(".webm")

                    hasher = hashlib.sha256()
                    total = 0
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > cap_per_file:
                                raise ValueError(f"exceeds max_mb_per_file={max_mb_per_file}")
                            hasher.update(chunk)
                            f.write(chunk)

                file_hash = hasher.hexdigest()

                if file_hash in seen_hashes:
                    primary_filename = seen_hashes[file_hash]
                    dest.unlink(missing_ok=True)
                    entry = {
                        "url": u,
                        "filename": dest.name,
                        "sha256": file_hash,
                        "size": total,
                        "duplicate_of": primary_filename,
                    }
                    manifest_entries.append(entry)
                    duplicate_files.append(entry)
                else:
                    seen_hashes[file_hash] = dest.name
                    entry = {
                        "url": u,
                        "filename": dest.name,
                        "sha256": file_hash,
                        "size": total,
                        "duplicate_of": None,
                    }
                    manifest_entries.append(entry)
                    unique_files.append(entry)
                    downloaded.append({"filename": dest.name, "url": u, "size": total, "sha256": file_hash})
            except Exception as exc:
                if dest.exists():
                    dest.unlink(missing_ok=True)
                errors.append({"url": u, "error": str(exc)})

    if not downloaded and skipped_count == 0:
        return {"error": f"No files were successfully downloaded. Errors: {errors[:5]}"}

    # Generate manifest.json (contains all URLs/files with duplicate_of for duplicates)
    manifest_data = {
        "job_name": job_name,
        "created_at": datetime.now().isoformat(),
        "total_urls": len(valid_urls),
        "downloaded_count": len(downloaded),
        "skipped_count": skipped_count,
        "unique_count": len(unique_files),
        "duplicate_count": len(duplicate_files),
        "failed_count": len(errors),
        "files": manifest_entries,
    }
    manifest_path = media_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Automatically zip the folder (only contains unique files + manifest.json, splitting if exceeding max_zip_mb)
    zip_name = zip_filename.strip() if zip_filename else f"{job_name}.zip"
    zip_result = _zip_folder(
        str(media_dir),
        zip_filename=zip_name,
        output_dir=output_dir,
        max_zip_mb=max_zip_mb,
    )
    if "error" in zip_result:
        return zip_result

    zip_result["downloaded_count"] = len(downloaded)
    zip_result["skipped_count"] = skipped_count
    zip_result["unique_count"] = len(unique_files)
    zip_result["duplicate_count"] = len(duplicate_files)
    zip_result["failed_count"] = len(errors)
    zip_result["errors"] = errors[:10]
    zip_result["media_folder"] = str(media_dir)
    zip_result["manifest_path"] = str(manifest_path)
    zip_result["manifest"] = manifest_data
    zip_result["is_in_outputs"] = _is_in_outputs(media_dir)
    return zip_result


def _crawl_and_export_bundle(
    rows: list[dict[str, Any]],
    media_urls: list[str] | None = None,
    job_name: str = "",
    csv_filename: str = "",
    zip_filename: str = "",
    headers: list[str] | None = None,
    output_dir: str = "",
    max_zip_mb: int | float = 0,
) -> dict[str, Any]:
    """Save crawl rows to CSV, download related media into outputs/media/,
    zip the media folder into outputs/zips/ (splitting if exceeding max_zip_mb),
    and return both download URLs."""
    from .media_pipeline import crawl_and_export_bundle

    return crawl_and_export_bundle(
        rows=rows,
        media_urls=media_urls,
        job_name=job_name,
        csv_filename=csv_filename,
        zip_filename=zip_filename,
        headers=headers,
        output_dir=output_dir,
        max_zip_mb=max_zip_mb,
    )


def _download_media_from_csv(
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
    (splitting if exceeding max_zip_mb), and generate a summary Markdown report."""
    from .media_pipeline import download_media_from_csv

    return download_media_from_csv(
        csv_path=csv_path,
        url_columns=url_columns,
        job_name=job_name,
        zip_filename=zip_filename,
        output_dir=output_dir,
        max_zip_mb=max_zip_mb,
        max_files=max_files,
        max_mb_per_file=max_mb_per_file,
    )


# ─── Factory ─────────────────────────────────────────────────────────────────

def make_crawl_tools() -> list[Callable[..., Any]]:
    tools = []

    def _add(fn, name, desc, props, required, risk="medium"):
        fn.__name__ = name
        tools.append(_attach(fn, _schema(name, desc, props, required), risk))

    _add(
        _crawl_urls, "crawl_urls",
        "BFS-crawl N page bắt đầu từ 1 URL seed. Follow link cùng domain (mặc định). "
        "`follow_pattern` = regex lọc URL. Trả về list {url, status, title, text} — "
        "dùng cho static/HTML site. Cho SPA cần Chromium: browser_open + browser_read.",
        {
            "start_url": {"type": "string"},
            "max_pages": {"type": "integer", "description": "default 20, max 200"},
            "same_domain": {"type": "boolean", "description": "default True"},
            "follow_pattern": {"type": "string", "description": "regex e.g. '/blog/'"},
            "include_text": {"type": "boolean"},
            "max_text_chars": {"type": "integer"},
            "delay_ms": {"type": "integer", "description": "ms giữa mỗi request, default 300"},
        },
        ["start_url"],
    )
    _add(
        _extract_html, "extract_html",
        "CSS-selector extract từ URL hoặc raw HTML. Trả text + html + attrs cho mỗi "
        "match, hoặc chỉ 1 attribute nếu đưa `attribute`. Bulk pull structured data.",
        {
            "source": {"type": "string", "description": "URL hoặc raw HTML"},
            "selector": {"type": "string", "description": "CSS selector"},
            "attribute": {"type": "string", "description": "chỉ trả về 1 attr (href, src, ...)"},
            "max_matches": {"type": "integer", "description": "default 100"},
        },
        ["source", "selector"],
        risk="low",
    )
    _add(
        _extract_table, "extract_table",
        "Extract HTML `<table>` thành list-of-dicts (JSON). Auto-detect header row 0. "
        "Dùng cho Wikipedia infobox, giá bảng, số liệu tables trên news/finance sites.",
        {
            "source": {"type": "string", "description": "URL hoặc raw HTML"},
            "table_index": {"type": "integer", "description": "default 0 (first table)"},
            "header_row": {"type": "integer"},
        },
        ["source"],
        risk="low",
    )
    _add(
        _parse_sitemap, "parse_sitemap",
        "Parse sitemap.xml (hoặc sitemapindex) → list URL + lastmod. Tìm mọi URL công "
        "khai của 1 site trong 1 call, thay vì phải BFS crawl.",
        {
            "url": {"type": "string", "description": "URL tới sitemap.xml"},
            "max_urls": {"type": "integer", "description": "default 500, max 5000"},
        },
        ["url"],
        risk="low",
    )
    _add(
        _save_page_snapshot, "save_page_snapshot",
        "Download HTML + inline mọi <img>/<link>/<script> asset vào 1 folder — snapshot "
        "offline để phân tích. Trả về dir path + số asset đã save.",
        {
            "url": {"type": "string"},
            "save_dir": {"type": "string", "description": "default: ./snapshots/<domain>_<ts>/"},
        },
        ["url"],
    )
    _add(
        _download_file, "download_file",
        "Tải 1 file (PDF, ZIP, image, ...) từ URL về disk. Stream, max 50MB mặc định. "
        "Dùng khi cần file để pass tới tool khác (send_document, xlsx analyzer, ...).",
        {
            "url": {"type": "string"},
            "save_to": {"type": "string", "description": "absolute path; default: ./downloads/<basename>"},
            "max_mb": {"type": "integer", "description": "default 50"},
        },
        ["url"],
    )
    _add(
        _save_artifact, "save_artifact",
        "Lưu 1 file TEXT (CSV/JSON/MD/TXT/HTML) để user download qua URL. Auto add "
        "UTF-8 BOM cho CSV/text (Excel/tiếng Việt hiển thị đúng). Trả URL PUBLIC "
        "http://localhost:8766/artifacts/<filename> — LUÔN gửi URL này cho user, "
        "KHÔNG gửi localhost:1420 (Vite fallback → HTML rác). `filename` = tên file "
        "đơn giản, không path.",
        {
            "content": {"type": "string"},
            "filename": {"type": "string", "description": "e.g. articles.csv, report.md"},
            "encoding": {"type": "string", "description": "default utf-8"},
            "add_utf8_bom": {"type": "boolean", "description": "default true (fix Excel Vietnamese)"},
        },
        ["content", "filename"],
        risk="low",
    )
    _add(
        _save_csv, "save_csv",
        "Chuyển list rows (dict hoặc list) → CSV Excel-safe (UTF-8 BOM + CRLF + "
        "proper quoting) → lưu artifacts/ → trả URL public. ONE-SHOT thay cho "
        "manual CSV build + save_artifact. Rows = list of dicts (headers auto từ "
        "keys) hoặc list of lists (dùng `headers` param).",
        {
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "list of dicts hoặc list of arrays",
            },
            "filename": {"type": "string"},
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "optional, override / order columns",
            },
            "output_dir": {"type": "string", "description": "Thư mục lưu tùy chỉnh (để trống nếu dùng mặc định outputs/)"},
        },
        ["rows", "filename"],
        risk="low",
    )
    _add(
        _zip_folder, "zip_folder",
        "Nén toàn bộ một thư mục thành file .zip đặt tại outputs/zips/ và trả về "
        "đường dẫn file zip cùng link tải public (http://localhost:8766/outputs/zips/<name>.zip). "
        "Hỗ trợ tự động chia nhiều file zip nếu vượt quá max_zip_mb.",
        {
            "folder_path": {"type": "string", "description": "Đường dẫn thư mục cần nén"},
            "zip_filename": {"type": "string", "description": "Tên file zip (mặc định: <tên_thư_mục>.zip)"},
            "output_dir": {"type": "string", "description": "Thư mục lưu tùy chỉnh (để trống nếu dùng mặc định outputs/)"},
            "max_zip_mb": {"type": "integer", "description": "Dung lượng tối đa mỗi file zip MB, nếu vượt quá sẽ chia thành part01, part02... (mặc định 0: không chia)"},
        },
        ["folder_path"],
        risk="low",
    )
    _add(
        _download_media_and_zip, "download_media_and_zip",
        "Tải một danh sách URL ảnh/video/tài liệu về thư mục outputs/media/, tự động "
        "tính mã băm SHA-256 chống trùng file (chỉ lưu 1 bản duy nhất và ghi duplicate_of "
        "vào manifest.json), nén các file unique + manifest thành .zip (tự chia nhiều part nếu vượt max_zip_mb) "
        "và trả về danh sách URL tải trực tiếp.",
        {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Danh sách URL ảnh hoặc video cần tải",
            },
            "zip_filename": {"type": "string", "description": "Tên file zip xuất ra (ví dụ: product_images.zip)"},
            "folder_name": {"type": "string", "description": "Tên thư mục lưu tạm các media (tùy chọn)"},
            "max_files": {"type": "integer", "description": "Số lượng file tối đa (mặc định 100)"},
            "max_mb_per_file": {"type": "integer", "description": "Dung lượng tối đa mỗi file MB (mặc định 25)"},
            "output_dir": {"type": "string", "description": "Thư mục lưu tùy chỉnh (để trống nếu dùng mặc định outputs/)"},
            "max_zip_mb": {"type": "integer", "description": "Dung lượng tối đa mỗi file zip MB, tự chia thành part01, part02... nếu vượt quá"},
        },
        ["urls"],
        risk="low",
    )

    _add(
        _crawl_and_export_bundle, "crawl_and_export_bundle",
        "One-shot bundle export for scrape jobs with media: save rows as an Excel-safe CSV in "
        "outputs/csv/, find or accept image/video URLs, download them into outputs/media/<job_name>/, "
        "zip them into outputs/zips/<job_name>_media.zip (splitting into parts if exceeding max_zip_mb), then return CSV and ZIP links.",
        {
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Scraped data rows, usually a list of dictionaries",
            },
            "media_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional image/video URLs. If omitted, URLs are extracted from rows.",
            },
            "job_name": {"type": "string", "description": "Output slug, for example shopee_123"},
            "csv_filename": {"type": "string", "description": "CSV filename, defaults to <job_name>.csv"},
            "zip_filename": {"type": "string", "description": "ZIP filename, defaults to <job_name>_media.zip"},
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional CSV column order",
            },
            "output_dir": {"type": "string", "description": "Thư mục lưu tùy chỉnh (để trống nếu dùng mặc định outputs/)"},
            "max_zip_mb": {"type": "integer", "description": "Dung lượng tối đa mỗi file zip MB, tự chia thành part01, part02... nếu vượt quá"},
        },
        ["rows"],
        risk="low",
    )

    _add(
        _download_media_from_csv, "download_media_from_csv",
        "Đọc file CSV có sẵn, tự động tìm hoặc lọc theo các cột chứa URL ảnh/video, "
        "tải toàn bộ media về outputs/media/<job_name>/, chống trùng bằng SHA-256, "
        "tạo manifest.json, đóng gói file ZIP (hoặc chia part nếu vượt max_zip_mb) "
        "và tạo báo cáo Markdown tổng kết.",
        {
            "csv_path": {"type": "string", "description": "Đường dẫn file CSV (tuyệt đối hoặc tương đối hoặc trong outputs/csv/)"},
            "url_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tên các cột chứa URL ảnh/video (nếu để trống sẽ tự động quét mọi cột)",
            },
            "job_name": {"type": "string", "description": "Tên job xuất ra (mặc định lấy theo tên file CSV)"},
            "zip_filename": {"type": "string", "description": "Tên file ZIP xuất ra"},
            "output_dir": {"type": "string", "description": "Thư mục lưu tùy chỉnh (để trống nếu dùng mặc định outputs/)"},
            "max_zip_mb": {"type": "integer", "description": "Dung lượng tối đa mỗi file zip MB, tự chia part nếu vượt quá"},
            "max_files": {"type": "integer", "description": "Số lượng media tối đa (mặc định 500)"},
        },
        ["csv_path"],
        risk="low",
    )

    return tools
