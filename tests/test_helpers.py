"""Tests for control.py helpers (#11). Imports control.py as a module —
importing has no side effects beyond config parsing; the server only starts
in main().

Since #22 the implementation lives in the remote_desktop package and
`control` is a re-export shim. Tests patch mutable state on the OWNING
submodule (rd_auth/rd_files/rd_clipboard) so the functions see the patched
values; `control.<name>` still works for reads and plain functions."""
import base64
import hashlib
import io
import json
import os
import struct
import time

import pytest

import control
from remote_desktop import auth as rd_auth
from remote_desktop import clipboard as rd_clipboard
from remote_desktop import files as rd_files


# ---------------------------------------------------------------- tokens

@pytest.fixture
def auth(monkeypatch):
    """A configured password so token tests pass even where .passhash is
    absent (CI). Recomputes SECRET the same way main() does."""
    monkeypatch.setattr(rd_auth, "PASS_HASH",
                        "$6$rounds=1000$salt$" +
                        control.hashlib.sha256(b"x").hexdigest())
    monkeypatch.setattr(rd_auth, "SECRET",
                        control.hashlib.sha256(
                            (rd_auth.PASS_HASH + "rd-secret-v2").encode()).digest())


def test_token_roundtrip(auth):
    tok = control._make_token(control.AUTH_USER)
    assert control._verify_token(tok) is True


def test_token_tampered(auth):
    tok = control._make_token(control.AUTH_USER)
    raw = base64.urlsafe_b64decode(tok.encode() + b"==")
    payload, sig = raw.rsplit(b":", 1)
    payload = b"root:99999999999"
    forged = base64.urlsafe_b64encode(payload + b":" + sig).decode()
    assert control._verify_token(forged) is False


def test_token_expired(auth):
    exp = int(time.time()) - 10
    payload = f"{control.AUTH_USER}:{exp}".encode()
    sig = control.hmac.new(control.SECRET, payload, hashlib.sha256).hexdigest()
    tok = base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()
    assert control._verify_token(tok) is False


def test_token_wrong_user(auth):
    exp = int(time.time()) + control.TOKEN_TTL
    payload = f"somebody-else:{exp}".encode()
    sig = control.hmac.new(control.SECRET, payload, hashlib.sha256).hexdigest()
    tok = base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()
    assert control._verify_token(tok) is False


def test_token_garbage():
    assert control._verify_token("") is False
    assert control._verify_token(None) is False
    assert control._verify_token("not-a-token") is False


# ---------------------------------------------------------------- file paths

def test_safe_file_path_accepts_plain_names(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    assert control._safe_file_path("hello.txt") == os.path.join(str(tmp_path), "hello.txt")


def test_safe_file_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    # directory components are stripped (basename sanitisation), never escaped
    for evil in ("../etc/passwd", "a/b", "..", "..\\..\\x"):
        p = control._safe_file_path(evil)
        assert p is None or (os.path.dirname(p) == str(tmp_path) and not p.endswith("etc/passwd"))
    assert control._safe_file_path("") is None
    assert control._safe_file_path(None) is None


def test_safe_file_path_rejects_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    assert control._safe_file_path(".cliphistory.json") is None


# ---------------------------------------------------------------- websocket reader

def ws_stream(frames):
    return io.BytesIO(b"".join(frames))


def client_frame(payload, opcode=2, fin=True, mask=True):
    b1 = (0x80 if fin else 0) | opcode
    m = b"\x11\x22\x33\x44" if mask else b""
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(payload)) if mask else payload
    ln = len(payload)
    if ln < 126:
        return bytes((b1, (0x80 if mask else 0) | ln)) + m + masked
    if ln < 65536:
        return bytes((b1, (0x80 if mask else 0) | 126)) + struct.pack(">H", ln) + m + masked
    return bytes((b1, (0x80 if mask else 0) | 127)) + struct.pack(">Q", ln) + m + masked


def test_ws_single_binary_frame():
    r = control._ws_read_frame(ws_stream([client_frame(b"hello")]))
    assert r == (2, b"hello")


def test_ws_fragmented_message():
    stream = ws_stream([
        client_frame(b"foo", fin=False),
        client_frame(b"bar", opcode=0, fin=False),
        client_frame(b"baz", opcode=0, fin=True),
    ])
    assert control._ws_read_frame(stream) == (2, b"foobarbaz")


def test_ws_interleaved_ping_during_fragments():
    reader = control._WsReader(ws_stream([
        client_frame(b"abc", fin=False),
        client_frame(b"ping", opcode=9),
        client_frame(b"def", opcode=0, fin=True),
    ]))
    assert reader.read() == (9, b"ping")
    assert reader.read() == (2, b"abcdef")


def test_ws_close_returns_close():
    assert control._ws_read_frame(ws_stream([client_frame(b"", opcode=8)]))[0] == 8


def test_ws_rejects_stray_continuation():
    assert control._ws_read_frame(ws_stream([client_frame(b"x", opcode=0)])) is None


def test_ws_rejects_new_data_frame_mid_message():
    stream = ws_stream([client_frame(b"a", fin=False), client_frame(b"b", fin=True)])
    assert control._ws_read_frame(stream) is None


def test_ws_rejects_oversize():
    stream = ws_stream([client_frame(b"x" * 300)])
    assert control._ws_read_frame(stream, max_message=128) is None


