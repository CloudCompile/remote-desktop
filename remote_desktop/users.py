"""Multi-user store: accounts, roles and per-user state layout (#111/#112).

Ownership: the JSON accounts file under the state dir (``users.json``;
stdlib only, no DB — [ADR-0002](adr/ADR-0002-stdlib-only.md) and
[ADR-0005](adr/ADR-0005-file-backed-state.md)), password-hash custody,
role/status changes, account lifecycle (create, disable, reset, delete)
and the automatic migration of a single ``.passhash`` deployment to a
default admin user (#111).

Back-compat (#111): when only ``RD_USER``/``RD_PASSFILE`` exist the
server behaves exactly as before — that account becomes the default
admin. The store is created lazily on first migration; every accessor
treats a missing store as "single legacy admin".

Password hashes are crypt SHA-512 (``$6$``), same as ``.passhash``.
Hashes are never logged or returned by any API (#24).
"""

import hashlib
import os
import re
import secrets
import threading
import time

try:
    import crypt  # removed in Python 3.13; password ops fall back to "no login"
except ImportError:  # pragma: no cover
    crypt = None

from . import config, logging as log

USERS_VERSION = 1
USERS_MAX = 50  # small-host budget; a shared family/team box needs no more

ROLES = ("viewer", "operator", "admin")
ROLE_LEVEL = {"viewer": 1, "operator": 2, "admin": 3}
STATUSES = ("active", "disabled")

USERNAME_MIN = 2
USERNAME_MAX = 32
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
DEFAULT_ROLE = "operator"

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "data": None}  # mtime-stamped read cache


class UsersError(Exception):
    """User-store operation refused; message is safe to show."""


def users_dir():
    """Root of all per-user state (files, pins, clipboard history)."""
    return config.VALUES.get("users_dir") or os.path.join(config.STATE_DIR, "users")


def users_file():
    return os.path.join(users_dir(), "users.json")


def user_dir(user):
    """Runtime state directory for one user (created on first login)."""
    return os.path.join(users_dir(), _safe_component(user))


def user_files_dir(user):
    return os.path.join(user_dir(user), "files")


# ---------------------------------------------------------------- storage


def _safe_component(user):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(user))[:USERNAME_MAX]


def _load(force=False):
    """Parsed store (mtime-cached). Missing file -> None (legacy single user)."""
    path = users_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if not force and _cache["data"] is not None and _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        import json

        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
            raise ValueError("bad users.json shape")
        data = {"version": int(raw.get("version", USERS_VERSION)), "users": {}}
        for name, rec in list(raw["users"].items())[: USERS_MAX * 2]:
            rec = _clean_record(rec)
            if rec and isinstance(name, str) and _USERNAME_RE.match(name):
                data["users"][name] = rec
    except Exception as e:
        log.log(f"users store unreadable ({e}); treating as legacy single-user")
        return None
    _cache.update({"path": path, "mtime": mtime, "data": data})
    return data


def _clean_record(rec):
    if not isinstance(rec, dict):
        return None
    out = {
        "hash": str(rec.get("hash") or ""),
        "role": rec.get("role") if rec.get("role") in ROLES else DEFAULT_ROLE,
        "status": rec.get("status") if rec.get("status") in STATUSES else "active",
        "created": int(rec.get("created") or 0),
        "display": str(rec.get("display") or "")[:8],
        "totp": rec.get("totp") if isinstance(rec.get("totp"), dict) else {},
        "recovery": [h for h in rec.get("recovery", []) if isinstance(h, str)][:20],
        "last_login": int(rec.get("last_login") or 0),
    }
    return out


def _save(data):
    """Atomic bounded-JSON write (#17 pattern), 0600."""
    import json

    path = users_file()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    _cache.update({"path": path, "mtime": os.path.getmtime(path), "data": data})


