"""End-to-end integration tests (#34): the REAL server in a subprocess.

No Xvnc, xdotool or PulseAudio required — X-touching endpoints simply
report failures, VNC is faked with a local TCP echo server, and the
server runs with a fake HOME, files dir, state dir and log file.

Exercises: health, login (+rate limit), auth_check 401/200, clipboard
set/history, files upload/list/download, static 404, websocket upgrade
failure when VNC is absent, bidirectional websocket byte fidelity, and
graceful SIGTERM shutdown (#25). Only pytest + stdlib (urllib, socket).
"""

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# a fixed crypt SHA-512 hash of "rd-test-password" (salt "rdtestsalt"),
# so the test never needs to generate one
TEST_PASSWORD = "rd-test-password"
TEST_HASH = "$6$rdtestsalt$TmcuDB8aen/y0lmO76iwVMuKbwwSrvUoeVJfnWFcnA8gMLhaC9783vAtu.8UQ/6FBMgEOSG6beBVnzb9ZGmq5/"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """The real control.py in a subprocess with a sandboxed environment."""

    def __init__(self, tmp_path, port, vnc_port, passfile):
        self.port = port
        self.log_path = tmp_path / "server.log"
        self.files_dir = tmp_path / "downloads"
        self.home = tmp_path / "home"
        self.state_dir = tmp_path / "xdg" / "remote-desktop"
        for d in (self.files_dir, self.home, self.state_dir):
            os.makedirs(d, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "RD_PORT": str(port),
                "RD_BIND": "127.0.0.1",
                "RD_USER": "admin",
                "RD_PASSFILE": str(passfile),
                "RD_FILES_DIR": str(self.files_dir),
                "RD_STATE_DIR": str(self.state_dir),
                "RD_LOG_FILE": str(self.log_path),
                "VNC_HOST": "127.0.0.1",
                "VNC_PORT": str(vnc_port),
                "XDG_DATA_HOME": str(tmp_path / "xdg"),
                # the tests present themselves as different client IPs via
                # X-Forwarded-For (they connect from 127.0.0.1)
                "RD_TRUSTED_PROXIES": "127.0.0.1",
                # keep X out of the picture
                "DISPLAY": "",
            }
        )
        self.proc = None
        self._csrf_cache = {}  # cookie -> token (#116: the test client sends it like the browser)

    def _csrf(self, cookie):
        tok = self._csrf_cache.get(cookie)
        if not tok:
            code, _, body = self.request("/api/csrf", cookie=cookie, csrf=False)
            if code == 200:
                tok = json.loads(body).get("token") or ""
                self._csrf_cache[cookie] = tok
        return tok

    def start(self):
        # use the coverage entry wrapper when a coverage run is detected,
        # so the child's coverage is measured (#35); behaves like control.py
        entry = os.path.join(REPO, "tests", "coverage_entry.py")
        script = (
            entry if os.path.exists(os.environ.get("COVERAGE_PROCESS_START", "")) else os.path.join(REPO, "control.py")
        )
        self.env["RD_COVERAGE_RC"] = os.environ.get("COVERAGE_PROCESS_START", "")
        if script == entry:
            # the sandboxed HOME would hide the user site-packages that hold
            # coverage; point the child at the real one explicitly
            import site

            site_pkgs = site.getusersitepackages()
            self.env["PYTHONPATH"] = os.pathsep.join(p for p in [site_pkgs, os.environ.get("PYTHONPATH", "")] if p)
        self.proc = subprocess.Popen(
            [sys.executable, script],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=self.log_path.open("ab"),
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("server did not come up; log:\n" + self.log_tail())

    def stop(self, sig=signal.SIGTERM):
        if not self.proc or self.proc.poll() is not None:
            return self.proc.returncode if self.proc else None
        self.proc.send_signal(sig)
        rc = self.proc.wait(timeout=15)
        return rc

    def log_tail(self, lines=60):
        try:
            return "".join(self.log_path.read_text().splitlines(keepends=True)[-lines:])
        except OSError:
            return "(no server log)"

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    # --- stdlib HTTP helpers with timeout + cookie jar -------------------
    def request(
        self, path, data=None, headers=None, method=None, cookie=None, raw=None, timeout=10, ip=None, csrf=None
    ):
        """HTTP request. csrf=False suppresses the X-CSRF-Token the way a
        browser client normally sends it (#116) — used by the tests that
        assert the token is actually enforced."""
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        m = method or ("POST" if (data is not None or raw is not None) else "GET")
        if (
            cookie
            and csrf is not False
            and m in ("POST", "DELETE")
            and "X-CSRF-Token" not in headers
            and not path.startswith("/api/login")
            and path not in ("/api/logout", "/share")
        ):
            tok = self._csrf(cookie)
            if tok:
                headers["X-CSRF-Token"] = tok
        if ip:
            headers["X-Forwarded-For"] = ip
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        if body is not None and "Content-Type" not in headers and not raw:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path), data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()