def test_ws_rejects_unmasked():
    assert control._ws_read_frame(ws_stream([client_frame(b"x", mask=False)])) is None


def test_ws_truncated():
    assert control._ws_read_frame(io.BytesIO(b"\x82")) is None
    assert control._ws_read_frame(io.BytesIO(b"")) is None


def test_ws_16bit_and_64bit_lengths():
    big = b"y" * 70_000
    op, payload = control._ws_read_frame(ws_stream([client_frame(big)]))
    assert (op, len(payload)) == (2, 70_000)
    op, payload = control._ws_read_frame(ws_stream([client_frame(b"z" * 200)]))
    assert (op, len(payload)) == (2, 200)


# ---------------------------------------------------------------- clipboard persistence

@pytest.fixture
def clip_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_clipboard, "CLIP_HISTORY_FILE", str(tmp_path / "hist.json"))
    rd_clipboard._clip_history = []
    return tmp_path / "hist.json"


def test_clip_roundtrip(clip_file):
    control.clip_add("first", "desktop")
    control.clip_add("second", "phone")
    assert len(rd_clipboard._clip_history) == 2
    rd_clipboard._clip_history = []     # simulate restart
    control._clip_load()
    assert [i["text"] for i in rd_clipboard._clip_history] == ["second", "first"]


def test_clip_corrupt_quarantined(clip_file):
    clip_file.write_text("{not json")
    control._clip_load()
    assert rd_clipboard._clip_history == []
    assert list(clip_file.parent.glob("hist.json.corrupt-*"))


def test_clip_partial_corrupt_entries_skipped(clip_file):
    clip_file.write_text(json.dumps([
        {"id": "a", "text": "ok", "time": 1, "source": "desktop"},
        "not a dict",
        {"id": "b"},                        # no text
        {"id": "a", "text": "dup", "time": 2, "source": "desktop"},  # dup id
    ]))
    control._clip_load()
    assert [i["id"] for i in rd_clipboard._clip_history] == ["a"]
    assert rd_clipboard._clip_history[0]["text"] == "ok"


def test_clip_trim_to_max(clip_file, monkeypatch):
    monkeypatch.setattr(rd_clipboard, "CLIP_HISTORY_MAX", 5)
    for i in range(9):
        control.clip_add(f"item-{i}", "desktop")
    assert len(rd_clipboard._clip_history) == 5
    assert rd_clipboard._clip_history[0]["text"] == "item-8"


# ---------------------------------------------------------------- rate limit

def test_login_rate_limit_blocks_after_max():
    control.LOGIN_ATTEMPTS.clear()
    ip = "203.0.113.9"
    for _ in range(control.LOGIN_MAX_ATTEMPTS):
        assert control._login_allowed(ip) is True
        control.LOGIN_ATTEMPTS[ip].append(time.time())
    assert control._login_allowed(ip) is False


def test_login_rate_limit_window_expiry():
    control.LOGIN_ATTEMPTS.clear()
    ip = "203.0.113.10"
    control.LOGIN_ATTEMPTS[ip] = [time.time() - control.LOGIN_WINDOW - 1] * 5
    assert control._login_allowed(ip) is True   # all attempts expired


def test_login_rate_limit_table_bounded():
    control.LOGIN_ATTEMPTS.clear()
    now = time.time()
    for i in range(control.LOGIN_ATTEMPTS_MAX_IPS + 500):
        control.LOGIN_ATTEMPTS[f"10.0.0.{i % 256}.{i}"] = [now]
    control._login_allowed("new.ip")
    assert len(control.LOGIN_ATTEMPTS) <= control.LOGIN_ATTEMPTS_MAX_IPS


def test_client_ip_direct_peer():
    class H:  # minimal handler stand-in
        client_address = ("198.51.100.7", 4444)
        headers = {"X-Forwarded-For": "1.2.3.4"}
    assert control._client_ip(H()) == "198.51.100.7"   # spoofed header ignored


def test_client_ip_trusted_proxy():
    class H:
        client_address = ("10.8.0.1", 4444)
        headers = {"X-Forwarded-For": "1.2.3.4, 10.8.0.1"}
    old = rd_auth.TRUSTED_PROXIES
    rd_auth.TRUSTED_PROXIES = {"10.8.0.1"}
    try:
        assert control._client_ip(H()) == "1.2.3.4"
    finally:
        rd_auth.TRUSTED_PROXIES = old


# ---------------------------------------------------------------- misc

def test_health_shape():
    h = control.health()
    assert h["ok"] is True
    assert h["version"] == control.VERSION
    assert isinstance(h["uptime_s"], int)
    assert "active_streams" in h


def test_cleanup_partial_uploads(tmp_path, monkeypatch):
    old = tmp_path / ".upload-1-2.part"
    fresh = tmp_path / ".upload-3-4.part"
    visible = tmp_path / "keep.txt"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    visible.write_bytes(b"x")
    old_age = time.time() - 48 * 3600
    os.utime(old, (old_age, old_age))
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    assert control.cleanup_partial_uploads(max_age=24 * 3600) == 1
    assert not old.exists()
    assert fresh.exists()
    assert visible.exists()


def test_list_files_hides_dotfiles(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    (tmp_path / "visible.txt").write_bytes(b"x")
    (tmp_path / ".upload-9-9.part").write_bytes(b"x")
    names = [f["name"] for f in control.list_files()]
    assert names == ["visible.txt"]
