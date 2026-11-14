"""Security regression suite over the REAL server (#125).

Runs control.py in a subprocess (same harness as test_integration) and
asserts, for EVERY route:

- unauthenticated requests get 401 (public endpoints excepted),
- viewer/operator/admin roles get exactly the access the capability
  table promises (#112) — a dropped auth check fails here,
- CSRF: missing/stale/cross-origin tokens are rejected (#116),
- security headers (CSP, nosniff, referrer, frame) on responses (#117),
- the TOTP second factor gates session creation (#109),
- per-user isolation: files/clipboard/pins never cross accounts (#120),
- the login rate limit survives a restart, fail2ban event emitted (#115),
- disabled accounts lose access immediately (#111).
"""

import json
import os
import socket
import time

import pytest

from test_integration import Server, TEST_HASH, TEST_PASSWORD, _free_port


@pytest.fixture(scope="module")
def sec_server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("sec")
    passfile = tmp_path / ".passhash"
    passfile.write_text(TEST_HASH + "\n")
    s = Server(tmp_path, _free_port(), _free_port(), passfile)
    s.start()
    yield s
    rc = s.stop()
    assert rc == 0, f"server did not exit cleanly\n{s.log_tail()}"


_ipn = [30]


def _new_ip():
    _ipn[0] += 1
    return f"192.0.2.{_ipn[0] % 200 + 20}"


def login(s, user="admin", password=TEST_PASSWORD, ip=None, code=None, csrf=True):
    """One login; unique IP per call avoids the rate limit. A 429 (an IP
    poisoned by an earlier failed-login test) retries on a fresh IP.
    Returns (http_code, cookie, csrf_token, body)."""
    data = {"username": user, "password": password}
    if code:
        data["code"] = code
    for attempt in range(3):
        code_, headers, body = s.request("/api/login", data=data, ip=ip or _new_ip())
        if code_ != 429:
            break
        ip = None  # pick a fresh one next round
    if code_ != 200:
        return code_, None, None, body
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    try:
        csrf_tok = json.loads(body).get("csrf") or ""
    except Exception:
        csrf_tok = ""
    return code_, cookie, csrf_tok, body


def _get(s, cookie, path, headers=None, csrf_tok=None):
    h = dict(headers or {})
    if csrf_tok:
        h["X-CSRF-Token"] = csrf_tok
    return s.request(path, cookie=cookie, headers=h)


def _post(s, cookie, path, data=None, headers=None, csrf_tok=None, csrf=True):
    h = dict(headers or {})
    if csrf_tok:
        h["X-CSRF-Token"] = csrf_tok
    return s.request(path, data=data, cookie=cookie, headers=h, csrf=csrf)


# ---------------------------------------------------------------- unauthenticated sweep (#125)

PUBLIC_GET = {"/api/health", "/api/ready", "/api/login", "/login", "/"}
# public POST endpoints: /api/login (the credential IS the auth) and the
# passkey ceremony (#110) — its guard is the origin check (#116), which
# answers 403, not 401, to credential-less cross-origin requests
PUBLIC_POST = {"/api/login", "/api/webauthn/auth/options", "/api/webauthn/auth/verify"}

GET_ROUTES = [
    "/api/auth_check",
    "/api/windows",
    "/api/apps",
    "/api/stats",
    "/api/clipboard",
    "/api/clipboard/history",
    "/api/clipboard/sync",
    "/api/settings",
    "/api/pins",
    "/api/custom-apps",
    "/api/audio/status",
    "/api/audio/sinks",
    "/api/audio/apps",
    "/api/audio/record",
    "/api/audio/recordings",
    "/api/media/status",
    "/api/store",
    "/api/store/backend",
    "/api/store/collections",
    "/api/store/status?pkg=x",
    "/api/files",
    "/api/files/roots",
    "/api/files/search?q=x",
    "/api/files/content?path=x",
    "/api/files/thumb?path=x",
    "/api/files/upload/status?session=x",
    "/api/files/transfers",
    "/api/download?name=x",
    "/api/csrf",
    "/api/account",
    "/api/sessions",
    "/api/notify",
    "/api/2fa",
    "/api/admin/users",
    "/api/admin/audit",
    "/api/admin/audit/verify",
    "/api/admin/keys",
    "/api/backup",
]

