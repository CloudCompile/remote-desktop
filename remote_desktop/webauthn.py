"""WebAuthn / passkey login (#110) — opt-in, storage + ceremony layer.

Ownership: per-user credential storage (credential id, COSE public key,
sign counter, label), the registration and assertion ceremony endpoints,
and the RP-ID/origin rules behind tunnels. The actual cryptography is
delegated to the optional ``webauthn`` package:

    pip install webauthn          # extra: remote-desktop[webauthn]

and the feature is gated behind ``RD_WEBAUTHN=1`` (default off). With
the flag off — or the dependency missing — the password (and TOTP)
path keeps working unchanged; the endpoints answer with an actionable
error instead of pretending to be available (#110 acceptance: removing
the last passkey leaves a working password path).

Passkeys are PHISHING-RESISTANT but RP-ID sensitive: the RP ID must be
the registerable domain the browser sees. Behind a tunnel that means
RD_RP_ID must be set to the tunnel hostname's domain (e.g.
``example.ts.net``), and the origin must be the https URL — an
http-only origin cannot register passkeys.
"""

import base64
import json
import os
import threading
import time

from . import config, logging as log

CHALLENGE_TTL = 120  # seconds a ceremony challenge stays valid
CHALLENGES_MAX = 256
CREDENTIALS_MAX = 8  # per user
FILE_NAME = "webauthn.json"

_lock = threading.Lock()
_challenges = {}  # key -> (challenge_b64url, expires, kind)
_store = None  # lazy: {"users": {user: [cred,...]}}


class WebAuthnError(Exception):
    """Passkey ceremony refused; the message is safe to show."""


def enabled():
    """The feature flag AND the dependency (#110)."""
    return bool(config.WEBAUTHN_ENABLED)


def _require_dep():
    try:
        import webauthn  # noqa: F401  (optional dependency)

        return webauthn
    except ImportError:
        raise WebAuthnError(
            "passkeys are enabled (RD_WEBAUTHN=1) but the 'webauthn' package is not installed; "
            "install it with: pip install webauthn"
        ) from None


def rp_id_for(handler):
    """Relying Party ID: RD_RP_ID when set, else the request Host minus
    port. Misconfiguration surfaces as an actionable ceremony error (#110)."""
    if config.RP_ID:
        return config.RP_ID
    host = (handler.headers.get("Host", "") if handler else "").split(":")[0]
    if not host or host in ("127.0.0.1", "localhost"):
        # passkeys do not work on bare IP/localhost origins except in
        # special browser modes; make the failure self-explanatory
        raise WebAuthnError(
            "no RP ID configured: set RD_RP_ID to the public domain name the browser uses "
            "(passkeys cannot register against a bare IP or localhost)"
        )
    return host


def origin_for(handler):
    """The origin the browser sees (scheme + host[:port]); behind a
    trusted TLS proxy the https scheme wins."""
    from . import auth

    scheme = "https" if auth.is_secure_request(handler) else "http"
    host = handler.headers.get("Host", "")
    return f"{scheme}://{host}"


# ---------------------------------------------------------------- storage


def _path():
    from . import users

    return os.path.join(users.users_dir(), FILE_NAME)


def _load():
    global _store
    if _store is not None:
        return _store
    try:
        with open(_path()) as f:
            raw = json.load(f)
        _store = raw if isinstance(raw, dict) and isinstance(raw.get("users"), dict) else {"users": {}}
    except Exception:
        _store = {"users": {}}
    return _store


def _save():
    try:
        path = _path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_store, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass


def credentials_for(user):
    """Public view of a user's credentials (never key material)."""
    with _lock:
        st = _load()
        out = []
        for c in st["users"].get(user, []):
            out.append(
                {
                    "id": c.get("id", "")[:43],  # truncated: id is not secret but stays out of easy enumeration
                    "label": c.get("label") or "passkey",
                    "created": c.get("created"),
                    "last_used": c.get("last_used") or 0,
                }
            )
        return out


def remove_credential(user, cred_id_prefix):
    """Remove one credential by (id prefix or label). Returns (n, err)."""
    with _lock:
        st = _load()
        creds = st["users"].get(user, [])
        keep = [
            c for c in creds if not (c.get("id", "").startswith(cred_id_prefix) or c.get("label") == cred_id_prefix)
        ]
        if len(keep) == len(creds):
            return 0, "no such credential"
        st["users"][user] = keep
        _save()
    return len(creds) - len(keep), None


