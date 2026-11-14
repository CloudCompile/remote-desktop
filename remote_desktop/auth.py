"""Authentication (v2.6.0): multi-user tokens, session registry, CSRF,
persistent login rate limiting and signing-key rotation.

Ownership: token mint/verify (HMAC-SHA256, versioned keys with a grace
period #118), the session registry with device labels and revoke (#114),
CSRF token derivation + Origin/Sec-Fetch-Site checks (#116), the
persistent sliding-window login rate limiter with a fail2ban-ready event
(#115), cookie construction with Secure/``__Host-`` support (#119) and
client-IP extraction (trusted-proxy aware). Session state
(PASS_HASH/SECRET/LOGIN_ATTEMPTS) lives here — tests patch this module's
globals. Secrets are never logged (#24 privacy).

Users, roles and password hashes live in the user store
([users.py](users.py)); TOTP secrets are verified from there via
[totp.py](totp.py). Single-user deployments keep working unchanged: a
missing user store means "one legacy admin" (#111).
"""

import base64
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import threading
import time

from . import config, logging as log
from . import users

AUTH_USER = config.AUTH_USER
PASS_FILE = config.PASS_FILE

try:
    PASS_HASH = open(PASS_FILE).read().strip()
except Exception:
    PASS_HASH = ""  # logins disabled if the hash is missing and no store exists

TOKEN_TTL = 7 * 24 * 3600  # 7 days
SESSION_IDLE = config.SESSION_IDLE  # idle TTL (#114)
SESSIONS_MAX = 512  # registry cap (oldest last-seen are evicted)
LOGIN_ATTEMPTS = {}  # ip -> [timestamps]  (in-memory half of the rate limit)
LOGIN_ATTEMPTS_MAX_IPS = 10_000  # hard bound so a spoof flood cannot grow it forever
LOGIN_WINDOW = config.LOGIN_WINDOW  # 10 minutes
LOGIN_MAX_ATTEMPTS = config.LOGIN_MAX_ATTEMPTS  # 5
TRUSTED_PROXIES = config.TRUSTED_PROXIES

# legacy signing secret, derived from the password hash (v2.0.1 behaviour;
# tests patch this global directly). Rotation (#118) replaces it with a
# random key and keeps the previous one in the key file for a grace window.
SECRET = hashlib.sha256((PASS_HASH + "rd-secret-v2").encode()).digest()

# ------------------------------------------------------------------ keys (#118)

KEYS_FILE = None  # test override; default: STATE_DIR/keys.json
KEY_GRACE = config.KEY_GRACE  # seconds an old signing key stays valid

_keys_lock = threading.Lock()
_keys_extra = None  # [{"v", "secret", "expires"}] rotated keys; lazy-loaded


def _keys_path():
    return KEYS_FILE or os.path.join(config.STATE_DIR, "keys.json")


def _load_keys():
    global _keys_extra
    if _keys_extra is not None:
        return _keys_extra
    try:
        with open(_keys_path()) as f:
            raw = json.load(f)
        extra = [
            {"v": int(k["v"]), "secret": base64.b64decode(k["secret"]), "expires": int(k.get("expires") or 0)}
            for k in raw.get("keys", [])
            if int(k.get("v", 0)) >= 2
        ]
    except Exception:
        extra = []
    _keys_extra = [k for k in extra if k["secret"]]
    return _keys_extra


def _save_keys():
    try:
        path = _keys_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {
                    "version": max((k["v"] for k in _load_keys()), default=1),
                    "keys": [
                        {"v": k["v"], "secret": base64.b64encode(k["secret"]).decode(), "expires": k["expires"]}
                        for k in _load_keys()
                    ],
                },
                f,
            )
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key_list():
    """All live signing keys: the current SECRET first, then rotated keys
    still inside their grace window (#118)."""
    now = int(time.time())
    live = [k for k in _load_keys() if not k["expires"] or k["expires"] > now]
    if len(live) != len(_load_keys()):
        with _keys_lock:
            _keys_extra[:] = live
        _save_keys()
    return [{"v": 1, "secret": SECRET, "expires": 0}] + live