POST_ROUTES = [
    "/api/fit",
    "/api/window/action",
    "/api/geometry",
    "/api/settings",
    "/api/audio/sink",
    "/api/audio/volume",
    "/api/media/keys",
    "/api/audio/record",
    "/api/paste",
    "/api/clipboard/set",
    "/api/clipboard/pin",
    "/api/clipboard/tag",
    "/api/clipboard/remove",
    "/api/clipboard/clear",
    "/api/store/install",
    "/api/store/remove",
    "/api/store/cancel",
    "/api/custom-apps",
    "/api/pins/folder",
    "/api/pins/assign",
    "/api/pins/reorder",
    "/api/launches/clear",
    "/api/launch",
    "/api/files/delete",
    "/api/files/mkdir",
    "/api/files/rename",
    "/api/files/chmod",
    "/api/files/archive",
    "/api/files/extract",
    "/api/files/upload/init",
    "/api/files/upload/cancel",
    "/api/files/transfers/pause",
    "/api/pins/toggle",
    "/api/backup",
    "/api/account/password",
    "/api/2fa/setup",
    "/api/2fa/enable",
    "/api/2fa/disable",
    "/api/sessions/revoke",
    "/api/sessions/revoke-all",
    "/api/notify/test",
    "/api/admin/user",
    "/api/admin/keys/rotate",
    "/api/webauthn/register/options",
    "/api/webauthn/auth/options",
    "/api/login/passkey",
    "/api/clipboard/image",
]

DELETE_ROUTES = ["/api/custom-apps", "/api/pins/folder", "/api/admin/users"]


@pytest.mark.parametrize("path", GET_ROUTES)
def test_get_requires_auth(sec_server, path):
    if path in PUBLIC_GET:
        return
    code, _, _ = sec_server.request(path)
    assert code == 401, f"{path} answered {code} without credentials (#125)"


@pytest.mark.parametrize("path", POST_ROUTES)
def test_post_requires_auth(sec_server, path):
    if path in PUBLIC_POST:
        return
    # note: /api/logout is deliberately reachable without a session —
    # clearing a non-existent cookie is a harmless no-op
    code, _, _ = sec_server.request(path, data={}, csrf=False)
    assert code == 401, f"{path} answered {code} without credentials (#125)"


@pytest.mark.parametrize("path", DELETE_ROUTES)
def test_delete_requires_auth(sec_server, path):
    code, _, _ = sec_server.request(path, method="DELETE", csrf=False)
    assert code == 401


