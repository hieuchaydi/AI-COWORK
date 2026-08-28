"""
Hermetic local HTTP fixture server for BCP conformance tests.

Endpoints (§6.1):
  GET /                      basic-static
  GET /5k-node               5000-node table (dom.snapshot stress)
  GET /slow-500ms            slow endpoint (networkidle timing)
  GET /redirect-3x           3-hop redirect chain
  GET /json-api              XHR/JSON endpoint
  GET /spa                   SPA with pushstate navigation
  GET /shadow-dom            shadow DOM host
  GET /iframe                page with same-origin iframe
  GET /dialog                page that opens alert/confirm/prompt
  GET /form                  form with file-upload input
  GET /cookies               sets httponly cookie, reads it back
  GET /download              triggers a file download
  GET /js-error              page that throws JS exception on load
  POST /echo                 echo request body as JSON
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default logging
        pass

    def send_page(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self.send_page("""<!DOCTYPE html><html><head><title>BCP Fixture</title></head>
<body><h1>BCP Fixture Site</h1><p id="p1">Hello World</p>
<a id="link" href="/spa">Go SPA</a></body></html>""")

        elif path == "/5k-node":
            rows = "".join(
                f"<tr><td>{i}</td><td>Name-{i}</td><td>Value-{i}</td></tr>"
                for i in range(5000)
            )
            self.send_page(
                f"""<!DOCTYPE html><html><body>
<table id="big-table"><thead><tr><th>#</th><th>Name</th><th>Value</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
            )

        elif path == "/slow-500ms":
            time.sleep(0.5)
            self.send_page("""<!DOCTYPE html><html><body>
<p id="slow">Loaded after 500ms</p></body></html>""")

        elif path == "/redirect-3x":
            # First leg — redirect to /redirect-2x
            self.send_response(302)
            self.send_header("Location", "/redirect-2x")
            self.end_headers()

        elif path == "/redirect-2x":
            self.send_response(302)
            self.send_header("Location", "/redirect-1x")
            self.end_headers()

        elif path == "/redirect-1x":
            self.send_response(302)
            self.send_header("Location", "/redirect-final")
            self.end_headers()

        elif path == "/redirect-final":
            self.send_page("""<!DOCTYPE html><html><body>
<p id="final">Redirect chain complete</p></body></html>""")

        elif path == "/json-api":
            self.send_json({"status": "ok", "value": 42, "items": ["a", "b", "c"]})

        elif path == "/spa":
            # Single-page app with pushstate navigation
            self.send_page("""<!DOCTYPE html><html><head><title>SPA</title></head>
<body>
<nav>
  <a id="nav-home" href="#" onclick="navigate('/spa/home')">Home</a>
  <a id="nav-about" href="#" onclick="navigate('/spa/about')">About</a>
</nav>
<div id="content">SPA Home</div>
<script>
function navigate(path) {
  window.history.pushState({path}, '', path);
  document.getElementById('content').textContent = 'Page: ' + path;
}
window.onpopstate = function(e) {
  if (e.state) document.getElementById('content').textContent = 'Page: ' + e.state.path;
};
</script>
</body></html>""")

        elif path.startswith("/spa/"):
            page_name = path[5:] or "home"
            self.send_page(f"""<!DOCTYPE html><html><body>
<p id="spa-page">{page_name}</p></body></html>""")

        elif path == "/shadow-dom":
            self.send_page("""<!DOCTYPE html><html><body>
<div id="host"></div>
<script>
const host = document.getElementById('host');
const shadow = host.attachShadow({mode: 'open'});
shadow.innerHTML = '<p id="shadow-text">Inside Shadow DOM</p><button id="shadow-btn">Click me</button>';
</script>
</body></html>""")

        elif path == "/iframe":
            self.send_page("""<!DOCTYPE html><html><body>
<h1>Parent Frame</h1>
<p id="parent-text">I am the parent</p>
<iframe id="child-frame" src="/iframe-content" width="400" height="200"></iframe>
</body></html>""")

        elif path == "/iframe-content":
            self.send_page("""<!DOCTYPE html><html><body>
<p id="frame-text">I am the iframe</p>
<input id="frame-input" type="text" placeholder="Type here">
</body></html>""")

        elif path == "/dialog":
            self.send_page("""<!DOCTYPE html><html><body>
<p id="status">No dialog yet</p>
<button id="alert-btn" onclick="alert('Alert message'); document.getElementById('status').textContent='alert-done'">Show Alert</button>
<button id="confirm-btn" onclick="var r=confirm('Confirm?'); document.getElementById('status').textContent='confirm-'+r">Show Confirm</button>
<button id="prompt-btn" onclick="var r=prompt('Enter text:', 'default'); document.getElementById('status').textContent='prompt-'+r">Show Prompt</button>
</body></html>""")

        elif path == "/form":
            self.send_page("""<!DOCTYPE html><html><body>
<form id="upload-form" action="/echo" method="post" enctype="multipart/form-data">
  <input id="text-field" type="text" name="name" placeholder="Name">
  <input id="file-field" type="file" name="file">
  <button id="submit-btn" type="submit">Upload</button>
</form>
<div id="result"></div>
</body></html>""")

        elif path == "/cookies":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "session_id=abc123; HttpOnly; Path=/")
            self.send_header("Set-Cookie", "theme=dark; Path=/")
            cookie_header = self.headers.get("Cookie", "(none)")
            body = f"""<!DOCTYPE html><html><body>
<p id="cookie-echo">Received: {cookie_header}</p>
<p id="session-note">session_id is HttpOnly</p>
</body></html>""".encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/download":
            content = b"filename,value\nrow1,1\nrow2,2\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=export.csv")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        elif path == "/js-error":
            self.send_page("""<!DOCTYPE html><html><body>
<p>This page throws on load</p>
<script>throw new Error("Intentional JS error from fixture");</script>
</body></html>""")

        else:
            self.send_page("<html><body><p>Not found</p></body></html>", status=404)

    def do_POST(self):
        if self.path == "/echo":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            self.send_json({
                "method": "POST",
                "path": "/echo",
                "body_length": len(body),
                "body_preview": body[:200].decode("utf-8", errors="replace"),
            })
        else:
            self.send_json({"error": "not found"}, status=404)


class FixtureServer:
    """A synchronous HTTPServer run in a daemon thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._server = HTTPServer((host, port), FixtureHandler)
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "FixtureServer":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)


# Convenience for use as context manager
class FixtureContext:
    def __enter__(self) -> FixtureServer:
        self.server = FixtureServer().start()
        return self.server

    def __exit__(self, *_):
        self.server.stop()
