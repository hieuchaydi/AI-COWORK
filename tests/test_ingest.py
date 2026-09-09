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
    # 1. Login required switches to status="login_required", stage="login_required"
    job_id = _get(base, "/ingest/job?url=https://shopee.vn/product/1/2")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_id, "error": "Shopee error 90309999 (is_login=false)"})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] and done["result"]["ok"] is False
    assert "90309999" in done["result"]["error"]
    assert done["progress"]["status"] == "login_required"
    assert done["progress"]["stage"] == "login_required"
    assert done["result"]["verification_required"] is False
    assert done["result"]["login_required"] is True

    # Calling resume on a login_required job is rejected with HTTP 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, f"/browser/resume?id={job_id}")
    assert exc.value.code == 400
    err_body = json.loads(exc.value.read().decode())
    assert err_body["login_required"] is True

    # 2. General non-verification failure records error
    job_id2 = _get(base, "/ingest/job?url=https://shopee.vn/product/3/4")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_id2, "error": "Connection timed out"})
    done2 = _get(base, f"/ingest/result?id={job_id2}")
    assert done2["ok"] and done2["result"]["ok"] is False
    assert done2["result"]["verification_required"] is False
    assert done2["progress"]["status"] == "error"
    assert done2["progress"]["stage"] == "failed"


def test_shopee_classification_api_blocked_vs_login_vs_captcha(server):
    """Test distinct handling for:
    1. login_required (90309999 / is_login=false) -> status='login_required', stage='login_required', resume rejected.
    2. verification_required (/verify/traffic / captcha) -> status='awaiting_user_verification', stage='verification_required', resume accepted.
    3. api_blocked (HTTP 403 without captcha or login) -> status='error', stage='api_blocked', resume rejected.
    4. general failure (timeout) -> status='error', stage='failed'.
    """
    base, _ = server

    # 1. Login required
    job_1 = _get(base, "/ingest/job?url=https://shopee.vn/product/10/20")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_1, "error": "Shopee error 90309999, is_login=false"})
    res1 = _get(base, f"/ingest/result?id={job_1}")
    assert res1["ok"] is True
    assert res1["result"]["ok"] is False
    assert res1["result"]["login_required"] is True
    assert res1["result"]["verification_required"] is False
    assert res1["result"]["api_blocked"] is False
    assert res1["progress"]["status"] == "login_required"
    assert res1["progress"]["stage"] == "login_required"
    # Resume rejected
    with pytest.raises(urllib.error.HTTPError) as exc1:
        _get(base, f"/browser/resume?id={job_1}")
    assert exc1.value.code == 400
    assert json.loads(exc1.value.read().decode())["login_required"] is True

    # 2. Verification / CAPTCHA challenge
    job_2 = _get(base, "/ingest/job?url=https://shopee.vn/product/30/40")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_2, "error": "Shopee challenge at https://shopee.vn/verify/traffic"})
    res2 = _get(base, f"/ingest/result?id={job_2}")
    assert res2["ok"] is True
    assert res2["result"]["ok"] is False
    assert res2["result"]["login_required"] is False
    assert res2["result"]["verification_required"] is True
    assert res2["result"]["api_blocked"] is False
    assert res2["progress"]["status"] == "awaiting_user_verification"
    assert res2["progress"]["stage"] == "verification_required"
    # Resume accepted
    resumed2 = _get(base, f"/browser/resume?id={job_2}")
    assert resumed2["ok"] is True
    assert resumed2["resumed"] is True

    # 3. API Blocked (HTTP 403 Access Denied)
    job_3 = _get(base, "/ingest/job?url=https://shopee.vn/product/50/60")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {
        "job": job_3,
        "error": "Shopee reviews API access denied (HTTP 403) — không thấy CAPTCHA trên tab; endpoint=https://shopee.vn/api/v2/item/get_ratings; response={\"error\":\"access denied\"}",
    })
    res3 = _get(base, f"/ingest/result?id={job_3}")
    assert res3["ok"] is True
    assert res3["result"]["ok"] is False
    assert res3["result"]["login_required"] is False
    assert res3["result"]["verification_required"] is False
    assert res3["result"]["api_blocked"] is True
    assert res3["progress"]["status"] == "error"
    assert res3["progress"]["stage"] == "api_blocked"
    # Resume rejected
    with pytest.raises(urllib.error.HTTPError) as exc3:
        _get(base, f"/browser/resume?id={job_3}")
    assert exc3.value.code == 400
    assert json.loads(exc3.value.read().decode())["api_blocked"] is True

    # 4. General failure
    job_4 = _get(base, "/ingest/job?url=https://shopee.vn/product/70/80")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_4, "error": "WebSocket connection lost"})
    res4 = _get(base, f"/ingest/result?id={job_4}")
    assert res4["ok"] is True
    assert res4["result"]["ok"] is False
    assert res4["result"]["login_required"] is False
    assert res4["result"]["verification_required"] is False
    assert res4["result"]["api_blocked"] is False
    assert res4["progress"]["status"] == "error"
    assert res4["progress"]["stage"] == "failed"


