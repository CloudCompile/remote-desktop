"""CSRF tokens + Origin checks (#116) and the session registry (#114)."""

import base64
import json
import time

import pytest

from remote_desktop import auth, config


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(auth, "SECRET", b"k" * 32)
    monkeypatch.setattr(config, "CSRF_ENABLED", True)


class H:
    def __init__(self, headers=None, sid="sess-123"):
        self.headers = headers or {}
        self.sid = sid
        self.client_address = ("198.51.100.1", 1)
        self.user = "admin"


# ---------------------------------------------------------------- CSRF (#116)


def test_csrf_token_binds_to_session(key):
    t1 = auth.csrf_token("sess-123")
    t2 = auth.csrf_token("sess-other")
    assert t1 != t2 and len(t1) == 64


def test_csrf_accepts_matching_token(key):
    h = H(headers={"X-CSRF-Token": auth.csrf_token("sess-123")})
    assert auth._csrf_ok(h) is True


def test_csrf_rejects_missing_stale_foreign(key):
    assert auth._csrf_ok(H()) is False  # missing
    h = H(headers={"X-CSRF-Token": auth.csrf_token("sess-123") + "0"})  # stale/tampered
    assert auth._csrf_ok(h) is False
    h = H(sid="other", headers={"X-CSRF-Token": auth.csrf_token("sess-123")})  # another session's
    assert auth._csrf_ok(h) is False
    assert auth._csrf_ok(H(sid="")) is False  # untracked/legacy token


def test_csrf_rejects_cross_site_fetch_metadata(key):
    bad = H(headers={"X-CSRF-Token": auth.csrf_token("sess-123"), "Sec-Fetch-Site": "cross-site"})
    assert auth._csrf_ok(bad) is False
    # a same-site SUBDOMAIN is still cross-origin (#116: strict by design)
    same_site = H(headers={"X-CSRF-Token": auth.csrf_token("sess-123"), "Sec-Fetch-Site": "same-site"})
    assert auth._csrf_ok(same_site) is False
    ok = H(headers={"X-CSRF-Token": auth.csrf_token("sess-123"), "Sec-Fetch-Site": "same-origin"})
    assert auth._csrf_ok(ok) is True


def test_origin_validation(key, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", set())
    good = H(
        headers={
            "X-CSRF-Token": auth.csrf_token("sess-123"),
            "Origin": "https://rd.example.com",
            "Host": "rd.example.com",
        }
    )
    assert auth._csrf_ok(good) is True
    cross = H(
        headers={
            "X-CSRF-Token": auth.csrf_token("sess-123"),
            "Origin": "https://evil.example.com",
            "Host": "rd.example.com",
        }
    )
    assert auth._csrf_ok(cross) is False
    # explicitly allow-listed origins pass (#116)
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", {"evil.example.com"})
    assert auth._csrf_ok(cross) is True


def test_csrf_can_be_disabled(key, monkeypatch):
    monkeypatch.setattr(config, "CSRF_ENABLED", False)
    assert auth._csrf_ok(H()) is True


# ---------------------------------------------------------------- sessions (#114)


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth, "SESSION_IDLE", 3600)
    auth._sessions = None  # fresh registry per test (None = reload on next use)
    yield
    auth._sessions = None  # drop the in-process cache; the file stays in tmp_path


class FakeReq:
    def __init__(self, ua="Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/128.0"):
        self.headers = {"User-Agent": ua} if ua else {}
        self.client_address = ("203.0.113.9", 5)


def test_session_roundtrip_and_public_shape(reg, key):
    sid, tok = auth.session_create("admin", FakeReq())
    assert sid
    # the token is opaque base64; the user is INSIDE it (signed), never
    # in the raw token string (#114 acceptance)
    assert auth.user_of(tok) == "admin"
    assert "admin" not in tok and tok not in json.dumps(auth.sessions_public(user="admin"))
    assert auth._session_live(sid, "admin") is True
    pub = auth.sessions_public(user="admin")
    assert len(pub) == 1
    s = pub[0]
    assert s["id"] == sid[:12] and s["label"] == "Firefox · Linux"
    # the raw token and any key material never appear (#114 acceptance)
    assert tok not in str(pub) and b"k" * 32 not in str(pub).encode()
    # a foreign user sees nothing of it (#120)
    assert auth.sessions_public(user="jules") == []


def test_session_revoke_immediate(reg, key):
    sid, tok = auth.session_create("admin", FakeReq())
    assert auth._session_live(sid, "admin")
    assert auth.session_revoke(sid[:12], user="admin") == 1
    assert auth._session_live(sid, "admin") is False  # the cookie dies instantly
    # sessions survive a process restart (persisted registry): simulate a
    # fresh process by clearing the in-process cache to a real dict
    sid2, _ = auth.session_create("jules", FakeReq(ua="curl/8.0"))
    auth._sessions = None  # None = "not loaded": next check reloads from disk
    assert auth._session_live(sid2, "jules") is True
    assert auth._session_live(sid, "admin") is False


def test_idle_ttl_expiry(reg, key):
    sid, _ = auth.session_create("admin", FakeReq())
    # age out the session
    ss = auth._sessions_load()
    ss[sid]["last_seen"] = int(time.time()) - 7200
    auth._sessions_save()
    assert auth._session_live(sid, "admin") is False


def test_revoke_all_for_user(reg, key):
    s1, _ = auth.session_create("jules", FakeReq())
    s2, _ = auth.session_create("jules", FakeReq(ua="curl/8.0"))
    s3, _ = auth.session_create("admin", FakeReq())
    n = auth.session_revoke_user("jules", except_prefix=s1[:12])
    assert n == 1  # s2 revoked, s1 kept
    assert auth._session_live(s1, "jules") and not auth._session_live(s2, "jules")
    n = auth.session_revoke_user("jules")
    assert n == 1 and not auth._session_live(s1, "jules")
    assert auth._session_live(s3, "admin")  # other users unaffected