@pytest.fixture(scope="module")
def vnc_fake_port():
    """A TCP port with NOTHING listening (the 'VNC absent' case)."""
    return _free_port()


@pytest.fixture(scope="module")
def vnc_echo_port():
    """A fake VNC: TCP echo. The FIRST connection echoes one round then
    resets (SO_LINGER 0 → RST, no graceful FIN) — simulating an Xvnc
    crash. Later connections echo persistently."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    import threading

    counter = {"n": 0}

    def echo_loop():
        srv.settimeout(0.5)
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            counter["n"] += 1
            crash = counter["n"] == 1
            t = threading.Thread(target=_echo, args=(conn, crash), daemon=True)
            t.start()

    def _echo(conn, crash_after_first_round):
        conn.settimeout(30)
        try:
            if crash_after_first_round:
                # hard reset instead of a clean FIN: the proxy must survive
                # both the read error and failed writes on this socket
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                data = conn.recv(65536)
                if data:
                    conn.sendall(data)
            else:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
        except Exception:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=echo_loop, daemon=True)
    t.start()
    yield port
    srv.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory, vnc_fake_port):
    tmp_path = tmp_path_factory.mktemp("rd-int")
    passfile = tmp_path / "state" / ".passhash"
    os.makedirs(passfile.parent, exist_ok=True)
    passfile.write_text(TEST_HASH + "\n")
    srv = Server(tmp_path, _free_port(), vnc_fake_port, passfile)
    srv.start()
    yield srv
    rc = srv.stop()
    assert rc == 0, f"server did not exit cleanly (rc={rc})\n{srv.log_tail()}"
    _assert_no_server_children(srv)


def _assert_no_server_children(server):
    if server.proc is None or server.proc.poll() is None:
        return
    try:
        out = subprocess.run(["pgrep", "-P", str(server.proc.pid)], capture_output=True, text=True, timeout=5)
        assert out.stdout.strip() == "", f"orphaned children: {out.stdout}"
    except FileNotFoundError:
        pass  # pgrep unavailable


def _login(server, password=TEST_PASSWORD, user="admin", ip=None):
    code, headers, _ = server.request("/api/login", data={"username": user, "password": password}, ip=ip)
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    return code, cookie


# ---------------------------------------------------------------- health


def test_health_public(server):
    code, _, body = server.request("/api/health")
    assert code == 200
    h = json.loads(body)
    assert h["ok"] is True and h["version"]
    assert "active_streams" in h


def test_health_has_request_id_in_log(server):
    server.request("/api/health")
    log = server.log_tail()
    assert "GET /api/health" in log  # request logged...
    assert "INFO [" in log  # ...with a request id and level (#24)


# ------------------------------------------------------------ auth / login


def test_auth_check_unauthorized(server):
    code, _, body = server.request("/api/auth_check")
    assert code == 401
    assert json.loads(body)["ok"] is False


def test_login_bad_credentials(server):
    code, _ = _login(server, password="wrong")
    assert code == 401


def test_login_ok_then_auth_check_200(server):
    code, cookie = _login(server, ip="192.0.2.11")
    assert code == 200 and cookie.startswith("rd_session=")
    code, _, body = server.request("/api/auth_check", cookie=cookie)
    assert code == 200 and json.loads(body)["ok"] is True


def test_login_rate_limit_429(server):
    # the window is 5 attempts per client IP; this test pins one
    codes = []
    for _ in range(8):
        code, _ = _login(server, password=f"wrong-{time.time()}", ip="192.0.2.7")
        codes.append(code)
    assert codes.count(429) >= 1
    assert codes[-1] == 429
    assert 401 in codes


# ------------------------------------------------------------- clipboard


def test_clipboard_set_and_history(server):
    _, cookie = _login(server, ip="192.0.2.12")
    code, _, body = server.request(
        "/api/clipboard/set", cookie=cookie, data={"text": "integration-clipboard-\u00e9\u00fctest"}
    )
    assert code == 200
    # xclip fails without X — the API must still record history + answer JSON
    r = json.loads(body)
    assert "ok" in r
    code, _, body = server.request("/api/clipboard/history", cookie=cookie)
    assert code == 200
    items = json.loads(body)["items"]
    assert any("integration-clipboard" in i["preview"] for i in items)


def test_clipboard_requires_auth(server):
    code, _, _ = server.request("/api/clipboard/set", data={"text": "x"})
    assert code == 401


# ----------------------------------------------------------- file transfer


def test_files_upload_list_download_delete(server):
    _, cookie = _login(server, ip="192.0.2.13")
    payload = b"integration-file-content-\x00\x01\x02"
    csrf = server._csrf(cookie) or ""
    req = urllib.request.Request(
        server.url(f"/api/upload?name=hello.bin"),
        data=payload,
        method="POST",
        headers={
            "Cookie": cookie,
            "Content-Type": "application/octet-stream",
            "X-CSRF-Token": csrf,  # #116: state-changing requests carry the token
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        up = json.loads(r.read())
    assert up["ok"] is True and up["name"] == "hello.bin" and up["size"] == len(payload)

    code, _, body = server.request("/api/files", cookie=cookie)
    assert code == 200
    names = [f["name"] for f in json.loads(body)["files"]]
    assert names == ["hello.bin"]

    code, headers, body = server.request(f"/api/download?name=hello.bin", cookie=cookie)
    assert code == 200 and body == payload
    assert headers["Content-Type"] == "application/octet-stream"

    code, _, _ = server.request("/api/files/delete", cookie=cookie, data={"name": "hello.bin"})
    assert code == 200
    code, _, _ = server.request(f"/api/download?name=hello.bin", cookie=cookie)
    assert code == 404


def test_upload_traversal_rejected(server):
    # basename sanitisation: "../evil.txt" cannot escape the files dir —
    # the directory component is stripped and the file lands inside it
    _, cookie = _login(server, ip="192.0.2.14")
    code, _, _ = server.request("/api/upload?name=../evil.txt", cookie=cookie, raw=b"x")
    assert code in (200, 400)
    code, _, body = server.request("/api/files", cookie=cookie)
    names = [f["name"] for f in json.loads(body)["files"]]
    assert all("/" not in n and ".." not in n for n in names)
    # nothing was written outside the files dir
    escaped = server.files_dir.parent / "evil.txt"
    assert not escaped.exists()
    assert (server.files_dir / "evil.txt").exists() if code == 200 else True


def test_static_404(server):
    code, _, body = server.request("/no-such-asset.js")
    assert code == 404
    assert json.loads(body)["error"] == "not found"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_index_redirects_to_login(server):
    req = urllib.request.Request(server.url("/"))
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(req, timeout=10)
        assert False, "expected redirect"
    except urllib.error.HTTPError as e:
        assert e.code in (301, 302, 307)
        assert e.headers["Location"] == "/login"


# ------------------------------------------------------- websocket proxy

WS_KEY = base64.b64encode(b"0123456789abcdef").decode()  # 16 bytes, valid key


def _ws_headers():
    return {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Key": WS_KEY, "Sec-WebSocket-Version": "13"}


def _ws_connect(port, path="/websockify", host="127.0.0.1", headers=None, timeout=10):
    """Minimal RFC6455 client over a raw socket; returns the socket."""
    s = socket.create_connection((host, port), timeout=timeout)
    h = headers or _ws_headers()
    req = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    s.sendall(req.encode())
    resp = b""
    s.settimeout(timeout)
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            break
        resp += chunk
    head, _, rest = resp.partition(b"\r\n\r\n")
    return s, head, rest


def _client_frame(payload, opcode=2):
    b1 = 0x80 | opcode
    m = b"\x11\x22\x33\x44"
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
    ln = len(payload)
    if ln < 126:
        return bytes((b1, 0x80 | ln)) + m + masked
    if ln < 65536:
        return bytes((b1, 0x80 | 126)) + struct.pack(">H", ln) + m + masked
    return bytes((b1, 0x80 | 127)) + struct.pack(">Q", ln) + m + masked


def _read_ws_message(sock, timeout=10):
    """Read one complete server->client frame; returns (opcode, payload)."""
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 2:
        c = sock.recv(2 - len(hdr))
        if not c:
            return None, None
        hdr += c
    b1, b2 = hdr[0], hdr[1]
    op = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ext = b""
        while len(ext) < 2:
            c = sock.recv(2 - len(ext))
            if not c:
                return None, None
            ext += c
        ln = struct.unpack(">H", ext)[0]
    elif ln == 127:
        ext = b""
        while len(ext) < 8:
            c = sock.recv(8 - len(ext))
            if not c:
                return None, None
            ext += c
        ln = struct.unpack(">Q", ext)[0]
    payload = b""
    while len(payload) < ln:
        c = sock.recv(ln - len(payload))
        if not c:
            break
        payload += c
    return op, payload


def test_websocket_upgrade_fails_gracefully_when_vnc_absent(server):
    # VNC points at an empty port for the module server: upgrade gets 502
    # (after authenticating — the websocket is an endpoint like any other, #112)
    _, cookie = _login(server, ip="192.0.2.90")
    s, head, _ = _ws_connect(server.port, headers={**_ws_headers(), "Cookie": cookie})
    try:
        assert b"502" in head.split(b"\r\n")[0]
        assert b"application/json" in head
    finally:
        s.close()


def test_websocket_upgrade_requires_auth(server):
    # #125: no cookie -> 401 before any upstream connection is made
    s, head, _ = _ws_connect(server.port, headers=_ws_headers())
    try:
        assert b"401" in head.split(b"\r\n")[0]
    finally:
        s.close()


def test_websocket_upgrade_requires_upgrade_headers(server):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        req = f"GET /websockify HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(req.encode())
        resp = b""
        s.settimeout(10)
        while b"\r\n\r\n" not in resp:
            c = s.recv(4096)
            if not c:
                break
            resp += c
        assert b"400" in resp.split(b"\r\n")[0]
    finally:
        s.close()


def test_websocket_byte_fidelity(server, vnc_echo_port):
    """Bidirectional byte fidelity through the real proxy (#34)."""
    _proxy_against(server, vnc_echo_port, reconnect=False)


def test_websocket_reconnect_when_vnc_restarts(server, vnc_echo_port):
    """Upstream dies mid-session: the proxy retries with backoff, buffers
    client frames, preserves order, and keeps the client ws open (#39)."""
    _proxy_against(server, vnc_echo_port, reconnect=True)


def _proxy_against(server, vnc_port, reconnect):
    port = _free_port()
    passfile = server.env["RD_PASSFILE"]
    # second server instance pointed at the echo "VNC"
    import tempfile

    tmp_path = tempfile.mkdtemp(prefix="rd-int-echo-")
    os.makedirs(os.path.join(tmp_path, "home"))
    os.makedirs(os.path.join(tmp_path, "downloads"))
    passfile2 = os.path.join(tmp_path, ".passhash")
    open(passfile2, "w").write(TEST_HASH + "\n")
    env = dict(server.env)
    env.update(
        {
            "RD_PORT": str(port),
            "VNC_PORT": str(vnc_port),
            "HOME": os.path.join(tmp_path, "home"),
            "RD_FILES_DIR": os.path.join(tmp_path, "downloads"),
            "RD_LOG_FILE": os.path.join(tmp_path, "server.log"),
            "RD_PASSFILE": passfile2,
            "XDG_DATA_HOME": tmp_path,
        }
    )
    if reconnect:
        env.update({"RD_VNC_RETRIES": "5", "RD_VNC_BACKOFF": "0.05", "RD_WS_IDLE_TIMEOUT": "300"})
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "control.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(os.path.join(tmp_path, "err.log"), "ab"),
    )
    try:
        deadline = time.time() + 15
        up = False
        while time.time() < deadline and not up:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    up = r.status == 200
            except Exception:
                time.sleep(0.15)
        assert up, (
            "echo server did not come up; log:\n"
            + (
                open(os.path.join(tmp_path, "server.log")).read()[-2000:]
                if os.path.exists(os.path.join(tmp_path, "server.log"))
                else "(none)"
            )
            + "\nerr:\n"
            + (
                open(os.path.join(tmp_path, "err.log")).read()[-2000:]
                if os.path.exists(os.path.join(tmp_path, "err.log"))
                else "(none)"
            )
        )

        # the websocket proxy requires an authenticated session (#112/#125)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"username": "admin", "password": TEST_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                cookie = r.headers.get("Set-Cookie", "").split(";", 1)[0]
        except urllib.error.HTTPError as e:
            cookie = e.headers.get("Set-Cookie", "").split(";", 1)[0]
        assert cookie.startswith("rd_session="), "echo-server login failed"

        s, head, _ = _ws_connect(port, headers={**_ws_headers(), "Cookie": cookie})
        try:
            assert b"101" in head.split(b"\r\n")[0], head
            payload = bytes(range(256)) * 4  # 1 KiB binary
            s.sendall(_client_frame(payload))
            op, echoed = _read_ws_message(s)
            assert op == 2
            assert echoed == payload  # client -> proxy -> VNC -> proxy -> client
            # fragmented + control frames still work through the same socket:
            # the proxy answers pings with pongs itself (never forwarded)
            s.sendall(_client_frame(b"ping-me", opcode=9))
            op, pong = _read_ws_message(s)
            assert op == 0xA and pong == b"ping-me"
            if reconnect:
                # the echo VNC closes the upstream after the first echo;
                # the proxy must reconnect transparently and send the
                # buffered client bytes to the NEW upstream, in order
                s.sendall(_client_frame(b"round2-after-reconnect"))
                op, data = _read_ws_message(s, timeout=15)
                assert op == 2, op
                assert data == b"round2-after-reconnect"
        finally:
            s.close()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = "TIMEOUT"
        import shutil

        shutil.rmtree(tmp_path, ignore_errors=True)
        assert rc == 0, f"echo server did not exit cleanly (rc={rc})"