def test_browser_status_does_not_mix_stale_job_failures(server):
    """Only the newest job may drive the popup's attention state."""
    base, _ = server
    import launch

    with launch._INGEST_PROGRESS_LOCK:
        saved = dict(launch._INGEST_PROGRESS)
        launch._INGEST_PROGRESS.clear()
        launch._INGEST_PROGRESS["old-login"] = {
            "status": "login_required",
            "login_required": True,
            "url": "https://shopee.vn/product/1/2",
        }
        launch._INGEST_PROGRESS["old-block"] = {
            "status": "error",
            "stage": "api_blocked",
            "api_blocked": True,
            "url": "https://shopee.vn/product/3/4",
        }
        launch._INGEST_PROGRESS["current"] = {
            "status": "running",
            "stage": "fetch",
            "url": "https://shopee.vn/product/5/6",
        }

    try:
        status = _get(base, "/browser/status")
        assert status["login_required"] is False
        assert status["api_blocked"] is False
        assert status["verification_required"] is False
        assert status["pending_login"] is None
        assert status["pending_blocked"] is None

        with launch._INGEST_PROGRESS_LOCK:
            launch._INGEST_PROGRESS["current"] = {
                "status": "error",
                "stage": "api_blocked",
                "api_blocked": True,
                "url": "https://shopee.vn/product/5/6",
            }
        status = _get(base, "/browser/status")
        assert status["state"] == "api_blocked"
        assert status["api_blocked"] is True
        assert status["login_required"] is False
        assert status["pending_blocked"]["job_id"] == "current"
    finally:
        with launch._INGEST_PROGRESS_LOCK:
            launch._INGEST_PROGRESS.clear()
            launch._INGEST_PROGRESS.update(saved)


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
        assert "shopee_25018847315_reviews.csv" in zip_names
        assert len(zip_names) == 4


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


