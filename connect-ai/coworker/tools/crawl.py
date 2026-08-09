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

import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
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


def _output_subdir(kind: str) -> Path:
    """Get (and auto-create) a subdirectory under the output root."""
    kind = re.sub(r"[^a-z0-9_-]+", "", (kind or "misc").lower()) or "misc"
    d = _output_root() / kind
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
    target = _output_subdir(kind) / filename
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
    r = _save_artifact(csv_text, filename, encoding="utf-8", add_utf8_bom=True)
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
        },
        ["rows", "filename"],
        risk="low",
    )

    return tools