def rotate_key(reason="manual", grace=KEY_GRACE, revoke_all=True):
    """Replace the signing key; the old key stays valid for `grace`
    seconds so unrelated sessions survive (#118). Returns the new version."""
    global SECRET, _keys_extra
    with _keys_lock:
        extra = _load_keys()
        version = max((k["v"] for k in extra), default=1) + 1
        extra.append({"v": version, "secret": SECRET, "expires": int(time.time()) + max(0, int(grace))})
        _keys_extra = extra
        _save_keys()
    SECRET = hashlib.sha256(secrets.token_bytes(32)).digest()
    log.log(f"auth: signing key rotated to v{version} ({reason})")
    if revoke_all:
        n = session_revoke_all_global()
        if n:
            log.log(f"auth: {n} session(s) revoked by key rotation")
    return version


def reload():
    """Re-read the password file (#118, SIGHUP): when the legacy hash
    changed and the key was never rotated, old tokens die immediately
    (the derived secret changes). After any rotation the signing key is
    independent of the password, so only the hash is refreshed."""
    global PASS_HASH, SECRET
    try:
        new_hash = open(PASS_FILE).read().strip()
    except Exception:
        new_hash = ""
    if new_hash and new_hash != PASS_HASH:
        PASS_HASH = new_hash
        if not _load_keys():  # pristine legacy deployment
            SECRET = hashlib.sha256((PASS_HASH + "rd-secret-v2").encode()).digest()
        log.log("auth: password file changed; credential reload applied")
        return True
    users.invalidate()
    return False


# ------------------------------------------------------------------ tokens


def _make_token(user, sid=None, ttl=TOKEN_TTL):
    """Signed cookie token: b64("user:exp[:sid]:sig"). Versioned keys are
    tried at verify time (#118)."""
    exp = int(time.time()) + int(ttl)
    payload = f"{user}:{exp}".encode() if not sid else f"{user}:{exp}:{sid}".encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()


def _verify_token(tok):
    """True when: signed by a live key (#118), unexpired, the user is
    active, and — for session tokens — the session is live and inside its
    idle TTL (#114). Legacy tokens (no sid) only ever authenticate the
    legacy admin."""
    if not tok or not _auth_possible():
        return False
    try:
        dec = base64.urlsafe_b64decode(tok.encode() + b"==")
        payload, sig = dec.rsplit(b":", 1)
        parts = payload.decode().rsplit(":", 2)
        if len(parts) == 3:
            user, exp, sid = parts
        else:
            user, exp, sid = parts[0], parts[1], ""
        if user not in ("", "-") and int(exp) < int(time.time()):
            return False
        sig_ok = False
        for k in _key_list():
            if hmac.compare_digest(sig.decode(), hmac.new(k["secret"], payload, hashlib.sha256).hexdigest()):
                sig_ok = True
                break
        if not sig_ok:
            return False
        if not sid:  # legacy token: default admin only, not session-tracked
            return user == AUTH_USER and _legacy_admin_possible()
        ok = _session_live(sid, user)
        return ok
    except Exception:
        return False


def _legacy_admin_possible():
    return bool(PASS_HASH) or users.is_default_admin_configured()


def _auth_possible():
    return bool(PASS_HASH) or users.store_exists()


# ------------------------------------------------------------------ sessions (#114)

SESSIONS_FILE = None  # test override; default: STATE_DIR/sessions.json

_sessions = None
_sessions_lock = threading.Lock()


def _sessions_path():
    return SESSIONS_FILE or os.path.join(config.STATE_DIR, "sessions.json")


def _sessions_load():
    """The session registry (mtime-cached; a missing/unreadable file
    degrades to "no sessions", never to "every token is live")."""
    global _sessions
    if _sessions is not None:
        return _sessions
    ss = {}
    try:
        with open(_sessions_path()) as f:
            raw = json.load(f)
        ss = {
            sid: rec
            for sid, rec in raw.get("sessions", {}).items()
            if isinstance(rec, dict) and isinstance(rec.get("user"), str) and sid
        }
    except Exception:
        ss = {}
    _sessions = ss
    return _sessions


def _sessions_save():
    try:
        ss = _sessions_load()
        now = time.time()
        # prune dead sessions; cap the registry (oldest last-seen first)
        dead = [sid for sid, r in ss.items() if int(r.get("last_seen") or r.get("created") or 0) + SESSION_IDLE < now]
        for sid in dead:
            del ss[sid]
        if len(ss) > SESSIONS_MAX:
            for sid in sorted(ss, key=lambda k: ss[k].get("last_seen") or 0)[: len(ss) - SESSIONS_MAX]:
                del ss[sid]
        path = _sessions_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "sessions": ss}, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass


