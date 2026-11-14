"""TOTP two-factor authentication (RFC 6238) and recovery codes (#109).

Ownership: pure-stdlib HMAC-SHA1 time-based one-time-passwords —
secret generation, verification with a bounded drift window, the
``otpauth://`` enrollment URI, and one-time recovery-code generation +
hashing. No new runtime dependencies ([ADR-0002](adr/ADR-0002-stdlib-only.md)):
``hmac``/``hashlib``/``base64``/``struct`` only.

Secrets and recovery hashes live in the users store (``users.json``,
written by users.py) and are never logged or returned after enrollment
(#24 privacy).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

STEP = 30  # seconds per RFC 6238 time step
DIGITS = 6
DRIFT_WINDOW = 1  # ±1 step (±30 s) of clock drift tolerated
SECRET_BYTES = 20  # 160-bit secret, 32 base32 chars
RECOVERY_COUNT = 10
RECOVERY_LEN = 10  # characters, rendered XXXX-XXXX

try:
    import crypt  # noqa: F401  (used for recovery-code hashing)
except ImportError:  # pragma: no cover
    crypt = None


def generate_secret():
    """A new TOTP secret: base32, no padding (authenticator-app format)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def hotp_at(secret_b32, counter, digits=DIGITS):
    """RFC 4226 HOTP over the RFC 6238 time counter (dynamic truncation)."""
    key = base64.b32decode(secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8), casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = ((digest[offset] & 0x7F) << 24) | (digest[offset + 1] << 16) | (digest[offset + 2] << 8) | digest[offset + 3]
    return str(code % (10**digits)).zfill(digits)


def totp_at(secret_b32, at=None, digits=DIGITS, step=STEP):
    """The TOTP code valid at unix time `at` (now by default)."""
    counter = int(at if at is not None else time.time()) // step
    return hotp_at(secret_b32, counter, digits)


def verify(secret_b32, code, window=DRIFT_WINDOW, at=None, step=STEP):
    """Constant-time check of `code` against the current step and
    ±`window` neighbours. Non-numeric/short codes fail fast."""
    if not secret_b32 or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return False
    now_counter = int(at if at is not None else time.time()) // step
    ok = False
    for delta in range(-abs(window), abs(window) + 1):
        candidate = hotp_at(secret_b32, now_counter + delta)
        if hmac.compare_digest(candidate, code):
            ok = True  # keep scanning to keep the compare pattern uniform
    return ok


def otpauth_uri(user, secret_b32, issuer="Remote Desktop"):
    """The enrollment URI accepted by every common authenticator app."""
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(user)}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"
    )


# ---------------------------------------------------------------- recovery codes


def generate_recovery_codes(count=RECOVERY_COUNT):
    """One-time codes, shown to the user exactly once. Returned as plain
    text here; only their hashes are stored."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # unambiguous set
    out = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(RECOVERY_LEN))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def normalize_recovery_code(code):
    """Canonical form for comparison: upper-case, separators dropped."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def hash_recovery_code(code):
    """crypt SHA-512 of the canonical code (salted). "" when crypt is gone."""
    if crypt is None:
        return ""
    canonical = normalize_recovery_code(code)
    salt = "$6$" + secrets.token_hex(8)
    return crypt.crypt(canonical, salt)


def check_recovery_code(code, stored_hash):
    if not code or not stored_hash or crypt is None:
        return False
    canonical = normalize_recovery_code(code)
    try:
        return crypt.crypt(canonical, stored_hash) == stored_hash
    except Exception:
        return False