def _mutate(fn):
    """Read-modify-write under the lock; reloads from disk first so CLI
    (``--user-add``) and a running server converge. fn(data) mutates or
    returns an error string. A fresh store always starts with the
    migrated default admin (#111)."""
    with _lock:
        data = _load(force=True)
        if data is None:
            data = {"version": USERS_VERSION, "users": {}}
            try:
                legacy = open(config.PASS_FILE).read().strip()
            except Exception:
                legacy = ""
            if legacy and config.AUTH_USER not in data["users"]:
                data["users"][config.AUTH_USER] = {
                    "hash": legacy,
                    "role": "admin",
                    "status": "active",
                    "created": int(time.time()),
                    "display": config.DISPLAY,
                    "totp": {},
                    "recovery": [],
                    "last_login": 0,
                }
        err = fn(data)
        if err:
            return err
        _save(data)
        return None


# ---------------------------------------------------------------- migration


def migrate_single_user():
    """Create the default admin from ``.passhash`` when no store exists
    (#111). Idempotent, runs at startup before the socket binds."""
    if _load() is not None:
        return False
    try:
        pass_hash = open(config.PASS_FILE).read().strip()
    except Exception:
        pass_hash = ""
    if not pass_hash:
        return False
    user = config.AUTH_USER

    def _mk(data):
        if user in data["users"]:
            return None
        data["users"][user] = {
            "hash": pass_hash,
            "role": "admin",
            "status": "active",
            "created": int(time.time()),
            "display": config.DISPLAY,
            "totp": {},
            "recovery": [],
            "last_login": 0,
        }
        return None

    err = _mutate(_mk)
    if err is None:
        log.log(f"users: migrated single-user deployment to default admin {user!r} (#111)")
        return True
    return False


# ---------------------------------------------------------------- queries


def exists(user):
    data = _load()
    if data is None:
        return user == config.AUTH_USER  # legacy single admin
    return user in data["users"]


def record(user):
    data = _load()
    if data is None:
        if user != config.AUTH_USER:
            return None
        # synthetic legacy admin: hash lives in .passhash (auth.PASS_HASH)
        return {
            "hash": "",
            "role": "admin",
            "status": "active",
            "created": 0,
            "display": config.DISPLAY,
            "totp": {},
            "recovery": [],
            "last_login": 0,
            "legacy": True,
        }
    rec = data["users"].get(user)
    if rec is not None:
        rec = dict(rec)
        rec["legacy"] = False
    return rec


def role(user):
    rec = record(user)
    return rec["role"] if rec else None


def is_admin(user):
    return (ROLE_LEVEL.get(role(user)) or 0) >= ROLE_LEVEL["admin"]


def is_active(user):
    rec = record(user)
    return bool(rec) and rec["status"] == "active"


def is_default_admin(user):
    return user == config.AUTH_USER


def store_exists():
    """True when the users.json store has been created (i.e. migrated)."""
    return os.path.isfile(users_file())


def is_default_admin_configured():
    """The default admin is usable: either the store has a record for it
    or the legacy .passhash deployment is in place."""
    if store_exists():
        return exists(config.AUTH_USER)
    return False


def invalidate():
    """Drop the read cache (SIGHUP / external file edits)."""
    _cache.update({"path": None, "mtime": None, "data": None})


def display_for(user):
    """Per-user display (":1" default). Multi-desktop mapping lands with
    the v3 display work (#141); until then non-default users share :1
    unless explicitly configured in the store."""
    rec = record(user)
    return (rec.get("display") if rec else "") or config.DISPLAY


def verify_password(user, password):
    """crypt SHA-512 check against the record (or .passhash for the
    legacy admin). Never raises; returns False on any doubt."""
    if not password or not isinstance(password, str):
        return False
    rec = record(user)
    if not rec or rec["status"] != "active":
        return False
    if rec["legacy"]:
        from . import auth

        return auth._verify_password(password)
    stored = rec["hash"]
    if not stored or not crypt:
        return False
    try:
        return crypt.crypt(password, stored) == stored
    except Exception:
        return False


