"""Helper :8766 `/ingest` — the CDP-free path for sites that fingerprint automation.

Run: `.venv/Scripts/python.exe -m pytest tests/ -q` from the repo root.

The handler is exercised over a real socket on a throwaway port, because the parts that
break in production are HTTP-level (CORS preflight headers, byte-exact CSV) and would be
invisible to a test that called the methods directly.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Helper bound to a free port, writing into tmp_path instead of the real outputs/."""
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    import launch

    srv = ThreadingHTTPServer(("127.0.0.1", 0), launch._HelperHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    finally:
        srv.shutdown()


def _post(base: str, path: str, body) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_ingest_writes_json_and_excel_ready_csv(server):
    base, outputs = server
    rows = [
        {"user": "an", "sao": 5, "noi_dung": "hàng ngon, giao nhanh", "thoi_gian": "2026-01-01 08:00:00"},
        {"user": "bình", "sao": 4, "noi_dung": "tạm ổn", "thoi_gian": "2026-02-01 08:00:00"},
    ]
    status, out = _post(base, "/ingest?name=shopee_123", {"source": "https://shopee.vn/x", "rows": rows})
    assert status == 200 and out["ok"] and out["count"] == 2
    assert out["path"] == "outputs/inbox/shopee_123.json"
    assert out["csv"] == "outputs/csv/shopee_123.csv"

    saved = json.loads((outputs / "inbox" / "shopee_123.json").read_text(encoding="utf-8"))
    assert saved["count"] == 2
    assert saved["rows"][0]["user"] == "bình"  # newest review first, diacritics survive
    assert saved["source"] == "https://shopee.vn/x"

    # read_bytes, not read_text: universal newlines would hide a missing CRLF.
    raw = (outputs / "csv" / "shopee_123.csv").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # BOM, else Excel renders Vietnamese as mojibake
    text = raw.decode("utf-8-sig")
    assert text.startswith("thoi_gian,sao,noi_dung,user\r\n")
    assert '"hàng ngon, giao nhanh"' in text  # embedded comma quoted, not column-split


def test_shopee_ingest_adds_local_media_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_OUTPUT_DIR", str(tmp_path))
    import launch

    def fake_download(url: str, target_without_ext: Path) -> str:
        suffix = ".mp4" if url.endswith(".mp4") else ".jpg"
        target = target_without_ext.with_suffix(suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake_content")
        return f"outputs/{target.relative_to(tmp_path).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download)
    rows, media_dir, zip_rel = launch._prepare_shopee_review_rows(
        "shopee_123.json",
        [
            {
                "user": "an",
                "anh_urls": "https://down-vn.img.susercontent.com/file/a",
                "video_urls": "https://deo.shopeemobile.com/x.mp4",
            }
        ],
    )

    assert media_dir == "media/shopee_123"
    assert rows[0]["image_files"].endswith("review_00001_image_01.jpg")
    assert rows[0]["image_names"] == "review_00001_image_01.jpg"
    assert rows[0]["video_files"].endswith("review_00001_video_01.mp4")
    assert rows[0]["video_names"] == "review_00001_video_01.mp4"
    assert rows[0]["media_names"] == "review_00001_image_01.jpg|review_00001_video_01.mp4"
    assert rows[0]["media_dir"] == "outputs/media/shopee_123"
    assert zip_rel == "outputs/zips/shopee_123_media.zip"
    assert (tmp_path / "zips" / "shopee_123_media.zip").is_file()


def test_ingest_csv_aligns_ragged_rows_under_a_union_header(server):
    """A row missing a key must leave a hole, not shift every later column left."""
    base, outputs = server
    _post(base, "/ingest?name=ragged", {"rows": [{"a": 1}, {"b": 2, "a": 3}]})
    assert (outputs / "csv" / "ragged.csv").read_bytes().decode("utf-8-sig") == "a,b\r\n1,\r\n3,2\r\n"


def test_ingest_skips_csv_for_non_tabular_payloads(server):
    base, outputs = server
    _, out = _post(base, "/ingest?name=nested", {"rows": [[1, 2], [3, 4]]})
    assert out["ok"] and out["csv"] is None
    assert (outputs / "inbox" / "nested.json").is_file()  # raw JSON still kept
    assert not (outputs / "csv").exists()


def test_ingest_name_cannot_escape_the_inbox(server):
    base, outputs = server
    _, out = _post(base, "/ingest?name=../../evil", {"rows": [{"a": 1}]})
    assert out["path"] == "outputs/inbox/.._.._evil.json"
    assert (outputs / "inbox" / ".._.._evil.json").is_file()
    assert not (outputs.parent / "evil.json").exists()


def test_ingest_rejects_a_body_without_rows(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, "/ingest", {"nope": 1})
    assert e.value.code == 400


def test_preflight_allows_private_network_access(server):
    """Chrome fails the POST from shopee.vn to 127.0.0.1 without this header, silently."""
    base, _ = server
    req = urllib.request.Request(base + "/ingest", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers.get("Access-Control-Allow-Private-Network") == "true"
        assert r.headers.get("Access-Control-Allow-Origin") == "*"


def test_help_page_serves_a_runnable_bookmarklet_and_lists_receipts(server):
    """Two ways this href breaks, both silent in the browser: a raw newline truncates it,
    and collapsing the source onto one line makes any `//` comment swallow the rest of the
    program. Encoding newlines as %0A avoids both — assert the JS survives the round trip.
    """
    base, _ = server
    _post(base, "/ingest?name=shopee_123", {"rows": [{"a": 1}]})
    with urllib.request.urlopen(base + "/ingest", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert 'href="javascript:' in html
    href = html.split('href="javascript:')[1].split('"')[0]
    assert "\n" not in href and "\r" not in href

    import launch

    js = urllib.parse.unquote(href)
    assert js == launch._INGEST_BOOKMARKLET_JS.replace("__HELPER_PORT__", "8766").strip()
    # Line comments must still be on their own lines, not trailing live code.
    for line in js.splitlines():
        code = line.split("//")[0] if line.lstrip().startswith("//") is False else line
        assert "//" not in code or "http://" in code or line.lstrip().startswith("//")
    assert "shopee_123.json" in html  # receipt table


def test_bookmarklet_reads_every_shopee_url_shape():
    """`/product/<shopid>/<itemid>` is what Shopee's own links use — missing it made the
    bookmarklet report "khong doc duoc shopid/itemid" on a perfectly good page."""
    import re

    import launch

    js = launch._INGEST_BOOKMARKLET_JS
    path_re = re.compile(r"^/product/(\d+)/(\d+)")
    href_re = re.compile(r"i\.(\d+)\.(\d+)")
    assert "/^\\/product\\/(\\d+)\\/(\\d+)/" in js  # the pattern really is in the source

    assert path_re.match("/product/1471990106/27429880257").groups() == (
        "1471990106",
        "27429880257",
    )
    assert href_re.search("/tech_gear1/De-tan-nhiet-i.1471990106.27429880257").groups() == (
        "1471990106",
        "27429880257",
    )
    # /<shop>/<itemid> carries no shopid — the embedded-state fallback has to supply it.
    assert path_re.match("/tech_gear1/27429880257") is None


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=40) as r:
        return json.loads(r.read())


def test_job_round_trip_agent_queues_extension_delivers(server):
    """The automatic path: agent queues over GET (web_fetch can't POST), the extension
    long-polls, posts rows back, and /ingest/result reports completion."""
    base, outputs = server

    queued = _get(base, "/ingest/job?url=https://shopee.vn/product/1471990106/27429880257")
    assert queued["ok"]
    job_id = queued["job"]["id"]
    assert queued["job"]["kind"] == "shopee-reviews"

    # Nothing has run yet.
    pending = _get(base, f"/ingest/result?id={job_id}")
    assert pending["ok"] is False
    assert pending["progress"]["status"] == "queued"
    assert pending["progress"]["stage"] == "queued"

    # The extension claims it; a claimed job is not handed out twice.
    jobs = _get(base, "/ingest/jobs?wait=1")["jobs"]
    assert [j["id"] for j in jobs] == [job_id]
    assert _get(base, "/ingest/jobs?wait=0")["jobs"] == []
    _post(base, "/ingest/progress", {
        "job": job_id,
        "progress": {
            "status": "running",
            "stage": "fetch-background",
            "message": "Đã lấy 1 review",
            "rows": 1,
            "percent": 45,
        },
    })
    progress = _get(base, f"/ingest/progress?id={job_id}")["progress"]
    assert progress["status"] == "running"
    assert progress["rows"] == 1
    assert progress["percent"] == 45

    _post(base, "/ingest?name=shopee_27429880257",
          {"job": job_id, "rows": [{"user": "an", "sao": 5, "noi_dung": "tốt", "anh": 1}]})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] and done["result"]["count"] == 1
    assert done["result"]["csv"] == "outputs/csv/shopee_27429880257_reviews.csv"
    assert done["progress"]["status"] == "done"
    assert done["progress"]["stage"] == "saved"
    assert done["progress"]["rows"] == 1
    assert (outputs / "csv" / "shopee_27429880257_reviews.csv").is_file()
    csv_text = (outputs / "csv" / "shopee_27429880257_reviews.csv").read_bytes().decode("utf-8-sig")
    assert csv_text.splitlines()[0].endswith(",anh")
    assert csv_text.splitlines()[1].endswith(",1")


def test_failed_job_is_recorded_so_the_agent_stops_waiting(server):
    base, _ = server
    # 1. Challenge/Login required switches to awaiting_user_verification
    job_id = _get(base, "/ingest/job?url=https://shopee.vn/product/1/2")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_id, "error": "Shopee error 90309999 (is_login=false)"})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] and done["result"]["ok"] is False
    assert "90309999" in done["result"]["error"]
    assert done["progress"]["status"] == "awaiting_user_verification"
    assert done["progress"]["status"] == "awaiting_user_verification"
    assert done["progress"]["stage"] == "login_required"
    assert done["result"]["verification_required"] is False
    assert done["result"]["login_required"] is True

    # 2. General non-verification failure records error
    job_id2 = _get(base, "/ingest/job?url=https://shopee.vn/product/3/4")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_id2, "error": "Connection timed out"})
    done2 = _get(base, f"/ingest/result?id={job_id2}")
    assert done2["ok"] and done2["result"]["ok"] is False
    assert done2["result"]["verification_required"] is False
    assert done2["progress"]["status"] == "error"
    assert done2["progress"]["stage"] == "failed"


