"""Pure-Python DES for classic VNC auth (#122): correctness vectors."""

import pytest

from remote_desktop import _des


def test_fips_walkthrough_vector():
    # the canonical DES specification walkthrough vector
    key = bytes.fromhex("133457799BBCDFF1")
    pt = bytes.fromhex("0123456789ABCDEF")
    assert _des.encrypt_block(key, pt) == bytes.fromhex("85E813540F0AB405")


def test_block_sizes_enforced():
    with pytest.raises(ValueError):
        _des.encrypt_block(b"short", b"12345678")
    with pytest.raises(ValueError):
        _des.encrypt_block(b"12345678", b"short")


def test_vnc_key_bit_reversal():
    # RFB §7.2.2: each password byte is bit-reversed; 'A' (0x41) -> 0x82
    key = _des.vnc_key(b"A")
    assert key[0] == 0x82
    assert key[1:] == b"\x00" * 7  # padded with nulls to 8 bytes