def device_label(ua):
    """A useful human label from the User-Agent (#114): 'Firefox · Android'."""
    ua = str(ua or "")[:200]
    browser = "Browser"
    for probe, name in (
        ("Edg/", "Edge"),
        ("OPR/", "Opera"),
        ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"),
        ("Safari/", "Safari"),
        ("curl/", "curl"),
    ):
        if probe in ua:
            browser = name
            break
    os_name = ""
    for probe, name in (
        ("Android", "Android"),
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Mac OS X", "macOS"),
        ("Windows", "Windows"),
        ("Linux", "Linux"),
        ("CrOS", "ChromeOS"),
    ):
        if probe in ua:
            os_name = name
            break
    label = " · ".join(p for p in (browser, os_name) if p)
    return label[:60] or "unknown device"


def session_create(user, handler=None, label=None, ttl=TOKEN_TTL):
    """Register a new session and return (sid, signed token)."""
    sid = secrets.token_hex(16)
    ua = (handler.headers.get("User-Agent", "") if handler else "")[:200]
    rec = {
        "user": user,
        "created": int(time.time()),
        "last_seen": int(time.time()),
        "seen_at": time.time(),
        "ip": _client_ip(handler) if handler else "-",
        "ua": ua,
        "label": label or device_label(ua),
    }
    with _sessions_lock:
        ss = _sessions_load()
        mine = [k for k, v in ss.items() if v.get("user") == user]
        if len(mine) >= 32:  # per-user cap: drop the oldest
            for k in sorted(mine, key=lambda k: ss[k].get("last_seen") or 0)[: len(mine) - 31]:
                del ss[k]
        ss[sid] = rec
        _sessions_save()
    return sid, _make_token(user, sid=sid, ttl=ttl)


def _session_live(sid, user):
    """Exists, belongs to `user`, and is inside the idle TTL; touches
    last_seen at most once a minute (#114)."""
    ss = _sessions_load()
    rec = ss.get(sid)
    if not rec or rec.get("user") != user:
        return False
    now = time.time()
    if int(rec.get("last_seen") or rec.get("created") or 0) + SESSION_IDLE < now:
        with _sessions_lock:
            ss.pop(sid, None)
            _sessions_save()
        return False
    if now - float(rec.get("seen_at") or 0) > 60:
        rec["last_seen"] = int(now)
        rec["seen_at"] = now
        _sessions_save()
    return True


def sessions_public(user=None, current_sid=None):
    """List sessions (never the raw token or any key material, #114).
    `user=None` lists all (admin view)."""
    ss = _sessions_load()
    now = time.time()
    out = []
    for sid, rec in sorted(ss.items(), key=lambda kv: -(kv[1].get("last_seen") or 0)):
        if user is not None and rec.get("user") != user:
            continue
        if int(rec.get("last_seen") or 0) + SESSION_IDLE < now:
            continue
        out.append(
            {
                "id": sid[:12],
                "user": rec.get("user"),
                "label": rec.get("label") or device_label(rec.get("ua")),
                "ip": rec.get("ip"),
                "created": rec.get("created"),
                "last_seen": rec.get("last_seen"),
                "current": bool(current_sid and sid.startswith(current_sid)),
            }
        )
    return out


def session_revoke(sid_prefix, user=None):
    """Revoke one session by id (prefix match on the 12-char public id).
    Returns (count, user) — count is 0/1."""
    n = 0
    with _sessions_lock:
        ss = _sessions_load()
        for sid in list(ss):
            if not sid.startswith(sid_prefix):
                continue
            if user is not None and ss[sid].get("user") != user:
                continue
            del ss[sid]
            n += 1
        if n:
            _sessions_save()
    return n


def session_revoke_user(user, except_prefix=None):
    """Revoke every session of one user (#111 disable, #118 password)."""
    n = 0
    with _sessions_lock:
        ss = _sessions_load()
        for sid in list(ss):
            if ss[sid].get("user") != user:
                continue
            if except_prefix and sid.startswith(except_prefix):
                continue
            del ss[sid]
            n += 1
        if n:
            _sessions_save()
    return n


def session_revoke_all_global():
    n = 0
    with _sessions_lock:
        ss = _sessions_load()
        n = len(ss)
        ss.clear()
        _sessions_save()
    return n


def sid_of(token):
    """The sid embedded in a token ("" for legacy tokens)."""
    try:
        dec = base64.urlsafe_b64decode(token.encode() + b"==")
        payload, _sig = dec.rsplit(b":", 1)
        parts = payload.decode().rsplit(":", 2)
        return parts[2] if len(parts) == 3 else ""
    except Exception:
        return ""