# ------------------------------------------------- graceful shutdown (#25)


def test_sigterm_graceful_shutdown_no_orphans(tmp_path, vnc_fake_port):
    passfile = tmp_path / "sd" / ".passhash"
    os.makedirs(passfile.parent, exist_ok=True)
    passfile.write_text(TEST_HASH + "\n")
    srv = Server(tmp_path, _free_port(), vnc_fake_port, passfile)
    srv.env["RD_LOG_FILE"] = str(tmp_path / "sd-server.log")
    srv.start()
    rc = srv.stop(signal.SIGTERM)
    assert rc == 0, srv.log_tail()
    _assert_no_server_children(srv)
    assert (tmp_path / "sd-server.log").exists()


def test_second_signal_forces_fast_exit(tmp_path, vnc_fake_port):
    passfile = tmp_path / "sd2" / ".passhash"
    os.makedirs(passfile.parent, exist_ok=True)
    passfile.write_text(TEST_HASH + "\n")
    srv = Server(tmp_path, _free_port(), vnc_fake_port, passfile)
    srv.env["RD_LOG_FILE"] = str(tmp_path / "sd2-server.log")
    srv.start()
    srv.proc.send_signal(signal.SIGTERM)
    time.sleep(0.1)
    srv.proc.send_signal(signal.SIGTERM)  # second signal
    rc = srv.proc.wait(timeout=5)
    # forced exit is allowed to be non-zero; what matters is that it exits
    assert rc is not None