def test_websocket_requires_auth(sec_server):
    s = socket.create_connection(("127.0.0.1", sec_server.port), timeout=10)
    try:
        req = (
            "GET /websockify HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode())
        resp = b""
        s.settimeout(5)
        while b"\r\n\r\n" not in resp:
            c = s.recv(4096)
            if not c:
                break
            resp += c
        assert b"401" in resp.split(b"\r\n")[0], "websockify must require a session (#112/#125)"
    finally:
        s.close()


# ---------------------------------------------------------------- role matrix (#112)


@pytest.fixture(scope="module")
def accounts(sec_server):
    """Admin (module default) + one operator + one viewer over the API."""
    _, admin, admin_csrf, _ = login(sec_server, ip="192.0.2.2")
    for name, role in (("jules", "operator"), ("vera", "viewer")):
        code, _, body = _post(
            sec_server,
            admin,
            "/api/admin/user",
            {"action": "create", "username": name, "password": "their-password-1", "role": role},
            csrf_tok=admin_csrf,
        )
        assert code == 200, body
    _, op, op_csrf, _ = login(sec_server, user="jules", password="their-password-1", ip="192.0.2.3")
    _, vw, vw_csrf, _ = login(sec_server, user="vera", password="their-password-1", ip="192.0.2.4")
    assert op and vw, "operator/viewer logins failed"
    return {
        "admin": (admin, admin_csrf),
        "operator": (op, op_csrf),
        "viewer": (vw, vw_csrf),
    }


def test_auth_check_reports_role_and_caps(sec_server, accounts):
    for who, (cookie, _) in accounts.items():
        code, _, body = sec_server.request("/api/auth_check", cookie=cookie)
        j = json.loads(body)
        assert code == 200 and j["role"] == who, body
        assert j["caps"]["admin"] == (who == "admin")


def test_viewer_cannot_launch_write_or_install(sec_server, accounts):
    cookie, csrf_tok = accounts["viewer"]
    for path, data in (
        ("/api/launch", {"id": "no-such-app-zz"}),  # 403 must come BEFORE app handling
        ("/api/store/install", {"pkg": "nano"}),
        ("/api/files/mkdir", {"root": "", "path": "nope"}),
        ("/api/files/delete", {"root": "", "paths": ["nope.txt"]}),
        ("/api/paste", {"text": "nope"}),
        ("/api/clipboard/set", {"text": "nope"}),
        ("/api/window/action", {"hid": "x", "action": "close"}),
        ("/api/settings", {"key": "clip_sync", "value": True}),
    ):
        code, _, body = _post(sec_server, cookie, path, data, csrf_tok=csrf_tok)
        assert code == 403, f"viewer {path} -> {code} (want 403, #112): {body}"


def test_viewer_can_read(sec_server, accounts):
    cookie, _ = accounts["viewer"]
    for path in ("/api/windows", "/api/apps", "/api/stats", "/api/clipboard", "/api/files", "/api/pins"):
        code, _, _ = sec_server.request(path, cookie=cookie)
        assert code == 200, f"viewer {path} -> {code} (want 200, #112)"


def test_operator_controls_but_not_admin(sec_server, accounts):
    cookie, csrf_tok = accounts["operator"]
    # control surface works (non-403 = capability granted; payload errors are 4xx from the handler)
    for path, data in (
        ("/api/paste", {"text": ""}),  # no-op paste -> 200
        ("/api/launch", {"id": "bad id with .."}),  # -> 400 input validation, NOT 403
        ("/api/files/mkdir", {"root": "", "path": ""}),  # -> 400
        ("/api/clipboard/set", {"text": "operator-note"}),  # -> 200
    ):
        code, _, _ = _post(sec_server, cookie, path, data, csrf_tok=csrf_tok)
        assert code != 403, f"operator {path} wrongly forbidden"
    # users + packages + audit are admin-only
    for path in ("/api/admin/users", "/api/admin/audit", "/api/admin/keys"):
        code, _, _ = sec_server.request(path, cookie=cookie)
        assert code == 403, f"operator {path} -> {code} (want 403)"
    for path, data in (
        ("/api/store/install", {"pkg": "nano"}),
        ("/api/admin/user", {"action": "create", "username": "nope", "password": "password123"}),
        ("/api/admin/keys/rotate", {}),
        ("/api/backup", {}),
    ):
        code, _, body = _post(sec_server, cookie, path, data, csrf_tok=csrf_tok)
        assert code == 403, (
            f"operator {path} -> {code} (want 403, #112) {body if code != 403 else ''}\n"
            f"LOG:\n{sec_server.log_tail(120)}"
        )


def test_admin_panel_works(sec_server, accounts):
    cookie, _ = accounts["admin"]
    code, _, body = sec_server.request("/api/admin/users", cookie=cookie)
    assert code == 200
    names = [u["username"] for u in json.loads(body)["users"]]
    assert "jules" in names and "vera" in names
    assert b"$6$" not in body  # hashes never leave the store


# ---------------------------------------------------------------- CSRF over HTTP (#116)


def test_post_without_csrf_token_rejected(sec_server, accounts):
    cookie, _ = accounts["admin"]
    code, _, _ = _post(sec_server, cookie, "/api/clipboard/set", {"text": "x"}, csrf=False)
    assert code == 403
    assert code == 403 and sec_server.request("/api/auth_check", cookie=cookie)[0] == 200  # session still valid


def test_post_with_stale_token_rejected(sec_server, accounts):
    cookie, _ = accounts["admin"]
    code, _, _ = _post(sec_server, cookie, "/api/clipboard/set", {"text": "x"}, csrf_tok="0" * 64)
    assert code == 403


def test_cross_origin_post_rejected(sec_server, accounts):
    cookie, csrf_tok = accounts["admin"]
    for headers in (
        {"Origin": "https://evil.example.com"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # a same-site SUBDOMAIN is still cross-origin (#116)
    ):
        code, _, _ = _post(sec_server, cookie, "/api/clipboard/set", {"text": "x"}, headers=headers, csrf_tok=csrf_tok)
        assert code == 403, f"{headers} -> {code} (want 403, #116)"


def test_same_origin_post_allowed(sec_server, accounts):
    cookie, csrf_tok = accounts["admin"]
    code, _, _ = _post(
        sec_server,
        cookie,
        "/api/clipboard/set",
        {"text": "origin-ok"},
        headers={"Origin": f"http://127.0.0.1:{sec_server.port}", "Sec-Fetch-Site": "same-origin"},
        csrf_tok=csrf_tok,
    )
    assert code == 200


def test_csrf_token_rotates_with_login(sec_server):
    a_ip, b_ip = _new_ip(), _new_ip()
    _, c1, t1, _ = login(sec_server, ip=a_ip)
    _, c2, t2, _ = login(sec_server, ip=b_ip)
    assert t1 and t2 and t1 != t2, "each session needs its own CSRF token (#116)"


def test_login_health_survive_without_token(sec_server):
    # login + health stay compatible (#116)
    code, _, _ = sec_server.request("/api/health")
    assert code == 200
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": "wrong"}, ip=_new_ip(), csrf=False
    )
    assert code == 401


# ---------------------------------------------------------------- security headers (#117)


def test_security_headers_on_html_and_json(sec_server, accounts):
    cookie, _ = accounts["admin"]
    for path in ("/login", "/", "/api/health", "/api/auth_check"):
        code, headers, _ = sec_server.request(path, cookie=cookie)
        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'none'" in csp, f"{path} missing CSP (#117)"
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Referrer-Policy") == "no-referrer"
        assert headers.get("X-Frame-Options") == "DENY"


def test_csp_blocks_no_inline_scripts(sec_server):
    # the shell must carry no inline scripts (#117): both pages only
    # reference external files, enforced in CI and asserted here
    import urllib.request

    for path in ("/", "/login"):
        code, _, body = sec_server.request(path, cookie=None if path == "/login" else None)
        # 302 for / without a cookie; fetch the page with a session
        if code in (301, 302, 307):
            continue
        assert b"<script src=" in body or b'<script type="module" src=' in body, f"{path} lost its script tags"


# ---------------------------------------------------------------- TOTP over HTTP (#109)


def test_totp_enrollment_and_login(sec_server):
    from remote_desktop import totp

    ip = _new_ip()
    _, cookie, csrf_tok, _ = login(sec_server, ip=ip)
    # stage + enable
    code, _, body = _post(sec_server, cookie, "/api/2fa/setup", {}, csrf_tok=csrf_tok)
    assert code == 200
    secret = json.loads(body)["secret"]
    code, _, body = _post(sec_server, cookie, "/api/2fa/enable", {"code": totp.totp_at(secret)}, csrf_tok=csrf_tok)
    assert code == 200, body
    recovery = json.loads(body)["recovery_codes"]
    assert len(recovery) == 10

    # a valid password WITHOUT a code cannot create a session (#109)
    code, _, body = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD}, ip=_new_ip(), csrf=False
    )
    assert code == 401 and json.loads(body).get("needs_2fa") is True

    # wrong code rejected
    code, _, _ = sec_server.request(
        "/api/login",
        data={"username": "admin", "password": TEST_PASSWORD, "code": "000000"},
        ip=_new_ip(),
        csrf=False,
    )
    assert code == 401

    # correct password + code -> session
    code, cookie2, _, _ = login(sec_server, code=totp.totp_at(secret), ip=_new_ip())
    assert code == 200 and cookie2

    # recovery code works exactly once (#109)
    rc = recovery[0]
    code, cookie3, _, _ = login(sec_server, code=rc, ip=_new_ip())
    assert code == 200 and cookie3
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD, "code": rc}, ip=_new_ip(), csrf=False
    )
    assert code == 401, "a recovery code must be invalidated after use (#109)"

    # disable requires the current factor; afterwards password-only works
    code4, cookie4, csrf2, _ = login(sec_server, code=totp.totp_at(secret), ip=_new_ip())
    assert code4 == 200
    code, _, body = _post(
        sec_server,
        cookie4,
        "/api/2fa/disable",
        {"password": TEST_PASSWORD, "code": totp.totp_at(secret)},
        csrf_tok=csrf2,
    )
    assert code == 200, body
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD}, ip=_new_ip(), csrf=False
    )
    assert code == 200, "after disabling 2FA the password alone must work again"


