"""HTTP routes, server bootstrap and graceful shutdown.

Ownership: do_GET/do_POST routing (kept behaviour-identical to the
pre-split control.py, #22), the connection-bounded ThreadingHTTPServer
(#28), signal handling and child reaping (#25), startup validation
(#23) and the CLI (--check-config, --export-state, --import-state).
"""

import json
import os
import re
import shutil
import signal
import socket
import stat
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import (
    apps,
    audio,
    audit,
    auth,
    clipboard,
    config,
    files,
    http,
    limits,
    logging as log,
    media_in,
    notify,
    procs,
    ready,
    state,
    stats,
    store,
    totp,
    users,
    vncproxy,
    webauthn,
    webrtc,
    windows,
)

WEB_DIR = config.WEB_DIR

# ------------------------------------------------------------- capability table (#112)
#
# Every /api route declares the capability level it needs; the request's
# role level (viewer=1 < operator=2 < admin=3) must meet or exceed it.
# Unmapped /api paths FAIL CLOSED at admin level, so a forgotten entry
# cannot silently become world-accessible (#112).

CAP_VIEW, CAP_OPERATE, CAP_ADMIN = users.ROLE_LEVEL["viewer"], users.ROLE_LEVEL["operator"], users.ROLE_LEVEL["admin"]

_GET_CAPS = {
    "/api/auth_check": CAP_VIEW,
    "/api/windows": CAP_VIEW,
    "/api/window/thumbnail": CAP_VIEW,
    "/api/apps": CAP_VIEW,
    "/api/store/backend": CAP_VIEW,
    "/api/store/collections": CAP_VIEW,
    "/api/store": CAP_VIEW,
    "/api/store/search": CAP_VIEW,
    "/api/store/status": CAP_VIEW,
    "/api/audio/status": CAP_VIEW,
    "/api/audio/sinks": CAP_VIEW,
    "/api/audio/apps": CAP_VIEW,
    "/api/audio/diagnostics": CAP_VIEW,
    "/api/audio/record": CAP_VIEW,
    "/api/audio/recordings": CAP_VIEW,
    "/api/media/status": CAP_VIEW,
    "/api/clipboard": CAP_VIEW,
    "/api/clipboard/history": CAP_VIEW,
    "/api/clipboard/sync": CAP_VIEW,
    "/api/clipboard/image": CAP_VIEW,
    "/api/settings": CAP_VIEW,
    "/api/custom-apps": CAP_VIEW,
    "/api/pins": CAP_VIEW,
    "/api/audio/test": CAP_OPERATE,
    "/api/mic": CAP_OPERATE,
    "/api/mic/status": CAP_VIEW,
    "/api/camera": CAP_OPERATE,
    "/api/camera/status": CAP_VIEW,
    "/api/audio.raw": CAP_VIEW,
    "/api/audio.opus": CAP_VIEW,
    "/api/stats": CAP_VIEW,
    "/api/files": CAP_VIEW,
    "/api/files/roots": CAP_VIEW,
    "/api/files/search": CAP_VIEW,
    "/api/files/content": CAP_VIEW,
    "/api/files/thumb": CAP_VIEW,
    "/api/files/audit": CAP_ADMIN,
    "/api/files/upload/status": CAP_VIEW,
    "/api/files/transfers": CAP_VIEW,
    "/api/download": CAP_VIEW,
    "/api/backup": CAP_ADMIN,
    # v2.6.0: account surface is self-service (any role); admin is admin
    "/api/csrf": CAP_VIEW,
    "/api/account": CAP_VIEW,
    "/api/sessions": CAP_VIEW,
    "/api/notify": CAP_VIEW,
    "/api/2fa": CAP_VIEW,
    "/api/2fa/qr": CAP_VIEW,
    "/api/admin/users": CAP_ADMIN,
    "/api/admin/audit": CAP_ADMIN,
    "/api/admin/audit/verify": CAP_ADMIN,
    "/api/admin/keys": CAP_ADMIN,
}

_POST_CAPS = {
    "/share": CAP_VIEW,  # OS share-target form POST; origin-checked (#116)
    "/api/fit": CAP_OPERATE,
    "/api/webrtc/offer": CAP_VIEW,  # watching is view-level (#137)
    "/api/window/action": CAP_OPERATE,
    "/api/geometry": CAP_OPERATE,
    "/api/settings": CAP_OPERATE,
    "/api/audio/sink": CAP_OPERATE,
    "/api/audio/volume": CAP_OPERATE,
    "/api/audio/app-volume": CAP_OPERATE,
    "/api/media/keys": CAP_OPERATE,
    "/api/audio/record": CAP_OPERATE,
    "/api/resize": CAP_OPERATE,
    "/api/open/": CAP_OPERATE,  # prefix
    "/api/paste": CAP_OPERATE,
    "/api/clipboard/set": CAP_OPERATE,
    "/api/clipboard/paste": CAP_OPERATE,
    "/api/clipboard/pin": CAP_OPERATE,
    "/api/clipboard/tag": CAP_OPERATE,
    "/api/clipboard/item": CAP_VIEW,
    "/api/clipboard/remove": CAP_OPERATE,
    "/api/clipboard/clear": CAP_OPERATE,
    "/api/store/install": CAP_ADMIN,
    "/api/store/remove": CAP_ADMIN,
    "/api/store/update": CAP_ADMIN,
    "/api/store/cancel": CAP_OPERATE,
    "/api/custom-apps": CAP_OPERATE,
    "/api/pins/folder": CAP_VIEW,  # self-service personalization, per-user isolated (#120)
    "/api/pins/assign": CAP_VIEW,
    "/api/pins/reorder": CAP_VIEW,
    "/api/launches/clear": CAP_OPERATE,
    "/api/launch": CAP_OPERATE,
    "/api/store/launch": CAP_OPERATE,
    "/api/upload": CAP_OPERATE,
    "/api/files/delete": CAP_OPERATE,
    "/api/files/mkdir": CAP_OPERATE,
    "/api/files/rename": CAP_OPERATE,
    "/api/files/move": CAP_OPERATE,
    "/api/files/copy": CAP_OPERATE,
    "/api/files/chmod": CAP_OPERATE,
    "/api/files/archive": CAP_OPERATE,
    "/api/files/extract": CAP_OPERATE,
    "/api/files/upload/init": CAP_OPERATE,
    "/api/files/upload/chunk": CAP_OPERATE,
    "/api/files/upload/complete": CAP_OPERATE,
    "/api/files/upload/cancel": CAP_OPERATE,
    "/api/files/transfers/": CAP_OPERATE,  # prefix
    "/api/pins/toggle": CAP_VIEW,
    "/api/backup": CAP_ADMIN,
    # v2.6.0: self-service + admin
    "/api/account/password": CAP_VIEW,
    "/api/2fa/setup": CAP_VIEW,
    "/api/2fa/enable": CAP_VIEW,
    "/api/2fa/disable": CAP_VIEW,
    "/api/2fa/recovery/regenerate": CAP_VIEW,
    "/api/sessions/revoke": CAP_VIEW,
    "/api/sessions/revoke-all": CAP_VIEW,
    "/api/notify/test": CAP_VIEW,
    "/api/admin/user": CAP_ADMIN,
    "/api/admin/users": CAP_ADMIN,
    "/api/admin/keys/rotate": CAP_ADMIN,
    "/api/webauthn/register/options": CAP_VIEW,
    "/api/webauthn/register/verify": CAP_VIEW,
}

_DELETE_CAPS = {
    "/api/custom-apps": CAP_OPERATE,
    "/api/pins/folder": CAP_VIEW,
    "/api/admin/users": CAP_ADMIN,
    "/api/webauthn/credentials": CAP_VIEW,
}