# ---------------------------------------------------------------- lifecycle


def valid_username(user):
    return (
        isinstance(user, str)
        and USERNAME_MIN <= len(user) <= USERNAME_MAX
        and _USERNAME_RE.match(user) is not None
        and user not in (".", "..")
    )


def hash_password(password):
    """crypt SHA-512 with a random salt. Returns "" when crypt is gone."""
    if crypt is None:
        return ""
    salt = "$6$" + secrets.token_hex(8)
    return crypt.crypt(password, salt)


def create(user, password, role=DEFAULT_ROLE, display=""):
    """Add an account. Returns (ok, error)."""
    if not valid_username(user):
        return False, "username must be 2-32 chars: lowercase letters, digits, . _ - (start with a letter)"
    if role not in ROLES:
        return False, "role must be one of " + "|".join(ROLES)
    if not password or len(password) < 8:
        return False, "password must be at least 8 characters"
    h = hash_password(password)
    if not h:
        return False, "password hashing unavailable on this python (crypt removed)"

    def _mk(data):
        if user in data["users"]:
            return "user exists"
        if len(data["users"]) >= USERS_MAX:
            return "too many users"
        data["users"][user] = {
            "hash": h,
            "role": role,
            "status": "active",
            "created": int(time.time()),
            "display": str(display or "")[:8],
            "totp": {},
            "recovery": [],
            "last_login": 0,
        }
        return None

    err = _mutate(_mk)
    if err:
        return False, err
    log.log(f"users: created {user!r} (role {role})")
    return True, None