# ------------------------------------------------- backup API + readiness


def test_backup_endpoint_trigger_and_list(server):
    _, cookie = _login(server, ip="192.0.2.15")
    code, _, body = server.request("/api/backup", cookie=cookie, method="POST", data={})
    assert code == 200
    r = json.loads(body)
    assert r["ok"] is True and r["path"]
    code, _, body = server.request("/api/backup", cookie=cookie)
    assert code == 200
    backups = json.loads(body)["backups"]
    assert any("v1-" in b["dir"] for b in backups)
    assert backups[0]["manifest"]["state_version"] == 1


def test_backup_requires_auth(server):
    code, _, _ = server.request("/api/backup", method="POST", data={})
    assert code == 401


def test_ready_reports_dependencies(server):
    code, _, body = server.request("/api/ready")
    assert code in (200, 503)
    snap = json.loads(body)
    assert set(snap["dependencies"]) == {"x", "vnc", "pulse", "ffmpeg"}
    assert snap["dependencies"]["vnc"]["ok"] is False  # fake VNC port is empty
    assert "refused" in snap["dependencies"]["vnc"]["reason"] or snap["dependencies"]["vnc"]["reason"]


def test_audio_raw_stream_opens(server):
    # the audio route answers 200 with flowing bytes when PulseAudio +
    # ffmpeg are present; without ffmpeg it must NOT answer 200-with-silence
    # but 503 with an actionable error instead (#100). Raw socket so we can
    # bound the read of an endless stream.
    _, cookie = _login(server, ip="192.0.2.17")
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        req = f"GET /api/audio.raw HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode())
        s.settimeout(8)
        head = b""
        while b"\r\n\r\n" not in head:
            c = s.recv(4096)
            if not c:
                break
            head += c
        status = head.split(b"\r\n")[0]
        if b"503" in status:
            # the body may arrive in a later TCP segment than the header
            # terminator; read to EOF (Connection: close) before asserting
            body = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
            s.settimeout(3)
            try:
                while len(body) < 512:
                    c = s.recv(4096)
                    if not c:
                        break
                    body += c
            except socket.timeout:
                pass
            assert b"audio capture unavailable" in body, body
            return
        assert b"200" in status, status
        total = 0
        try:
            while total < 4096:
                c = s.recv(4096)
                if not c:
                    break
                total += len(c)
        except socket.timeout:
            pass  # stream is alive and flowing; that is all we assert
    finally:
        s.close()


def test_stats_shape(server):
    _, cookie = _login(server, ip="192.0.2.16")
    code, _, body = server.request("/api/stats", cookie=cookie)
    assert code == 200
    st = json.loads(body)
    for key in ("cpu_pct", "mem_total", "conns", "ws"):
        assert key in st