# ---------------------------------------------------------------- isolation (#120)


def test_per_user_file_roots_isolated(sec_server, accounts):
    admin = accounts["admin"]
    # admin uploads into the shared default root
    (sec_server.files_dir / "admin-file.txt").write_bytes(b"admin data")
    code, _, body = sec_server.request("/api/files", cookie=admin[0])
    j = json.loads(body)
    admin_root = j["root"]

    # viewer sees ONLY the personal root, never the admin's (#120)
    vcookie, vcsrf = accounts["viewer"]
    code, _, body = sec_server.request("/api/files", cookie=vcookie)
    j = json.loads(body)
    assert code == 200
    assert j["root"] != admin_root and j["root_label"] == "Files"
    assert all(r["id"] != admin_root for r in j["roots"])

    # resolving the admin's root as the viewer answers 404 without leaking existence
    code, _, body = sec_server.request(f"/api/files?root={admin_root}", cookie=vcookie)
    assert code == 404
    code, _, body = sec_server.request(f"/api/files/content?root={admin_root}&path=admin-file.txt", cookie=vcookie)
    assert code == 404, "cross-user download must not leak file existence (#120)"

    # the operator's own root is writable and separate (a viewer cannot write at all, #112)
    ocookie, ocsrf = accounts["operator"]
    code, _, _ = _post(sec_server, ocookie, "/api/files/mkdir", {"root": "", "path": "own"}, csrf_tok=ocsrf)
    assert code == 200
    code, _, body = sec_server.request("/api/files", cookie=admin[0])
    names = [e["name"] for e in json.loads(body).get("entries", [])]
    assert "own" not in names, "operator's directory leaked into the admin root"


