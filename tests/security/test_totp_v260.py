"""TOTP (RFC 6238) and recovery codes (#109).

The vectors are the published RFC 6238 SHA-1 test vectors (secret
"12345678901234567890" — "12345678901234567890123456789012" for 8-digit
sets), so any authenticator-app behaviour change is caught immediately.
"""

import time

import pytest

from remote_desktop import totp


SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"


@pytest.mark.parametrize(
    "at,expected",
    [
        (59, "287082"),  # 1970 + 59 s, 6 digits
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_rfc6238_vectors_sha1_6_digits(at, expected):
    assert totp.totp_at(SECRET, at=at) == expected


def test_verify_accepts_current_and_drifts_within_window():
    now = 1_111_111_109
    assert totp.verify(SECRET, "081804", at=now)
    assert totp.verify(SECRET, totp.totp_at(SECRET, at=now - 30), at=now)  # one step back
    assert totp.verify(SECRET, totp.totp_at(SECRET, at=now + 30), at=now)  # one step forward


def test_verify_rejects_outside_window_and_garbage():
    now = 1_111_111_109
    assert not totp.verify(SECRET, "081804", at=now + 120)  # two steps later
    assert not totp.verify(SECRET, "000000", at=now)
    assert not totp.verify(SECRET, "", at=now)
    assert not totp.verify(SECRET, "abc", at=now)
    assert not totp.verify("", "081804", at=now)
    assert not totp.verify(None, "081804", at=now)


def test_secret_generation_is_base32_and_unique():
    a, b = totp.generate_secret(), totp.generate_secret()
    assert a != b and len(a) >= 32
    import base64

    base64.b32decode(a + "=" * ((8 - len(a) % 8) % 8))  # must decode


def test_otpauth_uri_shape():
    uri = totp.otpauth_uri("admin", "ABC234DEF", issuer="Remote Desktop")
    assert uri.startswith("otpauth://totp/Remote%20Desktop:admin?")
    assert "secret=ABC234DEF" in uri and "digits=6" in uri and "period=30" in uri


def test_recovery_codes_are_once_only(monkeypatch):
    codes = totp.generate_recovery_codes()
    assert len(codes) == totp.RECOVERY_COUNT
    hashes = [totp.hash_recovery_code(c) for c in codes]
    assert all(hashes), "crypt-based hashing must work on the test python"
    code = codes[0]
    assert totp.check_recovery_code(code, hashes[0])
    # re-use of the SAME code against the same hash still verifies (the
    # one-time property is enforced by REMOVING the hash on use — see the
    # users.recovery_consume test), while other codes verify independently
    assert totp.check_recovery_code(codes[1], hashes[1])
    assert not totp.check_recovery_code("NOPE-NOPE", hashes[0])


def test_recovery_code_normalisation():
    assert totp.normalize_recovery_code("ab3de-f5hij") == "AB3DEF5HIJ"
    assert totp.normalize_recovery_code(" ab3de f5hij ") == "AB3DEF5HIJ"