def user_of(token):
    """The username embedded in a token ("" when unparsable)."""
    try:
        dec = base64.urlsafe_b64decode(token.encode() + b"==")
        payload, _sig = dec.rsplit(b":", 1)
        return payload.decode().rsplit(":", 2)[0]
    except Exception:
        return ""


# ------------------------------------------------------------------ CSRF (#116)


def csrf_token(sid):
    """Session-bound CSRF token: HMAC(current key, "csrf:"+sid). Rotates
    with the session and with key rotation (#116)."""
    if not sid:
        return ""
    return hmac.new(SECRET, ("csrf:" + sid).encode(), hashlib.sha256).hexdigest()


def _csrf_ok(handler):
    """State-changing requests must carry the session's CSRF token and a
    same-origin Origin/Sec-Fetch-Site when present (#116)."""
    if not config.CSRF_ENABLED:
        return True
    sid = getattr(handler, "sid", "") or ""
    if not sid:
        return False  # untracked (legacy) tokens never mutate state
    sent = handler.headers.get("X-CSRF-Token", "") or handler.headers.get("X-Csrf-Token", "")
    if not sent or not hmac.compare_digest(sent, csrf_token(sid)):
        return False
    return origin_ok(handler)


def origin_ok(handler):
    """Strict same-origin check (#116): Sec-Fetch-Site must be
    same-origin/none and Origin (when present) must match Host or an
    explicitly allowed origin. Requests without these headers (curl,
    tests) pass — the CSRF token remains the primary gate."""
    sfs = handler.headers.get("Sec-Fetch-Site", "")
    if sfs and sfs not in ("same-origin", "none"):
        return False
    origin = handler.headers.get("Origin", "")
    if origin and origin != "null":
        from urllib.parse import urlparse

        netloc = urlparse(origin).netloc
        if not netloc:
            return False
        if netloc == handler.headers.get("Host", ""):
            return True
        if netloc in config.ALLOWED_ORIGINS:
            return True
        return False
    return True


# ------------------------------------------------------------------ cookies (#119)


def cookie_name():
    name = config.COOKIE_NAME or "rd_session"
    if config.COOKIE_HOST_PREFIX:
        return "__Host-" + name  # __Host- rules: Path=/, no Domain, Secure
    return name


def is_secure_request(handler):
    """Secure-flag decision (#119): explicit config wins; otherwise the
    request is secure when the socket is TLS or a trusted proxy saw HTTPS."""
    if config.COOKIE_SECURE == "always":
        return True
    if config.COOKIE_SECURE == "never":
        return False
    try:
        import ssl

        if isinstance(getattr(handler, "connection", None), ssl.SSLSocket):
            return True
    except Exception:
        pass
    if handler is not None and handler.client_address:
        peer = handler.client_address[0]
        if peer in TRUSTED_PROXIES and handler.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
    return False


