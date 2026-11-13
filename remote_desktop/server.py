"""HTTP routes, server bootstrap and graceful shutdown.

Ownership: do_GET/do_POST routing (kept behaviour-identical to the
pre-split control.py, #22), the connection-bounded ThreadingHTTPServer
(#28), signal handling and child reaping (#25), startup validation
(#23) and the CLI (--check-config, --export-state, --import-state).
"""

import json
import os
import shutil
import signal
import socket
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
    log,
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

            if path == "/api/auth_check":
                return self._json(200, {"ok": True})
            if path == "/api/windows":
                try:
                    return self._json(200, {"ok": True, "windows": windows.list_windows()})
                except Exception as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            if path == "/api/apps":
                return self._json(200, {"apps": apps.list_desktop_apps()})
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
            if path == "/api/clipboard/history":
                return self._json(200, {"items": clipboard.clip_history()})
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
            if path == "/api/audio/raw":
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
            if path == "/api/files":
                return self._json(200, {"files": files.list_files(), "dir": files.FILES_DIR})
            if path == "/api/download":
                name = parse_qs(url.query).get("name", [""])[0]
                p = files._safe_file_path(name)
                if not p or not os.path.isfile(p):
                    return self._json(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            if path == "/api/pins":
                return self._json(200, {"pins": state.load_pins()})
            if path == "/api/backup":
                return self._json(200, {"ok": True, "backups": state.list_backups()})
            return self._json(404, {"error": "not found"})

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
            if path == "/api/fit":
                return self._json(200, {"ok": True, "windows": windows.fit_windows()})
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
                return self._json(200, clipboard.clipboard_set(it["text"]))
            if path == "/api/clipboard/item":
                it = clipboard.clip_item(parse_qs(url.query).get("id", [""])[0])
                if not it:
                    return self._json(404, {"ok": False, "error": "no such item"})
                return self._json(200, {"id": it["id"], "text": it["text"]})
            if path == "/api/clipboard/remove":
                clipboard.clip_remove(self._body().get("id", ""))
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/clear":
                clipboard.clip_clear()
                return self._json(200, {"ok": True})
            if path == "/api/store/install":
                data = self._body()
                pkg = data.get("pkg", "")
                if not store.valid_pkg_name(pkg):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, store.install_pkg(pkg))
            if path == "/api/launch":
                data = self._body()
                app_id = data.get("id", "")
                if not app_id or len(app_id) > 200 or ".." in app_id or app_id.startswith("/"):
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                return self._json(200, apps.launch_desktop(app_id))
            if path == "/api/store/launch":
                return self._json(200, apps.self_launch_name(self._body().get("name", "")))
            if path == "/api/upload":
                return self._upload(url)
            if path == "/api/files/delete":
                p = files._safe_file_path(self._body().get("name", ""))
                if not p or not os.path.isfile(p):
                    return self._json(404, {"ok": False, "error": "not found"})
                os.remove(p)
                return self._json(200, {"ok": True})
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
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log.log(f"POST {path} error: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    def _upload(self, url):
        name = os.path.basename(parse_qs(url.query).get("name", [""])[0])[:200]
        p = files._safe_file_path(name)
        if not p:
            return self._json(400, {"ok": False, "error": "invalid filename"})
        try:
            remaining = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            remaining = 0
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
        return self._json(200, {"ok": True, "name": name, "size": n})

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
        text = self._body().get("text", "")
        if not text:
            return self._json(200, {"ok": True})
        if len(text) > 100_000:
            return self._json(400, {"ok": False, "error": "text too long"})
        rc, _, err = procs._run(["xdotool", "type", "--clearmodifiers", "--delay", "3", text], timeout=30)
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
    clipboard._clip_load()
    files.cleanup_partial_uploads()
    clipboard.prime_watcher()
    threading.Thread(target=clipboard.clipboard_watcher, daemon=True).start()

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