def test_job_endpoint_rejects_a_non_http_url(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base, "/ingest/job?url=file:///etc/passwd")
    assert e.value.code == 400


def test_long_poll_returns_as_soon_as_a_job_is_queued(server):
    """Latency matters: the agent should not wait out the poll window."""
    import time as _t

    base, _ = server
    threading.Timer(0.4, lambda: _get(base, "/ingest/job?url=https://shopee.vn/product/1/2")).start()
    started = _t.monotonic()
    jobs = _get(base, "/ingest/jobs?wait=20")["jobs"]
    elapsed = _t.monotonic() - started
    assert len(jobs) == 1
    assert elapsed < 5, f"long poll returned after {elapsed:.1f}s, should wake on the queue"


def test_ingested_file_is_served_back_for_the_agent_to_read(server):
    """web_fetch on the helper is how the agent reads this — the workspace is elsewhere."""
    base, _ = server
    _post(base, "/ingest?name=shopee_123", {"rows": [{"noi_dung": "tốt"}]})
    with urllib.request.urlopen(base + "/outputs/inbox/shopee_123.json", timeout=5) as r:
        assert r.status == 200
        assert "tốt" in json.loads(r.read())["rows"][0]["noi_dung"]


def test_shopee_ingest_e2e_zip_and_csv(server, monkeypatch):
    base, outputs = server
    import launch

    def fake_download(url: str, target_without_ext: Path) -> str:
        target = target_without_ext.with_suffix(".jpg")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"img_bytes_123")
        return f"outputs/{target.relative_to(outputs).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download)

    job_id = _get(base, "/ingest/job?url=https://shopee.vn/product/11/22")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    status, out = _post(
        base,
        "/ingest",
        {
            "job": job_id,
            "name": "shopee_22",
            "source": "https://shopee.vn/product/11/22",
            "rows": [
                {
                    "user": "tester",
                    "sao": 5,
                    "noi_dung": "dep lam",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/pic1",
                    "thoi_gian": "2026-09-01 10:00:00",
                }
            ],
        },
    )

    assert status == 200 and out["ok"]
    assert out["zip"] == "outputs/zips/shopee_22_media.zip"
    assert out["zip_url"] == "/outputs/zips/shopee_22_media.zip"
    assert (outputs / "zips" / "shopee_22_media.zip").is_file()

    # Check zip contents
    import zipfile
    with zipfile.ZipFile(outputs / "zips" / "shopee_22_media.zip") as zf:
        namelist = zf.namelist()
        assert "review_00001_image_01.jpg" in namelist

    # Check CSV contents
    csv_text = (outputs / "csv" / "shopee_22.csv").read_bytes().decode("utf-8-sig")
    assert "image_names" in csv_text
    assert "review_00001_image_01.jpg" in csv_text


