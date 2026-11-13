"""Authentication: cookie tokens, password check, login rate limiting.

Ownership: token mint/verify, the crypt password check, cookie parsing,
client-IP extraction (trusted-proxy aware) and the sliding-window login
rate limiter. Session state (PASS_HASH/SECRET) lives here — tests patch
this module's globals. The password hash file itself is only read here;
its contents are never logged (#24 privacy).
"""

import base64
import hashlib
import hmac
import http.cookies
import time

try:
    import crypt  # removed in Python 3.13; login falls back to "no logins"
except ImportError:  # pragma: no cover
    crypt = None

from . import config

AUTH_USER = config.AUTH_USER
PASS_FILE = config.PASS_FILE

try:
    PASS_HASH = open(PASS_FILE).read().strip()
except Exception:
    PASS_HASH = ""  # logins disabled if the hash is missing
SECRET = hashlib.sha256((PASS_HASH + "rd-secret-v2").encode()).digest()

TOKEN_TTL = 7 * 24 * 3600  # 7 days
LOGIN_ATTEMPTS = {}  # ip -> [timestamps]  (simple rate limit)
LOGIN_ATTEMPTS_MAX_IPS = 10_000  # hard bound so a spoof flood cannot grow it forever
LOGIN_WINDOW = 600  # 10 minutes
LOGIN_MAX_ATTEMPTS = 5
TRUSTED_PROXIES = config.TRUSTED_PROXIES


def _make_token(user):
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{user}:{exp}".encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()


def _verify_token(tok):
    if not tok or not PASS_HASH:
        return False
    try:
        dec = base64.urlsafe_b64decode(tok.encode() + b"==")
        payload, sig = dec.rsplit(b":", 1)
        user, exp = payload.decode().rsplit(":", 1)
        if user != AUTH_USER or int(exp) < int(time.time()):
            return False
        expect = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.decode(), expect)
    except Exception:
        return False


def _verify_password(pw):
    try:
        return bool(PASS_HASH and crypt) and crypt.crypt(pw, PASS_HASH) == PASS_HASH
    except Exception:
        return False


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


def _login_allowed(ip):
    """Sliding-window rate limit. State is bounded: expired entries are
    pruned on access and the whole table is capped so a flood of spoofed
    IPs cannot grow memory forever (#16)."""
    now = time.time()
    if len(LOGIN_ATTEMPTS) > LOGIN_ATTEMPTS_MAX_IPS:
        for k in sorted(LOGIN_ATTEMPTS, key=lambda k: max(LOGIN_ATTEMPTS[k], default=0))[: LOGIN_ATTEMPTS_MAX_IPS // 2]:
            del LOGIN_ATTEMPTS[k]
    attempts = LOGIN_ATTEMPTS.get(ip)
    if attempts is None:
        attempts = LOGIN_ATTEMPTS[ip] = []
    attempts[:] = [t for t in attempts if now - t < LOGIN_WINDOW]
    return len(attempts) < LOGIN_MAX_ATTEMPTS
