"""Regression tests for phone-to-desktop paste semantics."""

from remote_desktop import server


class FakeHandler:
    def __init__(self, body):
        self.body = body
        self.user = "tester"
        self.response = None

    def _body(self):
        return self.body

    def _json(self, status, obj):
        self.response = (status, obj)
        return self.response


def test_auto_multiline_uses_clipboard_paste(monkeypatch):
    h = FakeHandler({"text": "first line\nsecond line", "mode": "auto"})
    calls = []
    monkeypatch.setattr(
        server.clipboard, "clipboard_set", lambda text, user=None: calls.append((text, user)) or {"ok": True}
    )
    monkeypatch.setattr(server.procs, "_run", lambda argv, timeout=10: (calls.append(argv) or 0, "", ""))

    assert server.Handler._paste(h) == (200, {"ok": True, "error": None})
    assert calls[0] == ("first line\nsecond line", "tester")
    assert calls[1] == ["xdotool", "key", "--clearmodifiers", "ctrl+v"]


def test_legacy_keys_multiline_is_safely_upgraded(monkeypatch):
    """Older clients sending the default keys mode must not press Return per line."""
    h = FakeHandler({"text": "one\ntwo", "mode": "keys"})
    calls = []
    monkeypatch.setattr(
        server.clipboard, "clipboard_set", lambda text, user=None: calls.append((text, user)) or {"ok": True}
    )
    monkeypatch.setattr(server.procs, "_run", lambda argv, timeout=10: (calls.append(argv) or 0, "", ""))

    assert server.Handler._paste(h)[0] == 200
    assert calls[1] == ["xdotool", "key", "--clearmodifiers", "ctrl+v"]


def test_auto_single_line_uses_keystrokes(monkeypatch):
    h = FakeHandler({"text": "short text", "mode": "auto"})
    calls = []
    monkeypatch.setattr(server.procs, "_run", lambda argv, timeout=10: (calls.append(argv) or 0, "", ""))

    assert server.Handler._paste(h) == (200, {"ok": True, "error": None})
    assert calls[0][:4] == ["xdotool", "type", "--clearmodifiers", "--delay"]


def test_auto_bulk_single_line_uses_clipboard(monkeypatch):
    text = "x" * 300
    h = FakeHandler({"text": text, "mode": "auto"})
    calls = []
    monkeypatch.setattr(
        server.clipboard, "clipboard_set", lambda value, user=None: calls.append((value, user)) or {"ok": True}
    )
    monkeypatch.setattr(server.procs, "_run", lambda argv, timeout=10: (calls.append(argv) or 0, "", ""))

    assert server.Handler._paste(h)[0] == 200
    assert calls[0] == (text, "tester")
    assert calls[1][-1] == "ctrl+v"