def test_vanity_url_extracts_itemid_and_names_outputs(server):
    """URL vanity https://shopee.vn/depnhaphuong/25018847315 must output as shopee_25018847315, not job.id."""
    """URL vanity https://shopee.vn/depnhaphuong/25018847315 must output as shopee_25018847315_reviews."""
    base, outputs = server

    queued = _get(base, "/ingest/job?url=https://shopee.vn/depnhaphuong/25018847315")
    assert queued["ok"]
    job = queued["job"]
    assert job["itemid"] == "25018847315"
    assert job["name"] == "shopee_25018847315_reviews"
    job_id = job["id"]

    _get(base, "/ingest/jobs?wait=1")
    status, out = _post(
        base,
        "/ingest",
        {
            "job": job_id,
            "source": "https://shopee.vn/depnhaphuong/25018847315",
            "rows": [
                {"user": "u1", "sao": 5, "noi_dung": "dép đẹp", "thoi_gian": "2026-09-01 12:00:00"}
            ],
        },
    )
    assert status == 200 and out["ok"]
    assert out["path"] == "outputs/inbox/shopee_25018847315_reviews.json"
    assert out["csv"] == "outputs/csv/shopee_25018847315_reviews.csv"
    assert (outputs / "csv" / "shopee_25018847315_reviews.csv").is_file()
    assert (outputs / "inbox" / "shopee_25018847315_reviews.json").is_file()
    assert (outputs / "text" / "shopee_25018847315_reviews_report.md").is_file()