def set_password(user, password):
    """Reset an account password. Returns (ok, error)."""
    if not password or len(password) < 8:
        return False, "password must be at least 8 characters"
    h = hash_password(password)
    if not h:
        return False, "password hashing unavailable on this python (crypt removed)"

    def _pw(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        rec["hash"] = h
        return None

    err = _mutate(_pw)
    if err:
        return False, err
    if user == config.AUTH_USER:  # legacy .passhash must stay in sync (#118)
        try:
            with open(config.PASS_FILE + ".tmp", "w") as f:
                f.write(h + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(config.PASS_FILE + ".tmp", config.PASS_FILE)
            os.chmod(config.PASS_FILE, 0o600)
        except OSError:
            pass
    log.log(f"users: password set for {user!r}")
    return True, None


def set_role(user, role):
    if role not in ROLES:
        return False, "role must be one of " + "|".join(ROLES)

    def _role(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        if rec["role"] == "admin" and role != "admin":
            admins = [u for u, r in data["users"].items() if r.get("role") == "admin" and r.get("status") == "active"]
            if len(admins) <= 1:
                return "cannot demote the last active admin"
        rec["role"] = role
        return None

    err = _mutate(_role)
    if not err:
        log.log(f"users: role of {user!r} set to {role}")
    return not err, err


def set_status(user, status):
    """Enable/disable. Disabling is what revokes a user's sessions (#111)."""
    if status not in STATUSES:
        return False, "status must be active|disabled"

    def _st(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        if status != "active" and rec["role"] == "admin":
            admins = [u for u, r in data["users"].items() if r.get("role") == "admin" and r.get("status") == "active"]
            if len(admins) <= 1:
                return "cannot disable the last active admin"
        rec["status"] = status
        return None

    err = _mutate(_st)
    if not err:
        log.log(f"users: status of {user!r} set to {status}")
    return not err, err


def delete(user):
    """Remove an account. The default admin cannot be deleted."""
    if is_default_admin(user):
        return False, "the default admin cannot be deleted"

    def _del(data):
        if user not in data["users"]:
            return "no such user"
        admins = [u for u, r in data["users"].items() if r.get("role") == "admin" and r.get("status") == "active"]
        rec = data["users"][user]
        if rec.get("role") == "admin" and rec.get("status") == "active" and len(admins) <= 1:
            return "cannot delete the last active admin"
        del data["users"][user]
        return None

    err = _mutate(_del)
    if not err:
        log.log(f"users: deleted {user!r}")
    return not err, err


def list_public():
    """Account list for the admin UI — never includes hashes."""
    data = _load()
    if data is None:
        rec = record(config.AUTH_USER) or {}
        return [
            {
                "username": config.AUTH_USER,
                "role": "admin",
                "status": "active",
                "created": rec.get("created") or 0,
                "last_login": rec.get("last_login") or 0,
                "totp": bool((rec.get("totp") or {}).get("enabled")),
                "legacy": True,
                "default": True,
            }
        ]
    out = []
    for name, rec in sorted(data["users"].items()):
        out.append(
            {
                "username": name,
                "role": rec["role"],
                "status": rec["status"],
                "created": rec["created"],
                "last_login": rec["last_login"],
                "totp": bool((rec.get("totp") or {}).get("enabled")),
                "legacy": False,
                "default": name == config.AUTH_USER,
            }
        )
    return out


def touch_last_login(user):
    """Best-effort last-login stamp (does not fail the login)."""

    def _ll(data):
        rec = data["users"].get(user)
        if rec:
            rec["last_login"] = int(time.time())
        return None

    _mutate(_ll)


# ---------------------------------------------------------------- TOTP state (#109)
# The users store owns the secrets; verification math lives in totp.py.


def totp_get(user):
    rec = record(user) or {}
    t = rec.get("totp") or {}
    return {
        "enabled": bool(t.get("enabled")),
        "pending": bool(t.get("pending")),
        "secret": t.get("enabled") and t.get("secret"),
    }


def totp_set_pending(user, secret):
    """Stage a new secret (not yet active until /api/2fa/enable)."""

    def _set(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        rec["totp"] = {
            "enabled": bool(rec.get("totp", {}).get("enabled")),
            "pending": secret,
            "secret": rec.get("totp", {}).get("secret", ""),
        }
        return None

    return not _mutate(_set)


def totp_enable(user, secret):
    """Activate the staged secret."""

    def _en(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        pending = rec.get("totp", {}).get("pending")
        if not pending or pending != secret:
            return "no pending enrollment"
        rec["totp"] = {"enabled": True, "pending": "", "secret": secret}
        return None

    return not _mutate(_en)


def totp_secret_enabled(user):
    """The ACTIVE secret, or None."""
    rec = record(user) or {}
    t = rec.get("totp") or {}
    return t.get("secret") if t.get("enabled") else None


def totp_disable(user):
    def _dis(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        rec["totp"] = {}
        rec["recovery"] = []
        return None

    return not _mutate(_dis)


def recovery_set(user, hashes):
    def _rs(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        rec["recovery"] = list(hashes)[:20]
        return None

    return not _mutate(_rs)


def recovery_hashes(user):
    rec = record(user) or {}
    return list(rec.get("recovery") or [])


def recovery_consume(user, code):
    """Remove one used recovery code (one-time use, #109). True when found."""
    from . import totp

    hit = {"h": None}

    def _rc(data):
        rec = data["users"].get(user)
        if rec is None:
            return "no such user"
        for h in rec.get("recovery", []):
            if totp.check_recovery_code(code, h):
                hit["h"] = h
                rec["recovery"].remove(h)
                return None
        return "not found"

    ok = not _mutate(_rc)
    return ok


def ensure_layout(user):
    """Create the per-user state directories on first login (#120)."""
    d = user_dir(user)
    try:
        os.makedirs(user_files_dir(user), exist_ok=True)
        try:
            os.chmod(users_dir(), 0o700)
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d
    except OSError as e:
        log.log(f"users: cannot create per-user dir {d!r}: {e}")
        return None


def store_path_of(user):
    """Hash of the username for cache keys etc. (not a secret)."""
    return hashlib.sha256(user.encode()).hexdigest()[:16]
