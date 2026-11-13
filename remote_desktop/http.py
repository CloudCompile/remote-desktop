"""HTTP plumbing: BaseHandler and static-file serving.

Ownership: everything every request handler shares — JSON replies (with
request-id header, #24), request-id context, body parsing, auth checks,
the base HTTP access log, MIME table and send_file with ETag caching.
Routing (do_GET/do_POST) lives in server.py.
"""

import json
import mimetypes
import os
import uuid

from http.server import BaseHTTPRequestHandler

from . import auth, config, logging as log

MIME = mimetypes.MimeTypes()
MIME.add_type("application/javascript", ".js")
MIME.add_type("application/wasm", ".wasm")
MIME.add_type("image/svg+xml", ".svg")

MAX_BODY = 1 << 20  # 1 MiB request bodies
STREAM_PATHS = (
    "/websockify",
    "/api/mic",
    "/api/camera",
    "/api/audio.raw",
    "/api/audio.opus",
)  # long-lived: counted by their own kind, not as plain http


class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RemoteControl/2.1"
    timeout = config.HTTP_TIMEOUT  # idle keep-alive connections are closed (#28)

    # ---- plumbing
    def _json(self, code, obj, extra=None):
        data = b"" if getattr(self, "_head_only", False) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(json.dumps(obj).encode())))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", getattr(self, "rid", "-") or "-")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _new_request_id(self):
        # short uuid per request (#24): correlatable in responses and logs
        self.rid = uuid.uuid4().hex[:8]
        log.request_id.set(self.rid)
        return self.rid

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _authed(self):
        return auth._verify_token(auth._parse_cookie(self, "rd_session"))

    def log_message(self, fmt, *args):
        log.log(f"{auth._client_ip(self)} {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def is_stream_path(self):
        return any(self.path == p or self.path.startswith(p + "/") for p in STREAM_PATHS)


def send_file(handler, path, cache="no-cache, must-revalidate"):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        handler._json(404, {"error": "not found"})
        return
    etag = f'"{int(st.st_mtime):x}-{st.st_size:x}"'
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.end_headers()
        return
    ctype = MIME.guess_type(path)[0] or "application/octet-stream"
    head_only = getattr(handler, "_head_only", False)
    with open(path, "rb") as f:
        data = b"" if head_only else f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache)  # ETag revalidation; changes show up immediately
    handler.end_headers()
    handler.wfile.write(data)