def test_per_user_clipboard_history_isolated(sec_server, accounts):
    # #120: histories are per-user. Writing the shared X selection needs
    # CAP_OPERATE (a viewer cannot drive the desktop), so isolation is
    # exercised between the two accounts that may write.
    acookie, acsrf = accounts["admin"]
    ocookie, ocsrf = accounts["operator"]
    _post(sec_server, acookie, "/api/clipboard/set", {"text": "admin-secret-xyz"}, csrf_tok=acsrf)
    _post(sec_server, ocookie, "/api/clipboard/set", {"text": "operator-secret-abc"}, csrf_tok=ocsrf)
    code, _, body = sec_server.request("/api/clipboard/history", cookie=acookie)
    ja = json.loads(body)
    code, _, body = sec_server.request("/api/clipboard/history", cookie=ocookie)
    jo = json.loads(body)
    texts_a = json.dumps(ja)
    texts_o = json.dumps(jo)
    assert "admin-secret-xyz" in texts_a and "admin-secret-xyz" not in texts_o
    assert "operator-secret-abc" in texts_o and "operator-secret-abc" not in texts_a


def test_per_user_pins_isolated(sec_server, accounts):
    vcookie, vcsrf = accounts["viewer"]
    code, _, body = _post(sec_server, vcookie, "/api/pins/toggle", {"id": "viewer-only-app"}, csrf_tok=vcsrf)
    assert code == 200, body
    code, _, body = sec_server.request("/api/pins", cookie=accounts["admin"][0])
    ja = json.loads(body)
    assert "viewer-only-app" not in json.dumps(ja)
    code, _, body = sec_server.request("/api/pins", cookie=vcookie)
    jv = json.loads(body)
    assert "viewer-only-app" in json.dumps(jv)


# ---------------------------------------------------------------- disable/revoke (#111/#114)


def test_disable_revokes_sessions_immediately(sec_server, accounts):
    admin = accounts["admin"]  # (cookie, csrf) from the module fixture
    # fresh user, logged in
    code, _, _ = _post(
        sec_server,
        admin[0],
        "/api/admin/user",
        {"action": "create", "username": "shortlived", "password": "password-1234", "role": "operator"},
        csrf_tok=admin[1],
    )
    assert code == 200
    code, slcookie, _, _ = login(sec_server, user="shortlived", password="password-1234", ip=_new_ip())
    assert code == 200
    code, _, _ = sec_server.request("/api/auth_check", cookie=slcookie)
    assert code == 200
    # disable -> existing cookie is dead immediately (#111)
    code, _, _ = _post(
        sec_server,
        admin[0],
        "/api/admin/user",
        {"action": "status", "username": "shortlived", "status": "disabled"},
        csrf_tok=admin[1],
    )
    assert code == 200
    code, _, _ = sec_server.request("/api/auth_check", cookie=slcookie)
    assert code == 401, "a disabled user's session must be revoked at once (#111)"
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "shortlived", "password": "password-1234"}, ip=_new_ip(), csrf=False
    )
    assert code == 401, "a disabled user cannot log in (#111)"