def cookie_header(handler, token, max_age=TOKEN_TTL):
    """Set-Cookie value for the session token (#119)."""
    parts = [f"{cookie_name()}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age and max_age > 0:
        parts.append(f"Max-Age={int(max_age)}")
    else:
        parts.append("Max-Age=0")
    if is_secure_request(handler) or config.COOKIE_HOST_PREFIX:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header(handler=None):
    return cookie_header(handler, "", max_age=0)


def csrf_cookie_header(token):
    """Mirror cookie readable by the web client (double-submit aid, #116)."""
    parts = [f"rd_csrf={token}", "Path=/", "SameSite=Lax"]
    return "; ".join(parts)


# ------------------------------------------------------------------ passwords


def _verify_password(pw):
    """Legacy single-admin password check (kept for the shim/tests)."""
    try:
        import crypt

        return bool(PASS_HASH and crypt) and crypt.crypt(pw, PASS_HASH) == PASS_HASH
    except Exception:
        return False


def verify_user_password(user, pw):
    """Multi-user password check (#111): the store first, the legacy
    .passhash for the default admin."""
    if not user or not isinstance(pw, str) or not pw:
        return False
    if users.store_exists():
        return users.verify_password(user, pw)
    return user == AUTH_USER and _verify_password(pw)


# ------------------------------------------------------------------ rate limiting (#115)

RL_FILE = None  # test override; default: STATE_DIR/ratelimit.json
RL_MAX_IPS = 5000  # bound on tracked identities
_rl_seeded = set()


def _rl_path():
    return RL_FILE or os.path.join(config.STATE_DIR, "ratelimit.json")


def rate_load():
    """Load + prune the persistent rate-limit store (#115): a restart
    must NOT clear an active lockout."""
    global LOGIN_ATTEMPTS
    try:
        with open(_rl_path()) as f:
            raw = json.load(f)
    except Exception:
        return 0
    now = time.time()
    ips = raw.get("ips", {}) if isinstance(raw, dict) else {}
    loaded = 0
    for ip, ts in list(ips.items())[:RL_MAX_IPS]:
        recent = [
            t for t in (ts if isinstance(ts, list) else []) if isinstance(t, (int, float)) and now - t < LOGIN_WINDOW
        ]
        if recent:
            LOGIN_ATTEMPTS[str(ip)[:64]] = recent
            loaded += len(recent)
    return loaded


def rate_save():
    """Persist + prune. Bounded: expired entries dropped, identities capped."""
    try:
        now = time.time()
        ips = {}
        for ip, ts in LOGIN_ATTEMPTS.items():
            recent = [t for t in ts if now - t < LOGIN_WINDOW]
            if recent:
                ips[ip] = recent
        path = _rl_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "ips": dict(list(ips.items())[:RL_MAX_IPS])}, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _login_allowed(ip):
    """Sliding-window rate limit, persisted (#115). Expired entries are
    pruned on access; the table is capped so a flood of spoofed IPs
    cannot grow memory forever (#16)."""
    now = time.time()
    if len(LOGIN_ATTEMPTS) > LOGIN_ATTEMPTS_MAX_IPS:
        for k in sorted(LOGIN_ATTEMPTS, key=lambda k: max(LOGIN_ATTEMPTS[k], default=0))[: LOGIN_ATTEMPTS_MAX_IPS // 2]:
            del LOGIN_ATTEMPTS[k]
    if ip not in _rl_seeded:  # a restart must not clear a lockout (#115)
        _rl_seeded.add(ip)
        try:
            with open(_rl_path()) as f:
                disk = json.load(f).get("ips", {}).get(ip, [])
            merged = sorted(set(LOGIN_ATTEMPTS.get(ip, [])) | {t for t in disk if now - t < LOGIN_WINDOW})
            if merged:
                LOGIN_ATTEMPTS[ip] = merged
        except Exception:
            pass
    attempts = LOGIN_ATTEMPTS.get(ip)
    if attempts is None:
        attempts = LOGIN_ATTEMPTS[ip] = []
    attempts[:] = [t for t in attempts if now - t < LOGIN_WINDOW]
    return len(attempts) < LOGIN_MAX_ATTEMPTS


def record_login_fail(ip, user=None):
    """Record a failed attempt (memory + persistent store) and emit the
    fail2ban-ready event (#115)."""
    now = time.time()
    LOGIN_ATTEMPTS.setdefault(ip, []).append(now)
    LOGIN_ATTEMPTS[ip][:] = [t for t in LOGIN_ATTEMPTS[ip] if now - t < LOGIN_WINDOW]
    rate_save()
    log.log(f"AUTH-FAILED ip={ip} user={_safe_user(user)}")  # matched by deploy/fail2ban


def login_reset(ip=None):
    """Clear one identity's counters (admin unban / test helper)."""
    if ip:
        LOGIN_ATTEMPTS.pop(ip, None)
        try:
            with open(_rl_path()) as f:
                raw = json.load(f)
            raw.get("ips", {}).pop(ip, None)
            with open(_rl_path(), "w") as f:
                json.dump(raw, f)
        except Exception:
            pass
    else:
        LOGIN_ATTEMPTS.clear()
        try:
            os.remove(_rl_path())
        except OSError:
            pass


def _safe_user(user):
    user = str(user or "-")
    return re.sub(r"[^a-zA-Z0-9_.-]", "?", user)[:32]


# ------------------------------------------------------------------ misc


def _parse_cookie(handler, name):
    try:
        c = http.cookies.SimpleCookie()
        c.load(handler.headers.get("Cookie", ""))
        return c[name].value
    except Exception:
        return None


def _client_ip(handler):
    """Client IP for rate limiting. X-Forwarded-For is honoured only when the
    direct peer is an explicitly trusted proxy (RD_TRUSTED_PROXIES), so a
    spoofed header from an arbitrary client cannot rotate its identity (#16)."""
    peer = handler.client_address[0]
    if peer in TRUSTED_PROXIES:
        return handler.headers.get("X-Forwarded-For", peer).split(",")[0].strip()
    return peer


def _login_allowed_wrapper(ip):  # pragma: no cover  (legacy name alias)
    return _login_allowed(ip)