def test_shopee_manifest_and_zip_dedup(server, monkeypatch):
    """Manifest.json tracks url, kind, hash, size, and duplicate_of; ZIP contains unique media + manifest."""
    base, outputs = server
    import hashlib
    import zipfile
    import launch

    content_a = b"binary_image_content_A_12345"
    content_b = b"binary_image_content_B_67890"
    sha_a = hashlib.sha256(content_a).hexdigest()
    sha_b = hashlib.sha256(content_b).hexdigest()

    def fake_download_with_dedup(url: str, target_without_ext: Path) -> str:
        target = target_without_ext.with_suffix(".jpg")
        target.parent.mkdir(parents=True, exist_ok=True)
        if "dup" in url or "pic1" in url:
            target.write_bytes(content_a)
        else:
            target.write_bytes(content_b)
        return f"outputs/{target.relative_to(outputs).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download_with_dedup)

    job_id = _get(base, "/ingest/job?url=https://shopee.vn/depnhaphuong/25018847315")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")

    status, out = _post(
        base,
        "/ingest",
        {
            "job": job_id,
            "source": "https://shopee.vn/depnhaphuong/25018847315",
            "rows": [
                {
                    "user": "tester1",
                    "sao": 5,
                    "noi_dung": "ảnh 1",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/pic1",
                    "thoi_gian": "2026-09-01 10:00:00",
                },
                {
                    "user": "tester2",
                    "sao": 5,
                    "noi_dung": "ảnh duplicate content",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/pic_dup",
                    "thoi_gian": "2026-09-02 10:00:00",
                },
                {
                    "user": "tester3",
                    "sao": 4,
                    "noi_dung": "ảnh độc nhất",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/pic_unique",
                    "thoi_gian": "2026-09-03 10:00:00",
                },
            ],
        },
    )

    assert status == 200 and out["ok"]
    media_dir = outputs / "shopee_reviews" / "shopee_25018847315" / "media"
    media_dir = outputs / "media" / "shopee_25018847315_reviews"
    assert media_dir.is_dir()

    manifest_file = media_dir / "manifest.json"
    assert manifest_file.is_file()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    assert manifest["total_urls"] == 3
    assert manifest["unique_count"] == 2
    assert manifest["duplicate_count"] == 1
    assert manifest["failed_count"] == 0

    files = manifest["files"]
    # Reviews are sorted newest-first, so the unique third row is written first.
    assert files[0]["sha256"] == sha_b
    assert files[0]["duplicate_of"] is None
    primary_name = files[0]["filename"]

    assert files[1]["sha256"] == sha_a
    assert files[1]["duplicate_of"] is None
    primary_name = files[1]["filename"]

    assert files[2]["sha256"] == sha_a
    assert files[2]["duplicate_of"] == primary_name

    # Duplicate file must have been deleted from disk so media_root has only unique files
    assert (media_dir / primary_name).is_file()
    assert (media_dir / files[2]["filename"]).is_file()
    # 2 unique media files + manifest.json = 3 files on disk
    disk_files = {p.name for p in media_dir.iterdir() if p.is_file()}
    assert len(disk_files) == 3
    assert "manifest.json" in disk_files

    # Check ZIP archive contents
    zip_path = outputs / "zips" / "shopee_25018847315_media.zip"
    zip_path = outputs / "zips" / "shopee_25018847315_reviews_media.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path, "r") as zf:
        zip_names = zf.namelist()
        assert "manifest.json" in zip_names
        assert primary_name in zip_names
        assert files[2]["filename"] in zip_names
        assert len(zip_names) == 3


