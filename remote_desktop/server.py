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
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import (
    apps,
    audio,
    auth,
    clipboard,
    config,
    files,
    http,
    limits,
    logging as log,
    media_in,
    procs,
    ready,
    state,
    stats,
    store,
    vncproxy,
    windows,
)

WEB_DIR = config.WEB_DIR


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
    # ---- streaming audio (needs custom response, not _json)
    def _stream_audio(self, mime, codec_args):
        if not limits.gate(self, "audio", "audio streams"):
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            cmd = [
                "ffmpeg",
                "-loglevel",
                "warning",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-f",
                "pulse",
                "-i",
                audio.audio_source(),
                *codec_args,
            ]
            try:
                proc = procs.spawn_stream(
                    cmd,
                    env=config._pa_env(),
                    stdout=subprocess.PIPE,
                    stderr=open(audio.AUDIO_LOG, "ab"),
                    preexec_fn=procs.die_with_parent,
                )
            except Exception as e:
                log.log(f"ffmpeg start failed: {e}")
                return
            started = time.time()
            log.log(f"audio open  {self.path.split('?')[0]} {auth._client_ip(self)}")
            try:
                while True:
                    chunk = proc.stdout.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away; normal on mobile when the tab is backgrounded
            finally:
                with procs._child_procs_lock:
                    procs.child_procs.discard(proc)
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
                log.log(f"audio close {self.path.split('?')[0]} after {time.time() - started:.0f}s")
        finally:
            limits.release("audio")

    # ---- GET
    def do_GET(self):
        self._new_request_id()
        url = urlparse(self.path)
        path = url.path

        # websocket upgrade for the VNC stream (same origin, same port)
        if path == "/websockify" or path.startswith("/websockify/"):
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
        if path == "/api/auth_check":
            return self._json(200, {"ok": True})
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
        if path == "/api/clipboard":
            return self._json(200, clipboard.clipboard_get())
        if path == "/api/clipboard/history":  # #70: q= full-text search, tag= filter
            q = parse_qs(url.query)
            items = clipboard.clip_history(q.get("q", [None])[0], q.get("tag", [None])[0])
            return self._json(200, {"items": items, "tags": clipboard.clip_tags()})
        if path == "/api/clipboard/sync":  # #72: desktop -> phone push (opt-in)
            since = parse_qs(url.query).get("since", [""])[0][:64]
            return self._json(200, clipboard.clip_sync(since))
        if path == "/api/clipboard/image":  # #71: serve a stored image
            it = clipboard.clip_item(parse_qs(url.query).get("id", [""])[0])
            p = clipboard.clip_image_path(it["id"]) if it else None
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
            return self._json(200, {"ok": True, **state.pins_snapshot()})
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
            return self._stream_audio(
                "application/octet-stream", ["-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-f", "s16le", "-"]
            )
        if path == "/api/audio.opus":
            return self._stream_audio(
                'audio/webm; codecs="opus"',
                ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on", "-frame_duration", "20", "-f", "webm", "-"],
            )
        if path == "/api/stats":
            return self._json(200, stats.system_stats())
        # ---- files & transfer (#78/#79/#81/#83/#84/#88/#89/#90/#92)
        if path == "/api/files":
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0])
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
                    "roots": files.roots_public(),
                    "root": root.rid,
                    "root_label": root.label,
                    "readonly": root.readonly,
                    **d,
                    "disk": files.disk_info(root),
                },
            )
        if path == "/api/files/roots":  # #84
            return self._json(200, {"ok": True, "roots": files.roots_public(), "errors": files.ROOT_ERRORS})
        if path == "/api/files/search":  # #81: bounded recursive search
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0])
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
            root = files.resolve_root(q.get("root", [""])[0])
            if root is None:
                return self._json(404, {"ok": False, "error": "no such root"})
            return self._file_download(root, q.get("path", [""])[0], inline=q.get("inline", ["0"])[0] == "1")
        if path == "/api/files/thumb":  # #82: bounded cached thumbnail
            q = parse_qs(url.query)
            root = files.resolve_root(q.get("root", [""])[0])
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
            return self._file_download(files.default_root(), name, inline=False, legacy=True)
        if path == "/api/pins":
            return self._json(200, {"pins": state.load_pins()})
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
        if path == "/api/logout":
            return self._json(
                200, {"ok": True}, extra={"Set-Cookie": "rd_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"}
            )

        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        try:
            if path == "/share":  # #87: PWA share-target uploads (multipart)
                return self._share_upload()
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
                return self._json(200, clipboard.clipboard_set(text))
            if path == "/api/clipboard/paste":
                data = self._body()
                it = clipboard.clip_item(data.get("id", ""))
                if not it:
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                if it.get("kind") == "image":  # #71: paste images back
                    return self._json(200, clipboard.clipboard_set_image(it))
                return self._json(200, clipboard.clipboard_set(it["text"]))
            if path == "/api/clipboard/pin":  # #70
                d = self._body()
                if not clipboard.clip_pin(d.get("id", ""), bool(d.get("pinned"))):
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/tag":  # #70
                d = self._body()
                if not clipboard.clip_tag(d.get("id", ""), d.get("tags", [])):
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                return self._json(200, {"ok": True, "tags": clipboard.clip_tags()})
            if path == "/api/clipboard/item":
                it = clipboard.clip_item(parse_qs(url.query).get("id", [""])[0])
                if not it:
                    return self._json(404, {"ok": False, "error": "no such item"})
                return self._json(200, {"id": it["id"], "text": it["text"]})
            if path == "/api/clipboard/remove":
                clipboard.clip_remove(self._body().get("id", ""))
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/clear":
                clipboard.clip_clear(keep_pinned=True)  # #70: bulk clear preserves pins
                return self._json(200, {"ok": True})
            if path == "/api/store/install":
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, store.install_pkg(pkg))
            if path == "/api/store/remove":  # #66
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, store.remove_pkg(pkg))
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
                r = state.pins_folder_create(self._body().get("name", ""))
                if r.get("error"):
                    return self._json(400, {"ok": False, **r})
                return self._json(200, {"ok": True, **r})
            if path == "/api/pins/assign":  # #64: a pin lives in at most one folder
                d = self._body()
                r = state.pins_assign(d.get("id", ""), d.get("folder", ""))
                if r.get("error"):
                    return self._json(400, {"ok": False, **r})
                return self._json(200, {"ok": True, **r})
            if path == "/api/pins/reorder":  # #64
                r = state.pins_reorder(self._body().get("ids", []))
                if r is None:
                    return self._json(400, {"ok": False, "error": "ids must be a list"})
                return self._json(200, {"ok": True, **r})
            if path == "/api/launches/clear":  # #62: clear history, keep pins + apps
                state.clear_launches()
                apps._apps_cache.update({"fp": None, "etag": None, "body": b""})
                return self._json(200, {"ok": True})
            if path == "/api/launch":
                data = self._body()
                app_id = data.get("id", "")
                if not app_id or len(app_id) > 200 or ".." in app_id or app_id.startswith("/"):
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                r = apps.launch_desktop(app_id)
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
                root = files.resolve_root(d.get("root", ""))
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
                root = files.resolve_root(d.get("root", ""))
                r = files.mkdir(root, d.get("path", ""))
                files.audit("mkdir", self, root=root, path=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/rename":  # #79
                d = self._body()
                root = files.resolve_root(d.get("root", ""))
                r = files.rename(root, d.get("path", ""), d.get("name", ""))
                files.audit("rename", self, root=root, path=d.get("path"), dest=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path in ("/api/files/move", "/api/files/copy"):  # #79
                d = self._body()
                root = files.resolve_root(d.get("root", ""))
                root2 = files.resolve_root(d.get("root2", "")) or root
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
                root = files.resolve_root(d.get("root", ""))
                try:
                    mode = int(str(d.get("mode", "")), 8)
                except ValueError:
                    return self._json(400, {"ok": False, "error": "mode must be octal (e.g. 750)"})
                r = files.chmod(root, d.get("path", ""), mode)
                files.audit("chmod", self, root=root, path=d.get("path"), detail={"mode": oct(mode)})
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/archive":  # #85
                d = self._body()
                root = files.resolve_root(d.get("root", ""))
                r = files.archive_create(root, d.get("paths", []), d.get("format", "zip"), d.get("name", ""))
                files.audit("archive", self, root=root, path=r.get("path"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/extract":  # #85
                d = self._body()
                root = files.resolve_root(d.get("root", ""))
                r = files.archive_extract(root, d.get("path", ""), d.get("dest", ""))
                files.audit("extract", self, root=root, path=d.get("path"), dest=r.get("dest"))
                return self._json(200, {"ok": True, **r})
            if path == "/api/files/upload/init":  # #78: resumable session
                d = self._body()
                root = files.resolve_root(d.get("root", ""))
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
                    root=files.resolve_root(r.get("root", "")),
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
                pins = state.load_pins()
                if app_id in pins:
                    pins.remove(app_id)
                else:
                    pins.insert(0, app_id)
                state.save_pins(pins)
                return self._json(200, {"ok": True, "pins": pins})
            if path == "/api/backup":
                bdir = state.snapshot(reason="api")
                if not bdir:
                    return self._json(500, {"ok": False, "error": "backup failed"})
                return self._json(200, {"ok": True, "path": bdir, "backups": state.list_backups()})
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
        try:
            if path == "/api/custom-apps":  # #63
                r = apps.delete_custom_app(parse_qs(url.query).get("id", [""])[0])
                return self._json(200 if r.get("ok") else 404, r)
            if path == "/api/pins/folder":  # #64: pins stay pinned under Unfiled
                return self._json(
                    200, {"ok": True, **state.pins_folder_delete(parse_qs(url.query).get("name", [""])[0])}
                )
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log.log(f"DELETE {path} error: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    def _upload(self, url):
        """Legacy single-shot upload (v2.0.1): atomic temp+rename (#15),
        now also quota/reserve-checked and root-aware (#84/#89)."""
        name = os.path.basename(parse_qs(url.query).get("name", [""])[0])[:200]
        p = files._safe_file_path(name)
        if not p:
            return self._json(400, {"ok": False, "error": "invalid filename"})
        root = files.default_root()
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
        os.makedirs(files.FILES_DIR, exist_ok=True)
        # atomic upload (#15): stream to a hidden temp file in the same
        # directory, fsync, then rename over the target so a crash or
        # full disk can never leave a truncated "real" file behind
        tmp = os.path.join(files.FILES_DIR, f".upload-{os.getpid()}-{int(time.time() * 1000)}.part")
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
        root = files.resolve_root(q.get("root", [""])[0]) or files.default_root()
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
            r = clipboard.clipboard_set(text)
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

    def _login(self):
        ip = auth._client_ip(self)
        if not auth._login_allowed(ip):
            log.log(f"login rate-limited: {ip}")
            return self._json(429, {"ok": False, "error": "too many attempts, wait 10 minutes"})
        try:
            data = self._body()
        except Exception:
            return self._json(400, {"ok": False, "error": "bad request"})
        auth.LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())
        if data.get("username") == auth.AUTH_USER and auth._verify_password(data.get("password", "")):
            log.log(f"login ok: {ip}")
            return self._json(
                200,
                {"ok": True},
                extra={
                    "Set-Cookie": f"rd_session={auth._make_token(auth.AUTH_USER)}; Path=/; HttpOnly; "
                    f"SameSite=Lax; Max-Age={auth.TOKEN_TTL}"
                },
            )
        log.log(f"login failed: {ip}")
        return self._json(401, {"ok": False})


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
    for err in config.ensure_dirs(config.VALUES):
        log.error(err)
        print(err, file=sys.stderr)
        return 2

    state.snapshot("startup")  # 30: versioned backup of runtime state
    for err in files.setup_roots():  # #84: invalid/missing roots reported at startup
        log.error(err)
        print(err, file=sys.stderr)
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