# state-changing requests that CANNOT carry a CSRF token by design (#116):
# login (no session yet), logout (stale-session escape hatch) and the PWA
# share-target form POST (sent by the OS, no custom headers possible).
# All three are still subject to Origin/Sec-Fetch-Site validation.
CSRF_EXEMPT = ("/api/login", "/api/logout", "/share")


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard connection cap (#28): over-cap
    connections get a fast 503 (+ Retry-After) answered on the accept
    thread — no new handler thread, so a flood cannot exhaust threads."""

    daemon_threads = True
    allow_reuse_address = True

    def process_request(self, request, client_address):
        if not limits.acquire("connections"):
            log.log(f"connection limit reached ({limits.caps()['connections']}); refusing", peer=client_address[0])
            _reject_503(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            limits.release("connections")


def _reject_503(request):
    body = json.dumps({"ok": False, "error": "server busy: connection limit reached; retry shortly"}).encode()
    try:
        request.settimeout(5)
        request.readline()  # request line
        while True:  # headers
            line = request.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
        head = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json\r\n"
            b"Retry-After: " + str(limits.RETRY_AFTER).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        request.sendall(head + body)
    except Exception:
        pass
    finally:
        try:
            request.close()
        except Exception:
            pass


class Handler(http.BaseHandler):
    # ---- role enforcement (#112): every request resolves its required
    # capability from the tables above; unmapped /api paths fail closed.
    def _cap_needed(self, path, table):
        if path in table:
            return table[path]
        for prefix, cap in table.items():
            if prefix.endswith("/") and path.startswith(prefix):
                return cap
        return CAP_ADMIN  # fail closed

    def _require(self, cap):
        """True when the request was REJECTED (403 sent + audited, #112).
        `_json` returns None, so the rejection must be signalled by bool."""
        if (getattr(self, "cap", 0) or 0) >= cap:
            return False
        audit.event(audit.SECURITY, "role_denied", handler=self, target=self.path, result="denied")
        self._json(403, {"ok": False, "error": "forbidden: your role does not allow this action"})
        return True

    # ---- streaming audio (needs custom response, not _json)
    def _stream_audio(self, codec, bitrate=audio.BITRATE_DEFAULT, shared=None):
        """Chunked audio stream (#14). The encoder process is shared per
        (source, codec, bitrate) tier and fans chunks out to every listener
        (#105); `shared=0` requests an isolated per-client capture."""
        if not limits.gate(self, "audio", "audio streams"):
            return
        cap, mime = audio.acquire_capture(audio.audio_source(), codec, bitrate, shared=shared)
        if cap is None:
            return self._json(503, {"error": "audio capture unavailable"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            lsn = cap.attach()
            started = time.time()
            log.log(f"audio open  {self.path.split('?')[0]} {auth._client_ip(self)}")
            try:
                while True:
                    chunk = lsn.next()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away; normal on mobile when the tab is backgrounded
            finally:
                cap.detach(lsn)
                log.log(f"audio close {self.path.split('?')[0]} after {time.time() - started:.0f}s")
        finally:
            limits.release("audio")

    # ---- GET
    def do_GET(self):
        self._new_request_id()
        url = urlparse(self.path)
        path = url.path

        # websocket upgrade for the VNC stream (same origin, same port):
        # malformed upgrade attempts are answered first (400, no auth
        # surface), then the session check (#112/#125)
        if path == "/websockify" or path.startswith("/websockify/"):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or "upgrade" not in (self.headers.get("Connection", "") + "").lower():
                return self._json(400, {"error": "expected websocket upgrade"})
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            r = self._require(CAP_OPERATE)
            if r:
                return r
            return vncproxy.websocket_proxy(self)

        # public endpoints
        if path == "/api/health":
            return self._json(200, stats.health())
        if path == "/api/ready":
            snap = ready.ready_snapshot()
            return self._json(200 if snap["ok"] else 503, snap)
        if path == "/api/login":
            return self._login()
        if path == "/login":
            return http.send_file(self, os.path.join(WEB_DIR, "login.html"))

        if path.startswith("/api/"):
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            r = self._require(self._cap_needed(path, _GET_CAPS))
            if r:
                return r
            try:
                return self._dispatch_api_get(url, path)
            except files.FilesError as e:  # #79+: client-safe file errors
                return self._json(e.status, {"ok": False, "error": str(e)})

        # static files
        if path == "/":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return http.send_file(self, os.path.join(WEB_DIR, "index.html"))

        # icons for the Apps menu (resolved from the app's own .desktop Icon=)
        if path.startswith("/icon/"):
            app_id = unquote(path[6:])
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            p = apps.icon_path_for_app(app_id)
            if not p or not os.path.isfile(p):
                return self._json(404, {"error": "no icon"})
            return http.send_file(self, p, cache="max-age=86400")

        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            return self._json(403, {"error": "forbidden"})
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        return http.send_file(self, full)

    def _dispatch_api_get(self, url, path):
        """Authenticated GET /api/* routes."""
        if path == "/api/auth_check":  # #112: capabilities for the UI
            return self._json(
                200,
                {
                    "ok": True,
                    "user": getattr(self, "user", ""),
                    "role": getattr(self, "role", ""),
                    "caps": {"view": True, "operate": self.cap >= CAP_OPERATE, "admin": self.cap >= CAP_ADMIN},
                    "sessions": {"current": (getattr(self, "sid", "") or "")[:12]},
                },
            )
        if path == "/api/csrf":  # #116: per-session CSRF token (+ cookie mirror)
            tok = auth.csrf_token(getattr(self, "sid", "") or "")
            return self._json(200, {"ok": True, "token": tok}, extra={"Set-Cookie": auth.csrf_cookie_header(tok)})
        if path == "/api/account":  # #109/#118/#123: self-service security settings
            from . import users as _u

            rec = _u.record(self.user) or {}
            t = rec.get("totp") or {}
            return self._json(
                200,
                {
                    "ok": True,
                    "user": self.user,
                    "role": self.role,
                    "created": rec.get("created") or 0,
                    "last_login": rec.get("last_login") or 0,
                    "totp": {"enabled": bool(t.get("enabled")), "required": bool(config.TOTP_REQUIRED)},
                    "recovery_codes_left": len(rec.get("recovery") or []),
                    "passkeys": webauthn.credentials_for(self.user) if webauthn.enabled() else [],
                    "notify": {"enabled": bool(config.NOTIFY_ENABLED), "failed_24h": notify.failed_attempts(self.user)},
                    "failed_24h": notify.failed_attempts(self.user),
                },
            )
        if path == "/api/sessions":  # #114: device list (no raw tokens)
            return self._json(200, {"ok": True, "sessions": auth.sessions_public(user=self.user, current_sid=self.sid)})
        if path == "/api/notify":  # #123: unread alerts (marks them seen)
            return self._json(
                200, {"ok": True, "notifications": notify.unread(self.user), "devices": notify.known_devices(self.user)}
            )
        if path == "/api/2fa":  # #109: enrollment status
            t = users.totp_get(self.user)
            return self._json(
                200,
                {
                    "ok": True,
                    "enabled": t["enabled"],
                    "recovery_left": len(users.recovery_hashes(self.user)),
                    "required": bool(config.TOTP_REQUIRED),
                },
            )
        if path == "/api/2fa/qr":  # #109: QR of the pending otpauth URI (SVG)
            if not _qr_available():
                return self._json(501, {"ok": False, "error": "QR rendering unavailable; use the manual secret"})
            secret = _totp_pending_secret(self.user)
            if not secret:
                return self._json(404, {"ok": False, "error": "no pending enrollment; call /api/2fa/setup first"})
            uri = totp.otpauth_uri(self.user, secret)
            svg = _qr_svg(uri)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(svg)
            return
        if path == "/api/admin/audit":  # #113: read/filter for admins
            q = parse_qs(url.query)
            events = audit.read(
                kind=q.get("kind", [None])[0],
                q=q.get("q", [None])[0],
                limit=min(max(int(q.get("limit", ["200"])[0]), 1), 1000),
            )
            return self._json(200, {"ok": True, "events": events})
        if path == "/api/admin/audit/verify":  # #113: chain verification
            ok, first_bad, checked = audit.verify()
            return self._json(200, {"ok": True, "chain_ok": ok, "first_bad": first_bad, "checked": checked})
        if path == "/api/admin/users":  # #111: account list (no hashes)
            return self._json(200, {"ok": True, "users": users.list_public()})
        if path == "/api/admin/keys":  # #118: signing-key version
            return self._json(200, {"ok": True, "key_version": auth.key_version()})
        if path == "/api/windows":
            try:
                return self._json(
                    200,
                    {
                        "ok": True,
                        "wm": windows.wm_name(),
                        "windows": windows.list_windows(),
                        "display": windows.display_geometry(),  # #60: move/center math
                    },
                )
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)})
        if path == "/api/window/thumbnail":  # #59: single-frame JPEG of one window
            hid = parse_qs(url.query).get("hid", [""])[0]
            data, err = windows.window_thumbnail(hid)
            if data is None:
                return self._json(502, {"ok": False, "error": err or "thumbnail unavailable"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(data)
            return
        if path == "/api/apps":  # #77: ETag + If-None-Match -> 304
            body, etag = apps.apps_response()
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Request-Id", getattr(self, "rid", "-") or "-")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)
            return
        if path == "/api/store/backend":  # #67: detected backend + capabilities
            return self._json(200, {"ok": True, **store.backend_info()})
        if path == "/api/store/collections":  # #69: curated collections
            return self._json(200, {"ok": True, "collections": store.collections_with_status()})
        if path == "/api/store":
            return self._json(
                200, {"apps": [{**a, "installed": store.is_installed(a["pkg"])} for a in apps.APP_CATALOG]}
            )
        if path == "/api/store/search":
            q = parse_qs(url.query).get("q", [""])[0].strip()
            if not q or len(q) > 80:
                return self._json(200, {"apps": []})
            return self._json(200, {"apps": store.store_search(q)})
        if path == "/api/store/status":
            q = parse_qs(url.query)
            pkg = q.get("pkg", [""])[0]
            if not store.valid_pkg_name(pkg):
                return self._json(400, {"ok": False, "error": "invalid package name"})
            return self._json(200, store.install_status(pkg))
        if path == "/api/audio/status":
            return self._json(200, audio.audio_status())
        if path == "/api/audio/sinks":  # #94: pick the capture sink
            sel = audio._selected_sink_name()
            sink_list = audio.sinks()
            cur = next((s for s in sink_list if s["name"] == (sel or audio._pa("get-default-sink"))), None)
            return self._json(
                200,
                {
                    "ok": True,
                    "sinks": sink_list,
                    "selected": sel,
                    "volume": cur["volume"] if cur else None,
                    "mute": cur["mute"] if cur else None,
                    "notice": audio.notice(),
                },
            )
        if path == "/api/audio/apps":  # #95: per-application volume
            return self._json(200, {"ok": True, "supported": audio.app_supported(), "apps": audio.audio_apps()})
        if path == "/api/audio/diagnostics":  # #106
            return self._json(200, audio.diagnostics())
        if path == "/api/audio/record":  # #102: recording status
            return self._json(200, {"ok": True, **audio.record_status()})
        if path == "/api/audio/recordings":  # #102: stored recordings
            return self._json(200, {"ok": True, "recordings": audio.recordings_list()})
        if path == "/api/media/status":  # #101: MPRIS players
            players = audio.media_players()
            return self._json(200, {"ok": True, "players": players, "active": players[0] if players else ""})
        if path == "/api/clipboard":
            return self._json(200, clipboard.clipboard_get())
        if path == "/api/clipboard/history":  # #70: q= full-text search, tag= filter
            q = parse_qs(url.query)
            items = clipboard.clip_history(q.get("q", [None])[0], q.get("tag", [None])[0], user=self.user)
            return self._json(200, {"items": items, "tags": clipboard.clip_tags(user=self.user)})
        if path == "/api/clipboard/sync":  # #72: desktop -> phone push (opt-in)
            since = parse_qs(url.query).get("since", [""])[0][:64]
            return self._json(200, clipboard.clip_sync(since, user=self.user))
        if path == "/api/clipboard/image":  # #71: serve a stored image
            it = clipboard.clip_item(parse_qs(url.query).get("id", [""])[0], user=self.user)
            p = clipboard.clip_image_path(it["id"], user=self.user) if it else None
            if not p:
                return self._json(404, {"ok": False, "error": "no such image"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(os.path.getsize(p)))
            self.send_header("Content-Disposition", f'attachment; filename="clip-{it["id"]}.png"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            return
        if path == "/api/settings":  # #72/#76: opt-in toggles
            return self._json(200, {"ok": True, "settings": state.load_settings()})
        if path == "/api/custom-apps":  # #63
            return self._json(200, {"ok": True, "apps": apps.list_custom_apps()})
        if path == "/api/pins":  # #64: pins + folders ("Unfiled" virtual)
            return self._json(200, {"ok": True, **state.pins_snapshot(self.user)})
        if path == "/api/audio/test":
            sink = audio._pa("get-default-sink") or "remote_out"
            procs.run_detached(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=4",
                    "-f",
                    "pulse",
                    "-device",
                    sink,
                    "remote-test",
                ]
            )
            return self._json(200, {"ok": True, "msg": "playing a 4s 440Hz tone into " + sink})
        if path == "/api/mic":
            return media_in.websocket_mic(self)
        if path == "/api/mic/status":
            return self._json(200, media_in.mic_status())
        if path == "/api/camera":
            return media_in.websocket_camera(self)
        if path == "/api/camera/status":
            return self._json(200, media_in.camera_status())
        if path == "/api/audio.raw":
            return self._stream_audio("pcm")
        if path == "/api/audio.opus":  # #96: bitrate per query (clamped server-side)
            q = parse_qs(url.query)
            try:
                bitrate = int(q.get("bitrate", [""])[0])
            except ValueError:
                bitrate = audio.BITRATE_DEFAULT
            shared = q.get("shared", ["1"])[0] != "0"  # #105: per-client isolation opt-out
            return self._stream_audio("opus", bitrate, shared)
        if path == "/api/stats":
            return self._json(200, stats.system_stats())
        # ---- files & transfer (#78/#79/#81/#83/#84/#88/#89/#90/#92)
        if path == "/api/files":
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0], user=self.user, cap=self.cap)
            if root is None:
                return self._json(404, {"ok": False, "error": "no such root"})
            d = files.list_dir(
                root,
                q.get("path", [""])[0],
                sort=q.get("sort", ["name"])[0],
                desc=q.get("dir", ["asc"])[0] == "desc",
                dirs_first=q.get("dirs_first", ["1"])[0] != "0",
            )
            return self._json(
                200,
                {
                    "ok": True,
                    "roots": files.roots_public(user=self.user, cap=self.cap),
                    "root": root.rid,
                    "root_label": root.label,
                    "readonly": root.readonly,
                    **d,
                    "disk": files.disk_info(root),
                },
            )
        if path == "/api/files/roots":  # #84 — scoped per user (#120)
            is_admin = (getattr(self, "cap", 0) or 0) >= CAP_ADMIN
            return self._json(
                200,
                {
                    "ok": True,
                    "roots": files.roots_public(user=self.user, cap=self.cap),
                    **({"errors": files.ROOT_ERRORS} if is_admin else {}),
                },
            )
        if path == "/api/files/search":  # #81: bounded recursive search
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0], user=self.user, cap=self.cap)
            if root is None:
                return self._json(404, {"ok": False, "error": "no such root"})
            try:
                lim = min(int(q.get("limit", ["200"])[0]), files.SEARCH_RESULTS_MAX)
            except ValueError:
                lim = files.SEARCH_RESULTS_MAX
            r = files.search(root, q.get("path", [""])[0], q.get("q", [""])[0], limit=lim)
            return self._json(200, {"ok": True, **r})
        if path == "/api/files/content":  # #83: ranged, streamed download
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0], user=self.user, cap=self.cap)
            if root is None:
                return self._json(404, {"ok": False, "error": "no such root"})
            return self._file_download(root, q.get("path", [""])[0], inline=q.get("inline", ["0"])[0] == "1")
        if path == "/api/files/thumb":  # #82: bounded cached thumbnail
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0], user=self.user, cap=self.cap)
            if root is None:
                return self._json(404, {"ok": False, "error": "no such root"})
            try:
                size = min(max(int(q.get("size", ["256"])[0]), 16), 1024)
            except ValueError:
                size = 256
            return self._file_thumb(root, q.get("path", [""])[0], size)
        if path == "/api/files/audit":  # #92: read-only audit view
            q = parse_qs(url.query)
            try:
                lim = min(max(int(q.get("limit", ["200"])[0]), 1), 1000)
            except ValueError:
                lim = 200
            return self._json(200, {"ok": True, "events": files.audit_list(lim)})
        if path == "/api/files/upload/status":  # #78: acknowledged offset
            sid = parse_qs(url.query).get("session", [""])[0]
            return self._json(200, {"ok": True, **files.upload_status(sid)})
        if path == "/api/files/transfers":  # #88: persistent transfer queue
            return self._json(200, files.transfers_snapshot())
        if path == "/api/download":  # legacy: flat first-root download (v2.0.1 shape)
            name = parse_qs(url.query).get("name", [""])[0]
            p = files._safe_file_path(name)
            if not p or not os.path.isfile(p):
                return self._json(404, {"error": "not found"})
            return self._file_download(
                files.default_root(user=self.user, cap=self.cap), name, inline=False, legacy=True
            )
        if path == "/api/backup":
            return self._json(200, {"ok": True, "backups": state.list_backups()})
        return self._json(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        self._new_request_id()
        url = urlparse(self.path)
        path = url.path

        if path == "/api/login":
            return self._login()
        # passkey login ceremony (#110): public, BEFORE the cookie check —
        # the assertion itself is the credential. Origin-checked (#116).
        if path == "/api/webauthn/auth/options":
            if not auth.origin_ok(self):
                return self._json(403, {"ok": False, "error": "cross-origin request refused"})
            return self._webauthn_guard(lambda: webauthn.authentication_options(handler=self))
        if path == "/api/webauthn/auth/verify":
            if not auth.origin_ok(self):
                return self._json(403, {"ok": False, "error": "cross-origin request refused"})
            if not webauthn.enabled():
                return self._json(403, {"ok": False, "error": "passkeys are disabled (RD_WEBAUTHN)"})
            return self._passkey_login()
        if path == "/api/logout":
            return self._json(
                200,
                {"ok": True},
                extra={"Set-Cookie": auth.clear_cookie_header(self)},
            )

        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        # CSRF gate (#116): state-changing requests need the session's
        # token; login/logout/share are exempt (see CSRF_EXEMPT) but all
        # requests with Origin/Sec-Fetch-Site headers are origin-checked.
        if path not in CSRF_EXEMPT and not auth._csrf_ok(self):
            audit.event(audit.SECURITY, "csrf_rejected", handler=self, target=path, result="denied")
            return self._json(
                403,
                {"ok": False, "error": "csrf token missing or invalid"},
                extra={"X-CSRF-Required": "1"},
            )

        r = self._require(self._cap_needed(path, _POST_CAPS))
        if r:
            return r

        try:
            if path == "/share":  # #87: PWA share-target uploads (multipart)
                return self._share_upload()
            if path == "/api/webrtc/offer":  # #137: WebRTC signalling
                ok, reason = webrtc.available()
                if not ok:
                    return self._json(501, {"ok": False, "error": "WebRTC not enabled: " + reason})
                d = self._body()
                sdp, type_ = str(d.get("sdp", "")), str(d.get("type", "offer"))
                if not sdp or "v=0" not in sdp:
                    return self._json(400, {"ok": False, "error": "malformed SDP"})
                try:
                    answer = webrtc.handle_offer(sdp, type_, self.user)
                except Exception as e:
                    return self._json(503, {"ok": False, "error": str(e)})
                return self._json(200, {"ok": True, **answer})
            if path == "/api/fit":
                return self._json(200, {"ok": True, "windows": windows.fit_windows()})
            if path == "/api/window/action":  # #60/#61: close|minimize|maximize|restore|move|resize|layout|focus
                d = self._body()
                r = windows.window_action(
                    d.get("hid"),
                    d.get("action", ""),
                    x=d.get("x"),
                    y=d.get("y"),
                    w=d.get("w"),
                    h=d.get("h"),
                    slot=d.get("slot"),
                )
                return self._json(200 if r.get("ok") else 400, {"ok": r.get("ok", False), **r})
            if path == "/api/geometry":  # #76: toggle / reset per-app geometry remembering
                d = self._body()
                return self._json(
                    200, {"ok": True, **windows.geometry_settings(d.get("enabled"), bool(d.get("reset")))}
                )
            if path == "/api/settings":  # #72/#76: opt-in toggles
                d = self._body()
                r = state.save_setting(str(d.get("key", "")), d.get("value"))
                if r is None:
                    return self._json(400, {"ok": False, "error": "unknown setting or non-boolean value"})
                return self._json(200, {"ok": True, "settings": r})
            if path == "/api/audio/sink":  # #94: switch the capture sink
                ok, err = audio.select_sink(str(self._body().get("name", "")))
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path == "/api/audio/volume":  # #94: sink volume + mute (client debounces)
                d = self._body()
                vol, mute = d.get("volume"), d.get("mute")
                if vol is None and mute is None:
                    return self._json(400, {"ok": False, "error": "nothing to change"})
                if vol is not None and (not isinstance(vol, (int, float)) or isinstance(vol, bool)):
                    return self._json(400, {"ok": False, "error": "volume must be a number"})
                if mute is not None and not isinstance(mute, bool):
                    return self._json(400, {"ok": False, "error": "mute must be a boolean"})
                ok, err = audio.set_sink_volume(d.get("sink", ""), vol, mute)
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path == "/api/audio/app-volume":  # #95: per-application volume + mute
                d = self._body()
                vol, mute = d.get("volume"), d.get("mute")
                if vol is None and mute is None:
                    return self._json(400, {"ok": False, "error": "nothing to change"})
                if vol is not None and (not isinstance(vol, (int, float)) or isinstance(vol, bool)):
                    return self._json(400, {"ok": False, "error": "volume must be a number"})
                if mute is not None and not isinstance(mute, bool):
                    return self._json(400, {"ok": False, "error": "mute must be a boolean"})
                ok, err = audio.set_app_volume(d.get("app", ""), vol, mute)
                return self._json(200 if ok else 404, {"ok": ok, "error": err})
            if path == "/api/media/keys":  # #101: MPRIS when possible, keysyms otherwise
                r = audio.media_key(self._body().get("key", ""))
                return self._json(200 if r.get("ok") else 400, {"ok": r.get("ok", False), **r})
            if path == "/api/audio/record":  # #102: start/stop desktop-audio recording
                d = self._body()
                if d.get("action") == "start":
                    ok, err = audio.record_start(fmt=str(d.get("format", "opus")), name=d.get("name"))
                    return self._json(200 if ok else 400, {"ok": ok, "error": err, **audio.record_status()})
                if d.get("action") == "stop":
                    r = audio.record_stop()
                    return self._json(200, {**r, **audio.record_status()})
                return self._json(400, {"ok": False, "error": "action must be start or stop"})
            if path.startswith("/api/resize"):
                q = parse_qs(url.query)
                result = windows.resize_display(q["w"][0], q["h"][0])
                try:  # 31: persist the last client-requested size (bounded)
                    w, h = int(q["w"][0]), int(q["h"][0])
                    state.save_display_state(w, h)
                except (KeyError, ValueError, TypeError):
                    pass
                return self._json(200, result)
            if path.startswith("/api/open/"):
                name = path.split("/", 3)[3]
                return self._json(200, self._open_app(name))
            if path == "/api/paste":
                return self._paste()
            if path == "/api/clipboard/set":
                data = self._body()
                text = data.get("text", "")
                if len(text) > 100_000:
                    return self._json(400, {"ok": False, "error": "text too long"})
                return self._json(200, clipboard.clipboard_set(text, user=self.user))
            if path == "/api/clipboard/paste":
                data = self._body()
                it = clipboard.clip_item(data.get("id", ""), self.user)
                if not it:
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                if it.get("kind") == "image":  # #71: paste images back
                    return self._json(200, clipboard.clipboard_set_image(it))
                return self._json(200, clipboard.clipboard_set(it["text"], user=self.user))
            if path == "/api/clipboard/pin":  # #70
                d = self._body()
                if not clipboard.clip_pin(d.get("id", ""), bool(d.get("pinned")), self.user):
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/tag":  # #70
                d = self._body()
                if not clipboard.clip_tag(d.get("id", ""), d.get("tags", []), self.user):
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                return self._json(200, {"ok": True, "tags": clipboard.clip_tags(self.user)})
            if path == "/api/clipboard/item":
                it = clipboard.clip_item(parse_qs(url.query).get("id", [""])[0], self.user)
                if not it:
                    return self._json(404, {"ok": False, "error": "no such item"})
                return self._json(200, {"id": it["id"], "text": it["text"]})
            if path == "/api/clipboard/remove":
                clipboard.clip_remove(self._body().get("id", ""), self.user)
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/clear":
                clipboard.clip_clear(keep_pinned=True, user=self.user)  # #70: bulk clear preserves pins
                return self._json(200, {"ok": True})
            if path == "/api/store/install":
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                r = store.install_pkg(pkg)
                audit.event(audit.STORE, "install", handler=self, target=pkg, result="ok" if r.get("ok") else "failed")
                return self._json(200, r)
            if path == "/api/store/remove":  # #66
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                r = store.remove_pkg(pkg)
                audit.event(audit.STORE, "remove", handler=self, target=pkg, result="ok" if r.get("ok") else "failed")
                return self._json(200, r)
            if path == "/api/store/update":  # #66
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, store.update_pkg(pkg))
            if path == "/api/store/cancel":  # #66
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, store.cancel_pkg(pkg))
            if path == "/api/custom-apps":  # #63: create/update a custom entry
                r = apps.add_custom_app(self._body())
                return self._json(200 if r.get("ok") else 400, r)
            if path == "/api/pins/folder":  # #64
                r = state.pins_folder_create(self._body().get("name", ""), self.user)
                if r.get("error"):
                    return self._json(400, {"ok": False, **r})
                return self._json(200, {"ok": True, **r})
            if path == "/api/pins/assign":  # #64: a pin lives in at most one folder
                d = self._body()
                r = state.pins_assign(d.get("id", ""), d.get("folder", ""), self.user)
                if r.get("error"):
                    return self._json(400, {"ok": False, **r})
                return self._json(200, {"ok": True, **r})
            if path == "/api/pins/reorder":  # #64
                r = state.pins_reorder(self._body().get("ids", []), self.user)
                if r is None:
                    return self._json(400, {"ok": False, "error": "ids must be a list"})
                return self._json(200, {"ok": True, **r})
            if path == "/api/launches/clear":  # #62: clear history, keep pins + apps
                state.clear_launches(self.user)
                apps._apps_cache.update({"fp": None, "etag": None, "body": b""})
                return self._json(200, {"ok": True})
            if path == "/api/launch":
                data = self._body()
                app_id = data.get("id", "")
                if not app_id or len(app_id) > 200 or ".." in app_id or app_id.startswith("/"):
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                r = apps.launch_desktop(app_id)
                state.record_launch(app_id, self.user)
                audit.event(audit.APP, "launch", handler=self, target=app_id, result="ok" if r.get("ok") else "failed")
                if not r.get("ok"):
                    r["rid"] = self.rid  # #75: correlation id for the log lookup
                return self._json(200, r)
            if path == "/api/store/launch":
                r = apps.self_launch_name(self._body().get("name", ""))
                if not r.get("ok"):
                    r["rid"] = self.rid  # #75
                return self._json(200, r)
            if path == "/api/upload":  # legacy single-shot upload (atomic, #15/#78)
                return self._upload(url)
            if path == "/api/files/delete":  # #79: single name (legacy) or bulk paths
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                if root is None:
                    root = files.default_root()
                files.require_writable(root)  # a read-only root refuses the whole batch (#84)
                r = files.delete_many(root, d.get("paths") or [d.get("name", "")])
                files.audit(
                    "delete", self, root=root, detail={"deleted": len(r["deleted"]), "failed": len(r["failed"])}
                )
                return self._json(200 if r["ok"] else 207, {"ok": r["ok"], **r})
            if path == "/api/files/mkdir":  # #79
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                r = files.mkdir(root, d.get("path", ""))
                files.audit("mkdir", self, root=root, path=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/rename":  # #79
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                r = files.rename(root, d.get("path", ""), d.get("name", ""))
                files.audit("rename", self, root=root, path=d.get("path"), dest=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path in ("/api/files/move", "/api/files/copy"):  # #79
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                root2 = files.resolve_root(d.get("root2", ""), user=self.user, cap=self.cap) or root
                action = "copy" if path.endswith("copy") else "move"
                results = [
                    files.transfer(
                        root,
                        p,
                        root2,
                        d.get("dest", ""),
                        overwrite=bool(d.get("overwrite")),
                        copy=(action == "copy"),
                    )
                    for p in d.get("paths", [])
                ]
                files.audit(action, self, root=root, dest=d.get("dest"), detail={"count": len(results)})
                return self._json(200, {"ok": True, "results": results})
            if path == "/api/files/chmod":  # #90
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                try:
                    mode = int(str(d.get("mode", "")), 8)
                except ValueError:
                    return self._json(400, {"ok": False, "error": "mode must be octal (e.g. 750)"})
                r = files.chmod(root, d.get("path", ""), mode)
                files.audit("chmod", self, root=root, path=d.get("path"), detail={"mode": oct(mode)})
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/archive":  # #85
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                r = files.archive_create(root, d.get("paths", []), d.get("format", "zip"), d.get("name", ""))
                files.audit("archive", self, root=root, path=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/extract":  # #85
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                r = files.archive_extract(root, d.get("path", ""), d.get("dest", ""))
                files.audit("extract", self, root=root, path=d.get("path"), dest=r.get("dest"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/upload/init":  # #78: resumable session
                d = self._body()
                root = files.resolve_root(d.get("root", ""), user=self.user, cap=self.cap)
                r = files.upload_init(
                    root,
                    d.get("path", ""),
                    d.get("name", ""),
                    d.get("size", 0),
                    d.get("sha256", ""),
                    d.get("chunk_size", 0),
                    resume=d.get("session", ""),
                )
                files.audit("upload_init", self, root=root, path=r.get("path"), detail=r)
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/upload/chunk":  # #78: one verified chunk
                q = parse_qs(url.query)
                try:
                    offset = int(q.get("offset", ["-1"])[0])
                except ValueError:
                    offset = -1
                return self._upload_chunk(q.get("session", [""])[0], offset)
            if path == "/api/files/upload/complete":  # #78: verify + move into place
                d = self._body()
                r = files.upload_complete(d.get("session", ""))
                files.audit(
                    "upload",
                    self,
                    root=files.resolve_root(r.get("root", ""), user=self.user, cap=self.cap),
                    path=r.get("path"),
                    detail={"size": r.get("size")},
                )
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/upload/cancel":  # #78/#88
                d = self._body()
                r = files.upload_cancel(d.get("session", ""))
                files.audit("upload_cancel", self, detail={"session": d.get("session", "")})
                return self._json(200, {"ok": True, **r})
            if path.startswith("/api/files/transfers/"):  # #88: pause/resume/cancel/retry
                action = path.rsplit("/", 1)[-1]
                fn = {
                    "pause": files.transfer_pause,
                    "resume": files.transfer_resume,
                    "cancel": files.transfer_cancel,
                    "retry": files.transfer_retry,
                }.get(action)
                if fn is None:
                    return self._json(404, {"ok": False, "error": "no such transfer action"})
                r = fn(self._body().get("id", ""))
                files.audit("transfer_" + action, self, detail=r)
                return self._json(200, {"ok": True, **r})
            if path == "/api/pins/toggle":
                app_id = self._body().get("id", "")
                if not app_id or len(app_id) > 200:
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                pins = state.load_pins(self.user)
                if app_id in pins:
                    pins.remove(app_id)
                else:
                    pins.insert(0, app_id)
                state.save_pins(pins, self.user)
                return self._json(200, {"ok": True, "pins": pins})
            if path == "/api/backup":
                bdir = state.snapshot(reason="api")
                if not bdir:
                    return self._json(500, {"ok": False, "error": "backup failed"})
                audit.event(audit.ADMIN, "backup", handler=self, result="ok")
                return self._json(200, {"ok": True, "path": bdir, "backups": state.list_backups()})
            # ------------------------------------------------ v2.6.0 security (#109-#123)
            if path == "/api/account/password":  # #118: rotate without downtime
                return self._account_password()
            if path == "/api/2fa/setup":  # #109: stage a TOTP secret
                secret = totp.generate_secret()
                if not users.totp_set_pending(self.user, secret):
                    return self._json(500, {"ok": False, "error": "cannot stage secret"})
                audit.event(audit.SECURITY, "2fa_setup", handler=self, target=self.user)
                return self._json(200, {"ok": True, "secret": secret, "uri": totp.otpauth_uri(self.user, secret)})
            if path == "/api/2fa/enable":  # #109: activate + show recovery codes once
                d = self._body()
                rec = users.record(self.user) or {}
                pending = (rec.get("totp") or {}).get("pending")
                if not pending or not totp.verify(pending, str(d.get("code", ""))):
                    audit.event(audit.SECURITY, "2fa_enable", handler=self, target=self.user, result="failed")
                    return self._json(400, {"ok": False, "error": "invalid code; check the clock and try again"})
                if not users.totp_enable(self.user, pending):
                    return self._json(500, {"ok": False, "error": "cannot enable"})
                codes = totp.generate_recovery_codes()
                hashes = [totp.hash_recovery_code(c) for c in codes]
                users.recovery_set(self.user, [h for h in hashes if h])
                audit.event(audit.SECURITY, "2fa_enabled", handler=self, target=self.user)
                return self._json(200, {"ok": True, "recovery_codes": codes})
            if path == "/api/2fa/disable":  # #109: requires the current factor
                d = self._body()
                rec = users.record(self.user) or {}
                t = rec.get("totp") or {}
                if not auth.verify_user_password(self.user, str(d.get("password", ""))):
                    audit.event(audit.SECURITY, "2fa_disable", handler=self, target=self.user, result="failed")
                    return self._json(401, {"ok": False, "error": "password required"})
                if t.get("enabled") and not totp.verify(t.get("secret") or "", str(d.get("code", ""))):
                    audit.event(audit.SECURITY, "2fa_disable", handler=self, target=self.user, result="failed")
                    return self._json(401, {"ok": False, "error": "current 2FA code required"})
                users.totp_disable(self.user)
                audit.event(audit.SECURITY, "2fa_disabled", handler=self, target=self.user)
                return self._json(200, {"ok": True})
            if path == "/api/2fa/recovery/regenerate":  # #109: fresh codes invalidate the old
                d = self._body()
                rec = users.record(self.user) or {}
                t = rec.get("totp") or {}
                if not t.get("enabled") or not totp.verify(t.get("secret") or "", str(d.get("code", ""))):
                    return self._json(401, {"ok": False, "error": "current 2FA code required"})
                codes = totp.generate_recovery_codes()
                users.recovery_set(self.user, [h for h in (totp.hash_recovery_code(c) for c in codes) if h])
                audit.event(audit.SECURITY, "2fa_recovery_regenerated", handler=self, target=self.user)
                return self._json(200, {"ok": True, "recovery_codes": codes})
            if path == "/api/sessions/revoke":  # #114
                d = self._body()
                n = auth.session_revoke(str(d.get("id", ""))[:64], user=self.user)
                if not n:
                    return self._json(404, {"ok": False, "error": "no such session"})
                audit.event(audit.SESSION, "revoke", handler=self, target=d.get("id", "")[:12])
                return self._json(200, {"ok": True, "revoked": n})
            if path == "/api/sessions/revoke-all":  # #114: everything but this device
                d = self._body()
                n = auth.session_revoke_user(self.user, except_prefix=getattr(self, "sid", "") or None)
                audit.event(audit.SESSION, "revoke_all", handler=self, target=self.user, detail={"count": n})
                return self._json(200, {"ok": True, "revoked": n})
            if path == "/api/notify/test":  # #123
                notify.test_event(self.user)
                return self._json(200, {"ok": True, "msg": "test notification queued; it appears on the next visit"})
            if path == "/api/admin/user":  # #111: create/update one account
                d = self._body()
                return self._admin_user(d)
            if path == "/api/admin/keys/rotate":  # #118: global signing-key rotation
                d = self._body()
                keep = d.get("keep_sessions") is True
                v = auth.rotate_key(reason=f"rotate by {self.user}", revoke_all=not keep)
                audit.event(audit.ADMIN, "key_rotate", handler=self, target=f"v{v}", result="ok")
                return self._json(200, {"ok": True, "key_version": v})
            if path == "/api/webauthn/register/options":  # #110 (gated)
                return self._webauthn_guard(lambda: webauthn.registration_options(self.user, self))
            if path == "/api/webauthn/register/verify":
                return self._webauthn_guard(lambda: webauthn.registration_verify(self.user, self, self._body()))
            return self._json(404, {"error": "not found"})
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log.log(f"POST {path} error: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    # ---- DELETE (#63/#64): custom apps and pin folders
    def do_DELETE(self):
        self._new_request_id()
        url = urlparse(self.path)
        path = url.path
        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if path not in CSRF_EXEMPT and not auth._csrf_ok(self):  # #116
            audit.event(audit.SECURITY, "csrf_rejected", handler=self, target=path, result="denied")
            return self._json(403, {"ok": False, "error": "csrf token missing or invalid"})
        r = self._require(self._cap_needed(path, _DELETE_CAPS))
        if r:
            return r
        try:
            if path == "/api/custom-apps":  # #63
                r = apps.delete_custom_app(parse_qs(url.query).get("id", [""])[0])
                return self._json(200 if r.get("ok") else 404, r)
            if path == "/api/pins/folder":  # #64: pins stay pinned under Unfiled
                return self._json(
                    200, {"ok": True, **state.pins_folder_delete(parse_qs(url.query).get("name", [""])[0], self.user)}
                )
            if path == "/api/admin/users":  # #111: delete an account
                username = parse_qs(url.query).get("username", [""])[0].strip()
                ok, err = users.delete(username)
                if ok:
                    auth.session_revoke_user(username)
                audit.event(audit.ADMIN, "user_delete", handler=self, target=username, result="ok" if ok else "failed")
                return self._json(200 if ok else 400, {"ok": ok, "error": err})
            if path == "/api/webauthn/credentials":  # #110: remove one passkey
                n, err = webauthn.remove_credential(self.user, parse_qs(url.query).get("id", [""])[0])
                if err:
                    return self._json(404, {"ok": False, "error": err})
                audit.event(audit.SECURITY, "passkey_removed", handler=self, target=self.user)
                return self._json(200, {"ok": True, "removed": n})
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log.log(f"DELETE {path} error: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    def _upload(self, url):
        """Legacy single-shot upload (v2.0.1): atomic temp+rename (#15),
        now also quota/reserve-checked, root-aware (#84/#89) and
        user-scoped (#120: the file lands in the requesting user's root)."""
        name = os.path.basename(parse_qs(url.query).get("name", [""])[0])[:200]
        root = files.default_root(user=self.user, cap=self.cap)
        if root is None:
            return self._json(404, {"ok": False, "error": "no such root"})
        p = files._safe_file_path_in(root.path, name)
        if not p:
            return self._json(400, {"ok": False, "error": "invalid filename"})
        try:
            remaining = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            remaining = 0
        if remaining > files.UPLOAD_LIMIT:
            return self._json(413, {"ok": False, "error": "file too large (2GB limit)"})
        try:
            files.require_writable(root)
            files.quota_check(root, remaining)  # #89: refuse BEFORE writing
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        os.makedirs(root.path, exist_ok=True)
        # atomic upload (#15): stream to a hidden temp file in the same
        # directory, fsync, then rename over the target so a crash or
        # full disk can never leave a truncated "real" file behind
        tmp = os.path.join(root.path, f".upload-{os.getpid()}-{int(time.time() * 1000)}.part")
        n = 0
        try:
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    n += len(chunk)
                    remaining -= len(chunk)
                    if n > files.UPLOAD_LIMIT:
                        raise OverflowError
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)  # atomic on the same filesystem
        except OverflowError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(413, {"ok": False, "error": "file too large (2GB limit)"})
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._json(500, {"ok": False, "error": str(e)[:200]})
        files.audit("upload", self, root=root, path=name, size=n)
        return self._json(200, {"ok": True, "name": name, "size": n})

    def _upload_chunk(self, sid, offset):
        """One chunk of a resumable session (#78): bounded raw body."""
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > files.CHUNK_MAX:
            return self._json(400, {"ok": False, "error": f"chunk size must be 1..{files.CHUNK_MAX}"})
        body = self.rfile.read(n)
        if len(body) < n:
            return self._json(400, {"ok": False, "error": "truncated chunk body"})
        r = files.upload_chunk(sid, offset, body, self.headers.get("X-Chunk-Sha256", ""))
        return self._json(200, {"ok": True, **r})

    # ---- files: ranged streaming download (#83)
    def _file_download(self, root, relpath, inline=False, legacy=False):
        try:
            parts = files.split_parts(relpath)
            if not parts:
                return self._json(400, {"ok": False, "error": "no file given"})
            fd = files.open_file_fd(root.path, parts, os.O_RDONLY)
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                return self._json(404, {"ok": False, "error": "not found"})
            size, mtime = st.st_size, int(st.st_mtime)
            etag = f'"f{size:x}-{mtime:x}"'
            name = parts[-1]
            mime = "application/octet-stream" if legacy else files.mime_for(name)
            inline = inline and files.inline_ok(mime)  # #82: unsafe types never inline
            start, end = 0, size - 1
            partial = False
            rng = self.headers.get("Range")
            if rng and self._range_allowed(etag, mtime):
                parsed = files.parse_range(rng, size)
                if parsed == "invalid":
                    os.close(fd)
                    return self._json(
                        416,
                        {"ok": False, "error": "invalid range"},
                        extra={"Content-Range": f"bytes */{size}"},
                    )
                if parsed is not None:
                    start, end = parsed
                    partial = True
            n = end - start + 1 if size else 0
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(n))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Content-Disposition", files.content_disposition(name, inline=inline))  # #86
            self.send_header("Cache-Control", "no-store")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            served = 0
            if not getattr(self, "_head_only", False) and n > 0:
                os.lseek(fd, start, 0)
                left = n
                while left > 0:
                    b = os.read(fd, min(1 << 20, left))  # bounded buffer, never whole-file
                    if not b:
                        break
                    served += len(b)
                    left -= len(b)
                    self.wfile.write(b)
            files.audit("download", self, root=root, path="/".join(parts), size=served)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _range_allowed(self, etag, mtime):
        """If-Range (#83): a mismatching validator must ignore the Range."""
        ir = self.headers.get("If-Range")
        if not ir:
            return True
        if ir == etag:
            return True
        try:
            from email.utils import parsedate_to_datetime
            import calendar

            return calendar.timegm(parsedate_to_datetime(ir).timetuple()) == mtime
        except Exception:
            return False

    # ---- files: thumbnail streaming (#82)
    def _file_thumb(self, root, relpath, size):
        try:
            outcome = files.thumbnail(root, relpath, size)
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        if outcome[0] == "source":
            # small raster image: stream the (already fd-verified) original
            return self._file_download(root, relpath, inline=True)
        cache, mime = outcome
        try:
            st = os.stat(cache)
        except OSError:
            return self._json(501, {"ok": False, "error": "thumbnail unavailable"})
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            with open(cache, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    # ---- PWA share target (#87): multipart/form-data POST from the OS share sheet
    def _share_upload(self):
        from urllib.parse import parse_qs as _pqs

        q = _pqs(urlparse(self.path).query)
        root = files.resolve_root(q.get("root", [""])[0], user=self.user, cap=self.cap) or files.default_root(
            user=self.user, cap=self.cap
        )
        try:
            files.require_writable(root)
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._json(400, {"ok": False, "error": "expected multipart/form-data"})
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            return self._json(400, {"ok": False, "error": "missing multipart boundary"})
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n > files.UPLOAD_LIMIT:
            return self._json(413, {"ok": False, "error": "share too large (2GB limit)"})
        try:
            files.quota_check(root, n)  # #89
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        try:
            saved = files.share_target_save(root, self.rfile, n, m.group(1).encode())
        except files.FilesError as e:
            return self._json(e.status, {"ok": False, "error": str(e)})
        for s in saved:
            files.audit("upload", self, root=root, path=s.get("name"), size=s.get("size"))
        # the share sheet is rendered by the OS: land on the app shell
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- endpoint bodies
    OPEN_MAP = {
        "discord": lambda: windows.focus_or_launch("discord|Discord", ["discord"]),
        "chrome": lambda: windows.focus_or_launch(
            "Google-chrome",
            [
                "google-chrome",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
            ],
        ),
        "firefox": lambda: windows.focus_or_launch("Navigator|firefox", ["firefox"]),
        "nautilus": lambda: windows.focus_or_launch("nautilus|Nautilus", ["nautilus"]),
        "terminal": lambda: procs.run_detached(["xterm"]),
    }

    def _open_app(self, name):
        fn = self.OPEN_MAP.get(name)
        if fn:
            return fn()
        return apps.launch_desktop(name)

    def _paste(self):
        """Paste into the focused window (#74): mode "keys" types via
        xdotool (reliable fallback), mode "clipboard" puts the text on the
        X clipboard and sends Ctrl+V (fast, preserves formatting)."""
        data = self._body()
        text = data.get("text", "")
        mode = data.get("mode", "keys")
        if not text:
            return self._json(200, {"ok": True})
        if len(text) > 100_000:
            return self._json(400, {"ok": False, "error": "text too long"})
        if mode == "clipboard":
            r = clipboard.clipboard_set(text, user=self.user)
            if not r.get("ok"):
                return self._json(500, {"ok": False, "error": "could not set the X clipboard"})
            rc, _, err = procs._run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=10)
            return self._json(200 if rc == 0 else 500, {"ok": rc == 0, "error": None if rc == 0 else err[:200]})
        # keys mode (default): type it out; very large text is refused with
        # an actionable alternative (#74)
        if len(text) > 5000:
            return self._json(
                400,
                {
                    "ok": False,
                    "error": f"text too large to type ({len(text)} chars; limit 5000)",
                    "hint": "use mode 'clipboard' (or the Paste via clipboard button) instead",
                },
            )
        timeout = 15 + int(len(text) * 0.02)  # 12 ms per char + margin
        rc, _, err = procs._run(["xdotool", "type", "--clearmodifiers", "--delay", "12", text], timeout=timeout)
        return self._json(200 if rc == 0 else 500, {"ok": rc == 0, "error": None if rc == 0 else err[:200]})

    def _passkey_login(self):
        """Finish a passkey login (#110): verify the assertion, mint a
        session exactly like a password login (2FA-free — the passkey IS
        a second factor)."""
        try:
            body = self._body()
        except Exception:
            return self._json(400, {"ok": False, "error": "bad request"})
        try:
            user = webauthn.authentication_verify(self, body)
        except webauthn.WebAuthnError as e:
            audit.event(audit.AUTH, "login_passkey", handler=self, result="failed")
            return self._json(401, {"ok": False, "error": str(e)})
        except Exception as e:
            log.log(f"passkey login error: {e}")
            return self._json(400, {"ok": False, "error": "malformed assertion"})
        if not users.is_active(user):
            audit.event(audit.AUTH, "login_passkey", user=user, handler=self, result="disabled")
            return self._json(401, {"ok": False, "error": "account disabled"})
        users.ensure_layout(user)
        sid, token = auth.session_create(user, self)
        users.touch_last_login(user)
        csrf = auth.csrf_token(sid)
        audit.event(audit.AUTH, "login_passkey", user=user, handler=self, result="ok")
        notify.on_login(user, self)
        return self._json(
            200,
            {"ok": True, "user": user, "role": users.role(user) or "operator", "csrf": csrf},
            extra={"Set-Cookie": [auth.cookie_header(self, token), auth.csrf_cookie_header(csrf)]},
        )

    def _login(self):
        """Multi-user login (#111): rate limit -> password -> TOTP/recovery
        (#109) -> session create (#114) -> cookies (#119) -> CSRF token
        (#116) -> audit (#113) -> notifications (#123)."""
        ip = auth._client_ip(self)
        if not auth.origin_ok(self):  # #116: cross-origin login refused
            audit.event(audit.SECURITY, "login", handler=self, target=self.path, result="cross-origin")
            return self._json(403, {"ok": False, "error": "cross-origin login refused"})
        if not auth._login_allowed(ip):
            log.log(f"login rate-limited: {ip}")
            audit.event(audit.AUTH, "login", handler=self, result="rate-limited")
            return self._json(429, {"ok": False, "error": "too many attempts, wait 10 minutes"})
        try:
            data = self._body()
        except Exception:
            return self._json(400, {"ok": False, "error": "bad request"})
        username = str(data.get("username", "")).strip()[:64]
        password = str(data.get("password", ""))
        code = str(data.get("code") or data.get("totp") or "").strip()

        if not auth.verify_user_password(username, password):
            auth.record_login_fail(ip, username)  # #115: persistent + fail2ban event
            audit.event(audit.AUTH, "login", user=username, handler=self, target=ip, result="failed")
            log.log(f"login failed: {ip}")
            return self._json(401, {"ok": False})

        # second factor (#109): required when enrolled (or globally enforced)
        rec = users.record(username) or {}
        t = rec.get("totp") or {}
        if t.get("enabled"):
            secret = t.get("secret") or ""
            if not code:
                log.log(f"login 2fa required: {ip}")
                return self._json(
                    401, {"ok": False, "needs_2fa": True, "error": "enter the 6-digit code from your authenticator app"}
                )
            if not totp.verify(secret, code):
                used_recovery = users.recovery_consume(username, code)  # one-time codes (#109)
                if not used_recovery:
                    auth.record_login_fail(ip, username)
                    audit.event(audit.AUTH, "login_2fa", user=username, handler=self, result="failed")
                    return self._json(401, {"ok": False, "needs_2fa": True, "error": "invalid 2FA code"})
                audit.event(audit.AUTH, "login_recovery", user=username, handler=self, result="ok")
        elif config.TOTP_REQUIRED:
            return self._json(
                403,
                {
                    "ok": False,
                    "error": "two-factor authentication is enforced (RD_2FA_REQUIRED) but this account has none; "
                    "an admin must enroll it first",
                },
            )

        if not users.is_active(username):  # disabled account (#111)
            audit.event(audit.AUTH, "login", user=username, handler=self, result="disabled")
            return self._json(401, {"ok": False, "error": "account disabled"})

        users.ensure_layout(username)  # per-user state dirs (#120)
        sid, token = auth.session_create(username, self)
        users.touch_last_login(username)
        auth.LOGIN_ATTEMPTS.pop(ip, None)  # a success clears this IP's counter
        auth.rate_save()
        csrf = auth.csrf_token(sid)
        audit.event(audit.AUTH, "login", user=username, handler=self, target=ip, result="ok")
        notify.on_login(username, self)  # never blocks (#123)
        log.log(f"login ok: {ip}")
        return self._json(
            200,
            {"ok": True, "user": username, "role": rec.get("role") or "admin", "csrf": csrf},
            extra={
                "Set-Cookie": [auth.cookie_header(self, token), auth.csrf_cookie_header(csrf)],
                "X-CSRF-Token": csrf,
            },
        )

    # ---- v2.6.0 helpers

    def _account_password(self):
        """Authenticated password rotation (#118): no restart, immediate
        effect, sessions revoked unless the caller keeps them."""
        d = self._body()
        current, new = str(d.get("current", "")), str(d.get("new", ""))
        if not auth.verify_user_password(self.user, current):
            audit.event(audit.AUTH, "password_change", user=self.user, handler=self, result="failed")
            return self._json(401, {"ok": False, "error": "current password required"})
        if len(new) < 8:
            return self._json(400, {"ok": False, "error": "password must be at least 8 characters"})
        if users.is_default_admin(self.user):
            # #118: rotate the signing key first (grace keeps live devices),
            # then sync .passhash + the store; no restart needed.
            auth.rotate_key(reason="password change", grace=config.KEY_GRACE, revoke_all=False)
        ok, err = users.set_password(self.user, new)
        if not ok:
            return self._json(500, {"ok": False, "error": err or "cannot set password"})
        if users.is_default_admin(self.user):
            auth.reload()  # re-read .passhash into the process (#118)
        keep = d.get("keep_sessions") is True
        n = auth.session_revoke_user(self.user, except_prefix=(getattr(self, "sid", "") or None) if keep else None)
        audit.event(
            audit.AUTH,
            "password_change",
            user=self.user,
            handler=self,
            detail={"revoked_sessions": n, "key_rotated": users.is_default_admin(self.user)},
        )
        return self._json(200, {"ok": True, "revoked_sessions": n})

    def _admin_user(self, d):
        """Account management (#111/#112): create, role, status (disable
        revokes sessions), reset password, delete. Admin-only (route table)."""
        action = str(d.get("action", ""))
        username = str(d.get("username", "")).strip()
        if action == "create":
            ok, err = users.create(username, str(d.get("password", "")), role=d.get("role") or users.DEFAULT_ROLE)
            audit.event(audit.ADMIN, "user_create", handler=self, target=username, result="ok" if ok else "failed")
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        if action == "role":
            ok, err = users.set_role(username, d.get("role", ""))
            if ok:  # #114: rotate tokens on privilege change
                auth.session_revoke_user(username)
            audit.event(audit.ADMIN, "user_role", handler=self, target=username, detail={"role": d.get("role", "")})
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        if action == "status":
            ok, err = users.set_status(username, d.get("status", ""))
            if ok and d.get("status") != "active":  # #111: disabling revokes sessions
                auth.session_revoke_user(username)
            audit.event(
                audit.ADMIN, "user_status", handler=self, target=username, detail={"status": d.get("status", "")}
            )
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        if action == "password":
            ok, err = users.set_password(username, str(d.get("password", "")))
            if ok:  # the user's old sessions die with the old credential
                auth.session_revoke_user(username)
            audit.event(audit.ADMIN, "user_password_reset", handler=self, target=username)
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        if action == "delete":
            ok, err = users.delete(username)
            if ok:
                auth.session_revoke_user(username)
            audit.event(audit.ADMIN, "user_delete", handler=self, target=username, result="ok" if ok else "failed")
            return self._json(200 if ok else 400, {"ok": ok, "error": err})
        return self._json(400, {"ok": False, "error": "action must be create|role|status|password|delete"})

    def _webauthn_guard(self, fn):
        """Run a passkey ceremony; feature-flagged (#110)."""
        if not webauthn.enabled():
            return self._json(
                403,
                {
                    "ok": False,
                    "error": "passkeys are disabled; set RD_WEBAUTHN=1 and install the optional "
                    "'webauthn' package to enable them",
                },
            )
        try:
            return self._json(200, {"ok": True, **fn()})
        except webauthn.WebAuthnError as e:
            audit.event(audit.SECURITY, "webauthn", handler=self, target=self.path, result="failed")
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log.log(f"webauthn error: {e}")
            return self._json(503, {"ok": False, "error": "passkey backend unavailable"})


# ------------------------------------------------------------- QR rendering (#109)


def _qr_available():
    try:
        from ._vendor import qrcodegen  # noqa: F401  (vendored MIT, docs/VENDORED.md)

        return True
    except Exception:
        return False


def _qr_svg(text, border=4, white="#fff", black="#111"):
    """SVG rendering of a QR code (vendored Nayuki encoder, #109)."""
    from ._vendor import qrcodegen

    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    n = qr.get_size()
    dim = (n + border * 2) * 8
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" width="{dim}" height="{dim}"'
        f' role="img" aria-label="2FA QR code">',
        f'<rect width="{dim}" height="{dim}" fill="{white}"/>',
    ]
    for y in range(n):
        for x in range(n):
            if qr.get_module(x, y):
                parts.append(
                    f'<rect x="{(x + border) * 8}" y="{(y + border) * 8}" width="8" height="8" fill="{black}"/>'
                )
    parts.append("</svg>")
    return "".join(parts).encode()


def _totp_pending_secret(user):
    """The staged-but-not-yet-confirmed TOTP secret, or None."""
    rec = users.record(user) or {}
    return (rec.get("totp") or {}).get("pending") or None


# ------------------------------------------------------------------ main

_shutdown_started = False


def _shutdown(httpd, signum, _frame):
    """Graceful shutdown (#25): stop accepting, close streams, reap children.
    Clipboard and pins are persisted on every change and flushed once more
    here. A second signal forces a fast exit."""
    global _shutdown_started
    if _shutdown_started:
        log.log(f"signal {signum}: second signal, forcing exit")
        os._exit(1)
    _shutdown_started = True
    log.log(f"signal {signum}: shutting down, reaping children")
    audio.record_stop()  # #102: finalize the recording so the file stays playable
    vncproxy.shutdown_all()  # wake/exit websocket proxy threads
    procs.reap_children(timeout=3)
    clipboard.flush()  # persist clipboard (pins are saved on change)
    threading.Thread(target=httpd.shutdown, daemon=True).start()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    # CLI modes that must work without binding the socket (#23, #30)
    if "--check-config" in argv:
        try:
            config.check()
        except config.ConfigError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"config OK (port {config.PORT}, files {config.FILES_DIR}, vnc {config.VNC_HOST}:{config.VNC_PORT})")
        return 0
    if "--gen-config-docs" in argv:
        rest = argv[argv.index("--gen-config-docs") + 1 :]
        path = rest[0] if rest and not rest[0].startswith("--") else None
        print(config.write_env_doc(path), "written")
        return 0
    if "--export-state" in argv:
        path = argv[argv.index("--export-state") + 1] if len(argv) > argv.index("--export-state") + 1 else ""
        if not path:
            print("usage: control.py --export-state PATH.tar.gz", file=sys.stderr)
            return 2
        r = state.export_state(path)
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    if "--import-state" in argv:
        path = argv[argv.index("--import-state") + 1] if len(argv) > argv.index("--import-state") + 1 else ""
        if not path:
            print("usage: control.py --import-state PATH.tar.gz", file=sys.stderr)
            return 2
        r = state.import_state(path)
        print(json.dumps(r))
        return 0 if r["ok"] else 1

    log.setup()

    # validate BEFORE binding the socket (#23)
    try:
        config.check()
    except config.ConfigError as e:
        log.error(str(e))
        print(str(e), file=sys.stderr)
        return 2
    for w in config.exposure_warnings():  # #122: warn when VNC/bind leaves localhost
        log.log(f"security warning: {w}")
        print(f"warning: {w}", file=sys.stderr)
    for err in config.ensure_dirs(config.VALUES):
        log.error(err)
        print(err, file=sys.stderr)
        return 2

    users.migrate_single_user()  # #111: single .passhash -> default admin
    auth.rate_load()  # #115: a restart must not clear a lockout
    auth.reload()  # #118: pick up .passhash changes while stopped
    state.snapshot("startup")  # 30: versioned backup of runtime state
    for err in files.setup_roots():  # #84: invalid/missing roots reported at startup
        log.error(err)
        print(err, file=sys.stderr)
    audio.record_recover()  # #102: surface recording fragments from a crash
    audio.restore_state()  # #94: re-apply sink selection, volume and mute
    clipboard._clip_load()
    files.cleanup_partial_uploads()
    files.gc_transfers()  # #88: expire finished/abandoned transfer jobs
    store.restore_jobs()  # #66: job status survives a restart
    clipboard.prime_watcher()
    threading.Thread(target=clipboard.clipboard_watcher, daemon=True).start()
    threading.Thread(target=files.gc_loop, daemon=True, name="files-gc").start()  # #78/#88

    httpd = BoundedThreadingHTTPServer((config.BIND_HOST, config.PORT), Handler)

    def _handle(sig, frame):
        _shutdown(httpd, sig, frame)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGHUP"):  # #118: reload credentials without a restart

        def _hup(sig, frame):
            changed = auth.reload()
            log.log(f"SIGHUP: credential reload {'applied' if changed else '(no changes)'}")

        signal.signal(signal.SIGHUP, _hup)
    log.log(f"control server starting on {config.BIND_HOST}:{config.PORT} (v{config.VERSION})")
    print(f"Remote control server: http://{config.BIND_HOST}:{config.PORT}  (log: {config.LOG_FILE})")

    def _ready_log():
        time.sleep(0.5)
        ready.ready_once()

    threading.Thread(target=_ready_log, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        vncproxy.shutdown_all()
        procs.reap_children()
        clipboard.flush()
        httpd.server_close()
        log.log("control server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