# ---------------------------------------------------------------- ceremonies


def _challenge_store(key, value):
    with _lock:
        now = time.time()
        for k in [k for k, v in _challenges.items() if v[1] < now]:
            del _challenges[k]
        if len(_challenges) > CHALLENGES_MAX:
            _challenges.clear()
        _challenges[key] = (value, now + CHALLENGE_TTL)


def _challenge_take(key):
    with _lock:
        item = _challenges.pop(key, None)
        if item and item[1] > time.time():
            return item[0]
    return None


def registration_options(user, handler):
    """Begin passkey registration (#110). Requires the optional dep."""
    webauthn = _require_dep()
    rp_id = rp_id_for(handler)
    opts = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="Remote Desktop",
        user_id=base64.urlsafe_b64encode(user.encode()).decode().rstrip("="),
        user_name=user,
        user_display_name=user,
    )
    from webauthn.helpers import options_to_json

    _challenge_store(("reg", user), base64.urlsafe_b64encode(opts.challenge).decode())
    return json.loads(options_to_json(opts))


def registration_verify(user, handler, body):
    """Finish registration: verify + store the credential."""
    webauthn = _require_dep()
    challenge = _challenge_take(("reg", user))
    if not challenge:
        raise WebAuthnError("registration challenge expired or missing; restart enrollment")
    try:
        from webauthn.helpers.structs import RegistrationCredential as _RC  # noqa: F401

        cred = webauthn.verify_registration_response(
            credential=body.get("credential"),
            expected_challenge=base64.urlsafe_b64decode(challenge.encode()),
            expected_origin=origin_for(handler),
            expected_rp_id=rp_id_for(handler),
        )
    except Exception as e:
        raise WebAuthnError(f"registration rejected: {e}") from None
    label = str(body.get("label") or "passkey")[:60]
    rec = {
        "id": base64.urlsafe_b64encode(cred.credential_id).decode(),
        "public_key": base64.urlsafe_b64encode(cred.credential_public_key).decode(),
        "sign_count": int(cred.sign_count or 0),
        "label": label,
        "created": int(time.time()),
        "last_used": 0,
    }
    with _lock:
        st = _load()
        creds = st["users"].setdefault(user, [])
        creds.append(rec)
        del creds[: max(0, len(creds) - CREDENTIALS_MAX)]  # keep the newest
        _save()
    return {"id": rec["id"][:43], "label": label}


def authentication_options(user=None, handler=None):
    """Begin an assertion (login). Works for a known user or discoverable
    credentials (empty allow-list)."""
    webauthn = _require_dep()
    rp_id = rp_id_for(handler)
    allow = []
    if user:
        for c in _load()["users"].get(user, []):
            allow.append({"id": c["id"], "type": "public-key"})
    opts = webauthn.generate_authentication_options(rp_id=rp_id, allow_credentials=allow or None)
    _challenge_store(("auth", user or "*"), base64.urlsafe_b64encode(opts.challenge).decode())
    from webauthn.helpers import options_to_json

    return json.loads(options_to_json(opts))


def authentication_verify(handler, body):
    """Finish an assertion: returns the username on success."""
    webauthn = _require_dep()
    user = str(body.get("username") or "") or None
    challenge = _challenge_take(("auth", user or "*"))
    if not challenge:
        raise WebAuthnError("authentication challenge expired; restart the ceremony")
    cred_id = body.get("credential", {}).get("id", "")
    rec = None
    for u, creds in _load()["users"].items():
        for c in creds:
            if c["id"] == cred_id:
                rec = (u, c)
                break
    if rec is None:
        raise WebAuthnError("unknown credential (not registered on this server)")
    owner, c = rec
    try:
        result = webauthn.verify_authentication_response(
            credential=body.get("credential"),
            expected_challenge=base64.urlsafe_b64decode(challenge.encode()),
            expected_origin=origin_for(handler),
            expected_rp_id=rp_id_for(handler),
            credential_public_key=base64.urlsafe_b64decode(c["public_key"].encode()),
            credential_current_sign_count=int(c.get("sign_count") or 0),
        )
    except Exception as e:
        raise WebAuthnError(f"assertion rejected: {e}") from None
    with _lock:
        st = _load()
        for cc in st["users"][owner]:
            if cc["id"] == cred_id:
                cc["sign_count"] = int(getattr(result, "new_sign_count", 0) or 0)
                cc["last_used"] = int(time.time())
        _save()
    return owner
