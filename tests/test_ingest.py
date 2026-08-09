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
        {"user": "an", "sao": 5, "noi_dung": "hàng ngon, giao nhanh"},
        {"user": "bình", "sao": 4, "noi_dung": "tạm ổn"},
    ]
    status, out = _post(base, "/ingest?name=shopee_123", {"source": "https://shopee.vn/x", "rows": rows})
    assert status == 200 and out["ok"] and out["count"] == 2
    assert out["path"] == "outputs/inbox/shopee_123.json"
    assert out["csv"] == "outputs/csv/shopee_123.csv"

    saved = json.loads((outputs / "inbox" / "shopee_123.json").read_text(encoding="utf-8"))
    assert saved["count"] == 2
    assert saved["rows"][1]["user"] == "bình"  # diacritics survive the round trip
    assert saved["source"] == "https://shopee.vn/x"

    # read_bytes, not read_text: universal newlines would hide a missing CRLF.
    raw = (outputs / "csv" / "shopee_123.csv").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"  # BOM, else Excel renders Vietnamese as mojibake
    text = raw.decode("utf-8-sig")
    assert text.startswith("user,sao,noi_dung\r\n")
    assert '"hàng ngon, giao nhanh"' in text  # embedded comma quoted, not column-split


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
    assert _get(base, f"/ingest/result?id={job_id}")["ok"] is False

    # The extension claims it; a claimed job is not handed out twice.
    jobs = _get(base, "/ingest/jobs?wait=1")["jobs"]
    assert [j["id"] for j in jobs] == [job_id]
    assert _get(base, "/ingest/jobs?wait=0")["jobs"] == []

    _post(base, "/ingest?name=shopee_27429880257",
          {"job": job_id, "rows": [{"user": "an", "sao": 5, "noi_dung": "tốt"}]})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] and done["result"]["count"] == 1
    assert done["result"]["csv"] == "outputs/csv/shopee_27429880257.csv"
    assert (outputs / "csv" / "shopee_27429880257.csv").is_file()


def test_failed_job_is_recorded_so_the_agent_stops_waiting(server):
    base, _ = server
    job_id = _get(base, "/ingest/job?url=https://shopee.vn/product/1/2")["job"]["id"]
    _get(base, "/ingest/jobs?wait=1")
    _post(base, "/ingest", {"job": job_id, "error": "Shopee error 90309999 (is_login=false)"})

    done = _get(base, f"/ingest/result?id={job_id}")
    assert done["ok"] and done["result"]["ok"] is False
    assert "90309999" in done["result"]["error"]


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