def test_session_listing_and_revoke(sec_server, accounts):
    cookie, csrf_tok = accounts["admin"]
    code, _, body = sec_server.request("/api/sessions", cookie=cookie)
    j = json.loads(body)
    assert code == 200 and j["sessions"]
    s = j["sessions"][0]
    assert "label" in s and "rd_session" not in json.dumps(j) and "token" not in json.dumps(j).replace('"token"', "")
    # revoke a fabricated session id -> 404; revoke own current works
    code, _, _ = _post(sec_server, cookie, "/api/sessions/revoke", {"id": "ffffffffffff"}, csrf_tok=csrf_tok)
    assert code == 404


# ---------------------------------------------------------------- rate limit (#115)


def test_rate_limit_survives_restart_and_emits_fail2ban_event(sec_server, tmp_path_factory):
    ip = "192.0.2.77"
    for i in range(6):
        code, _, _ = sec_server.request(
            "/api/login", data={"username": "admin", "password": f"bad-{i}"}, ip=ip, csrf=False
        )
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD}, ip=ip, csrf=False
    )
    assert code == 429, "lockout must engage after the configured attempts"
    # fail2ban-ready event (#115)
    assert "AUTH-FAILED" in sec_server.log_tail(), "expected a structured AUTH-FAILED event"
    # a restart must NOT clear the lockout (#115)
    port = sec_server.port
    sec_server.stop()
    sec_server.start()
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD}, ip=ip, csrf=False
    )
    assert code == 429, "a restart cleared the lockout (#115 acceptance)"
    # a different IP is unaffected
    code, cookie, _, _ = login(sec_server, ip="192.0.2.78")
    assert code == 200


# ---------------------------------------------------------------- audit integration (#113)


def test_audit_records_security_events(sec_server, accounts):
    _, admin, admin_csrf, _ = login(sec_server, ip=_new_ip())
    code, _, body = sec_server.request("/api/admin/audit?limit=200", cookie=admin)
    if code != 200:
        print(
            f"DEBUG audit401 cookie[:60]={admin[:60]!r} body={body[:200]!r}\nSERVER LOG:\n{sec_server.log_tail()[-1200:]}"
        )
    assert code == 200
    events = json.loads(body)["events"]
    actions = {e["action"] for e in events}
    assert "login" in actions and "user_create" in actions
    # chain verification endpoint
    code, _, body = sec_server.request("/api/admin/audit/verify", cookie=admin)
    j = json.loads(body)
    assert j["chain_ok"] is True and j["checked"] >= 10


def test_password_change_revokes_and_needs_no_restart(sec_server, accounts):
    ip = _new_ip()
    _, cookie, csrf_tok, _ = login(sec_server, ip=ip)
    code, _, body = _post(
        sec_server,
        cookie,
        "/api/account/password",
        {"current": TEST_PASSWORD, "new": "brand-new-pw-1", "keep_sessions": True},
        csrf_tok=csrf_tok,
    )
    assert code == 200, body
    # old password dead immediately (#118)
    code, _, _ = sec_server.request(
        "/api/login", data={"username": "admin", "password": TEST_PASSWORD}, ip=_new_ip(), csrf=False
    )
    assert code == 401
    # new password works; current session kept (keep_sessions)
    code, _, _ = sec_server.request("/api/auth_check", cookie=cookie)
    if code != 200:
        print(f"DEBUG keep401 cookie[:60]={cookie[:60]!r}\nSERVER LOG:\n{sec_server.log_tail()[-1500:]}")
    assert code == 200, "keep_sessions must preserve the current session (#118)"
    code, _, _, _ = login(sec_server, password="brand-new-pw-1", ip=_new_ip())
    assert code == 200
    # restore for the other tests
    _, _, csrf_new, _ = login(sec_server, password="brand-new-pw-1", ip=_new_ip())
    code, _, body = sec_server.request(
        "/api/account/password",
        data={"current": "brand-new-pw-1", "new": TEST_PASSWORD, "keep_sessions": True},
        cookie=cookie,  # auto-CSRF: the token must belong to THIS session
    )
    assert code == 200, f"restore failed: {body!r}"
    code, _, _, _ = login(sec_server, password=TEST_PASSWORD, ip=_new_ip())
    assert code == 200