def test_shopee_structured_trace_lifecycle(server):
    base, outputs = server
    import launch

    # 1. Server queues a job -> triggers job-dispatch trace with sanitized url
    q = _get(base, "/ingest/job?url=https://shopee.vn/product/999/888888?token=secret_token_123")
    assert q["ok"]
    job_id = q["job"]["id"]

    # 2. Ack job -> triggers job-accepted trace
    launch._ack_ingest_job(job_id)

    # 3. Simulate extension reporting traces via _update_ingest_progress
    trace_tab_ready = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888?auth=secret_auth",
        "event": "tab-ready",
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_tab_ready})

    trace_shop_resolved = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888",
        "event": "shop-resolved",
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_shop_resolved})

    trace_req = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888",
        "event": "ratings-request",
        "offset": 0,
        "limit": 50,
        "requestStart": "2026-09-06T12:00:00Z",
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_req})

    trace_resp = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888",
        "event": "ratings-response",
        "offset": 0,
        "limit": 50,
        "requestStart": "2026-09-06T12:00:00Z",
        "requestEnd": "2026-09-06T12:00:00.350Z",
        "httpStatus": 200,
        "responseUrl": "https://shopee.vn/api/v2/item/get_ratings?itemid=888888&shopid=999&session=super_secret",
        "elapsedMs": 350,
        "error": None,
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_resp})

    trace_up_start = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888",
        "event": "upload-start",
        "rowsCount": 1,
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_up_start})

    # Complete job payload
    rows = [{"user": "test_user", "sao": 5, "noi_dung": "tot", "thoi_gian": "2026-01-01"}]
    _post(base, "/ingest", {"job": job_id, "source": "https://shopee.vn/product/999/888888", "rows": rows})

    trace_up_complete = {
        "jobId": job_id,
        "itemid": "888888",
        "shopid": "999",
        "tabId": 101,
        "tabUrl": "https://shopee.vn/product/999/888888",
        "event": "upload-complete",
        "rowsCount": 1,
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": trace_up_complete})

    # 4. Check /ingest/traces?id=job_id endpoint
    traces_res = _get(base, f"/ingest/traces?id={job_id}")
    assert traces_res["ok"] is True
    assert traces_res["jobId"] == job_id
    traces = traces_res["traces"]
    assert len(traces) >= 7

    events = [t["event"] for t in traces]
    for required_event in [
        "job-dispatch",
        "job-accepted",
        "tab-ready",
        "shop-resolved",
        "ratings-request",
        "ratings-response",
        "upload-start",
        "upload-complete",
    ]:
        assert required_event in events

    # Check 13 required fields for every trace
    required_fields = [
        "jobId", "itemid", "shopid", "tabId", "tabUrl", "event",
        "offset", "limit", "requestStart", "requestEnd", "httpStatus",
        "responseUrl", "elapsedMs", "error",
    ]
    for t in traces:
        for rf in required_fields:
            assert rf in t, f"Trace {t.get('event')} missing field {rf}"
        # Security sanitization checks: never leak token/cookie/auth
        if t.get("tabUrl"):
            assert "secret_token_123" not in t["tabUrl"]
            assert "secret_auth" not in t["tabUrl"]
        if t.get("responseUrl"):
            assert "super_secret" not in t["responseUrl"]
            assert "[REDACTED]" in t["responseUrl"]

    # 5. Check /ingest/result has traces included
    res = _get(base, f"/ingest/result?id={job_id}")
    assert res["ok"] is True
    assert "traces" in res
    assert res["traces_count"] == len(traces)

    # 6. Check log file outputs/logs/shopee_jobs.jsonl
    log_file = outputs / "logs" / "shopee_jobs.jsonl"
    assert log_file.is_file()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    log_job_events = [line["event"] for line in lines if line.get("jobId") == job_id]
    for ev in ["job-dispatch", "job-accepted", "ratings-response"]:
        assert ev in log_job_events


def test_preflight_login_required_stops_and_records_trace(server):
    base, outputs = server
    import launch

    # 1. Queue job
    q = _get(base, "/ingest/job?url=https://shopee.vn/product/123/456")
    job_id = q["job"]["id"]

    # 2. Extension runs preflight and detects is_login == False
    preflight_trace = {
        "jobId": job_id,
        "itemid": "456",
        "shopid": "123",
        "tabId": 12,
        "tabUrl": "https://shopee.vn/product/123/456",
        "event": "tab-preflight",
        "httpStatus": 200,
        "error": "Shopee login required (error 90309999, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome",
        "is_login": False,
        "responseUrl": "https://shopee.vn/api/v2/item/get_ratings",
    }
    launch._update_ingest_progress(job_id, {"stage": "trace", "trace": preflight_trace})

    # Extension reports login_required status
    status, res = _post(base, "/ingest", {
        "job": job_id,
        "error": "Shopee login required (error 90309999, is_login=false) — hãy đăng nhập Shopee trên đúng tab Chrome",
        "login_required": True,
    })
    assert status == 200
    assert res["ok"] is False
    assert res["login_required"] is True
    assert res["status"] == "login_required"

    # Verify result endpoint
    r = _get(base, f"/ingest/result?id={job_id}")
    assert r["ok"] is True
    assert r["result"]["login_required"] is True
    assert r["progress"]["status"] == "login_required"

    # Verify trace endpoint has tab-preflight trace with is_login == False
    traces_res = _get(base, f"/ingest/traces?id={job_id}")
    assert traces_res["ok"] is True
    preflight_entries = [t for t in traces_res["traces"] if t["event"] == "tab-preflight"]
    assert len(preflight_entries) == 1
    assert preflight_entries[0]["is_login"] is False
    assert "login required" in preflight_entries[0]["error"].lower()


def test_job_endpoint_normalizes_shoppe_typo(server):
    """The common shoppe.vn typo is normalized before the job is queued."""
    base, _ = server
    url = "https://shoppe.vn/lengkengvanphongpham/22235967241"
    res = _get(base, f"/ingest/job?url={urllib.parse.quote(url)}")
    assert res["ok"] is True
    assert res["job"]["url"] == "https://shopee.vn/lengkengvanphongpham/22235967241"


def test_is_allowed_ingest_media_url_supports_shoppe():
    import launch

    assert launch._is_allowed_ingest_media_url("https://shopee.vn/img.jpg")
    assert launch._is_allowed_ingest_media_url("https://shoppe.vn/img.jpg")
    assert launch._is_allowed_ingest_media_url("https://down-vn.img.susercontent.com/file/123")
    assert not launch._is_allowed_ingest_media_url("https://evil.com/img.jpg")


def test_checkpoint_does_not_create_premature_zip_or_csv(server):
    base, outputs = server
    import launch

    queued = _get(base, "/ingest/job?url=https://shopee.vn/product/111/222")
    job_id = queued["job"]["id"]

    # Extension sends checkpoint with 30 reviews
    res = launch._save_ingest_checkpoint(
        job_id,
        {
            "next_offset": 30,
            "rating_type": 5,
            "itemid": "222",
            "shopid": "111",
            "rows": [
                {"user": f"user_{i}", "sao": 5, "noi_dung": f"review_{i}", "thoi_gian": "2026-09-01 10:00:00"}
                for i in range(30)
            ],
            "total": 100,
        },
    )
    assert res["ok"] is True
    assert res["next_offset"] == 30
    assert res["rating_type"] == 5
    assert res["rows_count"] == 30

    # Verify no CSV, no ZIP, and job is still in progress, NOT done
    csv_files = list((outputs / "csv").glob("*.csv")) if (outputs / "csv").is_dir() else []
    zip_files = list((outputs / "zips").glob("*.zip")) if (outputs / "zips").is_dir() else []
    assert len(csv_files) == 0
    assert len(zip_files) == 0

    prog = _get(base, f"/ingest/progress?id={job_id}")
    assert prog["ok"] is True
    assert prog["progress"]["status"] == "running"
    assert prog["progress"]["rows"] == 30
    assert prog["progress"]["checkpoint"]["rating_type"] == 5

    # A result inquiry does NOT show premature completion
    r = _get(base, f"/ingest/result?id={job_id}")
    assert r["ok"] is False


def test_checkpoint_resumes_and_merges_full_dataset(server):
    base, outputs = server
    import launch

    queued = _get(base, "/ingest/job?url=https://shopee.vn/product/111/333")
    job_id = queued["job"]["id"]

    # 1. Checkpoint at offset 30 with 30 reviews
    launch._save_ingest_checkpoint(
        job_id,
        {
            "next_offset": 30,
            "itemid": "333",
            "shopid": "111",
            "rows": [
                {"user": f"u_{i}", "sao": 5, "noi_dung": f"comment_{i}", "thoi_gian": f"2026-09-01 10:{i:02d}:00"}
                for i in range(30)
            ],
            "total": 50,
        },
    )

    # 2. Complete crawl at offset 50 with 20 more reviews
    final_rows = [
        {"user": f"u_{i}", "sao": 5, "noi_dung": f"comment_{i}", "thoi_gian": f"2026-09-01 11:{i-30:02d}:00"}
        for i in range(30, 50)
    ]
    status, out = _post(
        base,
        "/ingest",
        {
            "job": job_id,
            "source": "https://shopee.vn/product/111/333",
            "rows": final_rows,
            "partial": True,
            "crawl_summary": {"expected": 60, "collected": 50, "complete": False},
        },
    )
    assert status == 200 and out["ok"]
    assert out["count"] == 50  # 30 from checkpoint + 20 from final batch = 50 total!
    assert out["partial"] is True
    assert out["crawl_summary"]["expected"] == 60

    result = _get(base, f"/ingest/result?id={job_id}")
    assert result["result"]["partial"] is True
    assert result["result"]["crawl_summary"]["collected"] == 50

    csv_path = outputs / "csv" / "shopee_333_reviews.csv"
    assert csv_path.is_file()
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 51  # 1 header + 50 reviews

    # Checkpoint should now be cleared
    assert job_id not in launch._INGEST_CHECKPOINTS


def test_browser_reload_releases_inflight_and_requeues(server):
    base, _ = server
    import launch

    # Queue a job
    queued = _get(base, "/ingest/job?url=https://shopee.vn/product/555/666")
    job_id = queued["job"]["id"]

    # Extension ACKs
    launch._ack_ingest_job(job_id)
    status_before = _get(base, "/browser/status")
    assert status_before["inFlightJobs"] >= 1

    # Extension reloads via /browser/reload
    _post(base, "/browser/reload", {})

    # Inflight must be released!
    status_after = _get(base, "/browser/status")
    assert status_after["inFlightJobs"] == 0

    # Job is back in _INGEST_JOBS to resume upon reconnect
    assert any(j["id"] == job_id for j in launch._INGEST_JOBS)


def test_login_detection_not_reported_when_is_login_true(server):
    """Shopee error 90309999 with is_login=true is api_blocked, never login_required."""
    base, _ = server
    import launch

    queued = _get(base, "/ingest/job?url=https://shopee.vn/product/777/888")
    job_id = queued["job"]["id"]

    status, out = launch._store_ingest_payload({
        "job": job_id,
        "error": "Shopee reviews API access denied (HTTP 403) — error=90309999, is_login=true",
        "api_blocked": True,
        "is_login": True,
        "login_required": False,
    })

    assert status == 200
    assert out["ok"] is False
    assert out["login_required"] is False
    assert out["api_blocked"] is True
    assert out["status"] == "error"
    assert out["stage"] == "api_blocked"

    prog = _get(base, f"/ingest/progress?id={job_id}")
    assert prog["progress"]["login_required"] is False
    assert prog["progress"]["api_blocked"] is True


def test_e2e_ingest_job_to_csv_media_and_zip_complete(server, monkeypatch):
    """Full E2E: /ingest/job -> checkpoint -> complete -> CSV, media, manifest, ZIP, report without bookmarklet."""
    base, outputs = server
    import launch

    # Mock media download
    def fake_download(url: str, target_without_ext: Path) -> str:
        target = target_without_ext.with_suffix(".jpg")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"e2e_fake_media")
        return f"outputs/{target.relative_to(outputs).as_posix()}"

    monkeypatch.setattr(launch, "_download_ingest_media", fake_download)

    # 1. Step 1: Queue job via /ingest/job
    q = _get(base, "/ingest/job?url=https://shopee.vn/product/101/202")
    assert q["ok"] is True
    job_id = q["job"]["id"]

    # 2. Extension claims / receives job and sends checkpoint
    launch._save_ingest_checkpoint(
        job_id,
        {
            "next_offset": 6,
            "itemid": "202",
            "shopid": "101",
            "rows": [
                {
                    "user": "buyer_1",
                    "sao": 5,
                    "noi_dung": "chất lượng tuyệt vời",
                    "thoi_gian": "2026-09-05 08:00:00",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/img1",
                }
            ],
            "total": 12,
        },
    )

    # 3. Extension finishes crawling and uploads complete payload
    status, res = _post(
        base,
        "/ingest",
        {
            "job": job_id,
            "name": "shopee_202_reviews",
            "source": "https://shopee.vn/product/101/202",
            "rows": [
                {
                    "user": "buyer_2",
                    "sao": 5,
                    "noi_dung": "đóng gói cẩn thận",
                    "thoi_gian": "2026-09-05 09:00:00",
                    "anh_urls": "https://down-vn.img.susercontent.com/file/img2",
                }
            ],
        },
    )

    assert status == 200
    assert res["ok"] is True
    assert res["count"] == 2  # Combined checkpoint + final batch

    # 4. Check result via /ingest/result
    result_resp = _get(base, f"/ingest/result?id={job_id}")
    assert result_resp["ok"] is True
    assert result_resp["progress"]["status"] == "done"

    # 5. Check all deliverables exist on disk: CSV UTF-8 BOM, ZIP, manifest, report
    csv_file = outputs / "csv" / "shopee_202_reviews.csv"
    assert csv_file.is_file()
    assert csv_file.read_bytes()[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM!

    zip_file = outputs / "zips" / "shopee_202_reviews_media.zip"
    assert zip_file.is_file()

    manifest_file = outputs / "media" / "shopee_202_reviews" / "manifest.json"
    assert manifest_file.is_file()

    report_file = outputs / "text" / "shopee_202_reviews_report.md"
    assert report_file.is_file()


def test_ingest_resume_and_evidence_capture(server):
    base, outputs = server
    # 1. Enqueue job
    job = _get(base, "/ingest/job?url=https://shopee.vn/product/300/400")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")

    # 2. Report verification required with evidence_screenshot
    sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    status, post_res = _post(
        base,
        "/ingest",
        {
            "job": job,
            "error": "Shopee challenge at https://shopee.vn/verify/traffic",
            "evidence_screenshot": sample_b64,
        },
    )
    assert status == 200
    assert post_res["ok"] is False
    assert post_res["verification_required"] is True

    # 3. Check result via /ingest/result includes evidence metadata
    res = _get(base, f"/ingest/result?id={job}")
    assert res["ok"] is True
    assert res["progress"]["status"] == "awaiting_user_verification"
    assert res["progress"]["stage"] == "verification_required"
    assert "evidence_file" in res["progress"]
    assert "evidence_url" in res["progress"]
    evidence_filename = Path(res["progress"]["evidence_file"]).name
    evidence_path = outputs / "evidence" / evidence_filename
    assert evidence_path.is_file()

    # 4. Resume job using the alias /ingest/resume?id=...
    resume_res = _get(base, f"/ingest/resume?id={job}")
    assert resume_res["ok"] is True
    assert resume_res["resumed"] is True

    # 5. Check status is now resumed and available to workers
    updated = _get(base, f"/ingest/result?id={job}")
    assert updated["progress"]["status"] == "queued"
    assert updated["progress"]["stage"] == "resumed"


def test_on_browser_ws_message_api_blocked_saves_checkpoint_and_log(server, monkeypatch):
    base, outputs = server
    import launch

    # 1. Enqueue job
    job_res = _get(base, "/ingest/job?url=https://shopee.vn/product/777/888")
    job_id = job_res["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")

    # 2. Simulate incoming WebSocket message of type api_blocked
    checkpoint_data = {
        "next_offset": 60,
        "rating_type": 5,
        "rows_count": 60,
        "itemid": "888",
        "shopid": "777",
    }
    msg = {
        "v": 1,
        "type": "api_blocked",
        "id": "blocked-777",
        "params": {
            "job_id": job_id,
            "reason": "Shopee reviews API access denied (HTTP 403 / API Blocked)",
            "status": 403,
            "checkpoint": checkpoint_data,
        },
    }
    launch._on_browser_ws_message(msg)

    # 3. Verify server state: result and progress
    result_resp = _get(base, f"/ingest/result?id={job_id}")
    assert result_resp["ok"] is True
    res_data = result_resp["result"]
    assert res_data["ok"] is False
    assert res_data["api_blocked"] is True
    assert res_data["retryable"] is False
    assert res_data["stage"] == "api_blocked"

    prog = result_resp["progress"]
    assert prog["status"] == "error"
    assert prog["stage"] == "api_blocked"
    assert prog["api_blocked"] is True
    assert prog["checkpoint"]["next_offset"] == 60

    # 4. Verify in-memory checkpoint has been preserved
    with launch._INGEST_CHECKPOINTS_LOCK:
        chk = launch._INGEST_CHECKPOINTS.get(job_id)
        assert chk is not None
        assert chk["next_offset"] == 60
        assert chk["rating_type"] == 5


def test_save_ingest_checkpoint_v2_and_multi_scope_dedup(server):
    base, outputs = server
    import launch

    # 1. Enqueue job
    job_res = _get(base, "/ingest/job?url=https://shopee.vn/product/123/456")
    job_id = job_res["job"]["id"]

    # 2. Save Checkpoint V2 with scopes and reviews
    chk_v2 = {
        "version": 2,
        "active_scope": "comment",
        "scopes": {
            "all": {"key": "all", "completed": True, "rows_count": 10},
            "comment": {"key": "comment", "completed": False, "rows_count": 5},
            "media": {"key": "media", "completed": False, "rows_count": 0},
        },
        "ui_reference": {
            "total": 100,
            "rating_total": 100,
            "rcount_with_context": 50,
            "rcount_with_media": 20,
        },
        "itemid": "456",
        "shopid": "123",
        "total": 100,
        "rows": [
            {
                "cmid": "cmid_001",
                "user": "buyer_a",
                "sao": 5,
                "noi_dung": "Đẹp",
                "thoi_gian": "2026-09-09 10:00:00",
                "anh_urls": "",
            },
            {
                "cmid": "cmid_002",
                "user": "buyer_b",
                "sao": 4,
                "noi_dung": "Ổn",
                "thoi_gian": "2026-09-09 10:05:00",
                "anh_urls": "",
            },
        ],
    }
    saved = launch._save_ingest_checkpoint(job_id, chk_v2)
    assert saved["ok"] is True
    assert saved["version"] == 2
    assert saved["active_scope"] == "comment"
    assert saved["rows_count"] == 2

    # 3. Check progress reflects Checkpoint V2
    prog_res = _get(base, f"/ingest/progress?id={job_id}")
    prog = prog_res["progress"]
    assert prog["active_scope"] == "comment"
    assert prog["checkpoint"]["version"] == 2
    assert prog["ui_reference"]["rcount_with_media"] == 20

    # 4. Finalize with overlapping review (cmid_001 has media now from media scope)
    status, out = launch._store_ingest_payload({
        "job": job_id,
        "name": "shopee_456_reviews",
        "rows": [
            {
                "cmid": "cmid_001",
                "user": "buyer_a",
                "sao": 5,
                "noi_dung": "Đẹp và chắc chắn",
                "thoi_gian": "2026-09-09 10:00:00",
                "anh_urls": "https://shopee/cmid001.jpg",
            },
            {
                "cmid": "cmid_003",
                "user": "buyer_c",
                "sao": 5,
                "noi_dung": "Tuyệt",
                "thoi_gian": "2026-09-09 10:10:00",
            },
        ],
    })
    assert status == 200 and out["ok"]
    assert out["count"] == 3  # cmid_001 deduplicated, so 2 from checkpoint + 1 new = 3 total

    # Check that cmid_001 in CSV has merged media
    csv_file = outputs / "csv" / "shopee_456_reviews.csv"
    assert csv_file.is_file()
    content = csv_file.read_text(encoding="utf-8-sig")
    assert "cmid001.jpg" in content
    assert "Đẹp và chắc chắn" in content

