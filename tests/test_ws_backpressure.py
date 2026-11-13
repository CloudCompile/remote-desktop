"""Websocket backpressure and buffer reuse tests (#29)."""

import struct

from remote_desktop import vncproxy
from remote_desktop.ws import _ws_frame


def test_pending_takes_and_reuses():
    p = vncproxy._Pending(1024)
    assert p.take() is None
    assert p.extend(b"abc") is True
    assert p.extend(b"def") is True
    assert p.take() == b"abcdef"
    assert len(p) == 0
    assert p.take() is None


def test_pending_high_water_rejects():
    p = vncproxy._Pending(16)
    assert p.extend(b"x" * 16) is True
    # 4 more bytes would cross the high-water mark -> rejected, buffer intact
    assert p.extend(b"y" * 4) is False
    assert p.take() == b"x" * 16
    assert len(p) == 0


def test_pending_accumulates_past_limit_only_after_drain():
    p = vncproxy._Pending(10)
    assert p.extend(b"a" * 10) is True
    assert p.extend(b"b") is False
    assert p.take() == b"a" * 10
    assert p.extend(b"b" * 10) is True


def test_ws_frame_shapes():
    assert _ws_frame(b"hi") == b"\x82\x02hi"
    big = b"z" * 70000
    fr = _ws_frame(big)
    assert fr[0] == 0x82 and fr[1] == 127
    assert struct.unpack(">Q", fr[2:10])[0] == len(big)


def test_close_frame_carries_code_and_reason():
    fr = vncproxy.ws._ws_close_frame(1013, b"vnc-unavailable")
    assert fr[0] == 0x88
    assert struct.unpack(">H", fr[2:4])[0] == 1013
    assert fr[4:] == b"vnc-unavailable"


def test_stats_snapshot_keys():
    snap = vncproxy.stats_snapshot()
    for key in ("active", "opened", "closed", "tx_bytes", "rx_bytes", "dropped_bytes", "slow_drops", "reconnects"):
        assert key in snap


def test_shutdown_all_safe_with_no_clients():
    assert vncproxy.shutdown_all() == 0