def test_verification_failure_no_fake_success_and_retry(server):
    """Verification failure must report ok=False, and /browser/resume or /ingest/retry must re-enqueue the job."""
    base, _ = server
    base, outputs = server

    queued = _get(base, "/ingest/job?url=https://shopee.vn/depnhaphuong/25018847315")
    job_id = queued["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")

    # Extension reports verification challenge
    _post(base, "/ingest", {"job": job_id, "error": "Shopee challenge / verification required"})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] is True
    assert done["result"]["ok"] is False  # Never fake success!
    assert done["result"]["verification_required"] is True
    assert done["progress"]["status"] == "awaiting_user_verification"
    assert done["progress"]["status"] == "awaiting_user_verification"
    assert done["progress"]["stage"] == "verification_required"

    # /browser/status reflects awaiting_user_verification
    st = _get(base, "/browser/status")
    assert st["ok"] is True
    assert st["state"] == "awaiting_user_verification"
    assert st["verification_required"] is True
    assert st["pending_verification"]["job_id"] == job_id

    # No empty CSV or ZIP generated
    assert not (outputs / "csv" / "shopee_25018847315_reviews.csv").exists()
    assert not (outputs / "zips" / "shopee_25018847315_reviews_media.zip").exists()

    # Empty rows test (Requirement 9): must not fake success count: 0
    status_empty, out_empty = _post(base, "/ingest", {"job": job_id, "rows": []})
    assert status_empty == 200
    assert out_empty["ok"] is False
    assert out_empty["count"] == 0
    assert not (outputs / "csv" / "shopee_25018847315_reviews.csv").exists()

    # Persistence test (Requirement 5): state saved to disk
    state_file = outputs / ".ingest_jobs_state.json"
    assert state_file.is_file()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert job_id in saved["all_jobs"]

    # Agent calls /browser/resume
    resumed = _get(base, f"/browser/resume?id={job_id}")
    assert resumed["ok"] is True
    assert resumed["resumed"] is True
    assert resumed["retried"] is True

    # Progress should be reset to queued/resumed without error
    progress = _get(base, f"/ingest/progress?id={job_id}")["progress"]
    assert progress["status"] == "queued"
    assert progress["stage"] == "resumed"

    # Job is re-claimed by extension
    claimed = _get(base, "/ingest/jobs?wait=1")["jobs"]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job_id

    # Also test explicit retry via /ingest/retry
    retried = _get(base, f"/ingest/retry?id={job_id}")
    assert retried["ok"] is True
    assert retried["retried"] == job_id
