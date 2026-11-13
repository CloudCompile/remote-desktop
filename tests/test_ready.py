"""Readiness probe tests (#40)."""

import pytest

from remote_desktop import ready


@pytest.fixture(autouse=True)
def fresh_cache():
    ready.ready_cache_clear()
    yield
    ready.ready_cache_clear()


def _patch_probes(monkeypatch, **results):
    fns = {}
    for name, (ok, reason) in results.items():
        calls = []

        def make(ok=ok, reason=reason, calls=calls):
            def fn():
                calls.append(1)
                return ok, reason

            return fn

        fns[name] = make()
    monkeypatch.setattr(ready, "PROBES", fns)
    return fns


def test_all_ok_when_probes_ok(monkeypatch):
    _patch_probes(
        monkeypatch,
        x=(True, "1280x800"),
        vnc=(True, "127.0.0.1:5901"),
        pulse=(True, "ok"),
        ffmpeg=(True, "/usr/bin/ffmpeg"),
    )
    snap = ready.ready_snapshot()
    assert snap["ok"] is True
    assert all(d["ok"] for d in snap["dependencies"].values())


def test_503_shape_when_dependency_down(monkeypatch):
    _patch_probes(
        monkeypatch, x=(False, "xdotool not installed"), vnc=(True, "ok"), pulse=(True, "ok"), ffmpeg=(True, "ok")
    )
    snap = ready.ready_snapshot()
    assert snap["ok"] is False
    assert snap["dependencies"]["x"]["ok"] is False
    assert "xdotool" in snap["dependencies"]["x"]["reason"]


def test_probe_results_cached_within_ttl(monkeypatch):
    fns = _patch_probes(monkeypatch, x=(True, "ok"), vnc=(True, "ok"), pulse=(True, "ok"), ffmpeg=(True, "ok"))
    ready.ready_snapshot()
    ready.ready_snapshot()
    ready.ready_snapshot()
    for fn in fns.values():
        assert len(fn.__closure__[0].cell_contents) == 1  # each probe ran once


def test_cache_cleared_forces_reprobe(monkeypatch):
    fns = _patch_probes(monkeypatch, x=(True, "ok"), vnc=(True, "ok"), pulse=(True, "ok"), ffmpeg=(True, "ok"))
    ready.ready_snapshot()
    ready.ready_cache_clear()
    ready.ready_snapshot()
    for fn in fns.values():
        assert len(fn.__closure__[0].cell_contents) == 2


def test_exception_in_probe_becomes_failure(monkeypatch):
    def boom():
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(
        ready,
        "PROBES",
        {"x": boom, "vnc": (lambda: (True, "ok")), "pulse": (lambda: (True, "ok")), "ffmpeg": (lambda: (True, "ok"))},
    )
    snap = ready.ready_snapshot()
    assert snap["ok"] is False
    assert "socket exploded" in snap["dependencies"]["x"]["reason"]


def test_reasons_are_single_line(monkeypatch):
    _patch_probes(
        monkeypatch,
        x=(False, "Authorization required\n\nError: Can't open display\n"),
        vnc=(True, "ok"),
        pulse=(True, "ok"),
        ffmpeg=(True, "ok"),
    )
    snap = ready.ready_snapshot()
    assert "\n" not in snap["dependencies"]["x"]["reason"]
