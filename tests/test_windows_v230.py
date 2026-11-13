"""v2.3.0 window tests (#59 #60 #61 #73 #76): thumbnails, WM actions,
layout slots and geometry remembering — all against faked xdotool/xprop
output (mocked procs._run); no X server required."""

import re

import pytest

from remote_desktop import config, windows


@pytest.fixture
def xtmp(tmp_path, monkeypatch):
    """Sandbox the state dir geometry.json writes into."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    return tmp_path


class FakeX:
    """A scripted xdotool/xprop/ffmpeg surface: window table + recorded
    commands, standing in for procs._run."""

    def __init__(self, wins=None, rc=0):
        self.wins = wins or {}  # wid -> dict(inst, cls, name, x, y, w, h)
        self.rc = rc
        self.calls = []
        self.ffmpeg_out = b""

    def __call__(self, cmd, timeout=6, env=None):
        self.calls.append(list(cmd))
        if cmd[0] == "xdotool":
            if cmd[1] == "search":
                args, only = cmd[2:], False
                if args and args[0] == "--onlyvisible":
                    only, args = True, args[1:]
                kind, pat = args[0], args[1]
                hit = [
                    wid
                    for wid, w in self.wins.items()
                    if (not only or w.get("visible", True)) and self._match(kind, pat, w)
                ]
                return 0, "\n".join(hit), ""
            if cmd[1] == "getwindowgeometry":
                w = self.wins.get(cmd[-1])
                if not w:
                    return 1, "", "no such window"
                return 0, f"X={w['x']}\nY={w['y']}\nWIDTH={w['w']}\nHEIGHT={w['h']}\n", ""
            if cmd[1] == "getwindowname":
                w = self.wins.get(cmd[-1])
                if not w:
                    return 1, "", "no such window"
                return 0, w["name"], ""
            if cmd[1] == "getdisplaygeometry":
                return 0, "1280 800", ""
        if cmd[0] == "xprop":
            if cmd[1] == "-root":
                return 0, "_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x400001", ""
            w = self.wins.get(cmd[2])
            if not w:
                return 1, "", "no such window"
            return 0, f'WM_CLASS(STRING) = "{w["inst"]}", "{w["cls"]}"', ""
        if cmd[0] == "wmctrl":
            return self.rc, "", ""
        if cmd[0] == "ffmpeg":
            return 0, self.ffmpeg_out or b"\xff\xd8fakejpeg", b""  # bytes ok: callers ignore types here
        return 0, "", ""

    @staticmethod
    def _match(kind, pat, w):
        try:
            rx = re.compile(pat)
        except re.error:
            return False
        field = {"--classname": "inst", "--class": "cls", "--name": "name"}.get(kind)
        if not field:
            return False
        return rx.search(w[field]) is not None


@pytest.fixture
def fake_x(monkeypatch):
    fx = FakeX()
    monkeypatch.setattr("remote_desktop.procs._run", fx)
    monkeypatch.setattr("shutil.which", lambda name: None)  # no wmctrl/ffmpeg unless said
    return fx


# ------------------------------------------------------------------ #59


def test_list_windows_includes_position(fake_x):
    fake_x.wins = {
        "111": dict(inst="xterm", cls="XTerm", name="Terminal", x=10, y=20, w=800, h=600, visible=True),
    }
    ws = windows.list_windows()
    assert len(ws) == 1
    assert ws[0]["x"] == "10" and ws[0]["y"] == "20"
    assert ws[0]["w"] == "800" and ws[0]["name"] == "Terminal"


def test_thumbnail_builds_x11grab_cmd(fake_x, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    fake_x.wins = {"555": dict(inst="x", cls="C", name="w", x=30, y=40, w=640, h=480, visible=True)}
    data, err = windows.window_thumbnail("555")
    assert err is None and data
    ff = next(c for c in fake_x.calls if c[0] == "ffmpeg")
    assert ff[ff.index("-f") + 1] == "x11grab"
    assert ff[ff.index("-video_size") + 1] == "640x480"
    assert ff[ff.index("-i") + 1].startswith(":1+") and ff[ff.index("-i") + 1].endswith("+30,40")
    assert ff[ff.index("-frames:v") + 1] == "1"


def test_thumbnail_caps_width_at_320(fake_x, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    fake_x.wins = {"555": dict(inst="x", cls="C", name="w", x=0, y=0, w=1280, h=800, visible=True)}
    windows.window_thumbnail("555")
    ff = next(c for c in fake_x.calls if c[0] == "ffmpeg")
    assert ff[ff.index("-vf") + 1].startswith("scale=320:")


def test_thumbnail_never_raises(fake_x, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # ffmpeg missing
    data, err = windows.window_thumbnail("555")
    assert data is None and "ffmpeg" in err
    assert windows.window_thumbnail("not-a-hid")[1] == "invalid window id"
    assert windows.window_thumbnail("")[1] == "invalid window id"
    data, err = windows.window_thumbnail("999")
    assert data is None and err  # window absent in the fake


# ------------------------------------------------------------------ #60


def test_action_rejects_bad_ids(fake_x):
    assert windows.window_action("abc", "close")["ok"] is False
    assert windows.window_action("", "close")["ok"] is False
    assert windows.window_action("1" * 20, "close")["ok"] is False


def test_action_unknown_and_unmanaged(fake_x, xtmp):
    fake_x.wins = {}
    r = windows.window_action("42", "explode")
    assert r["ok"] is False and "unknown action" in r["error"]
    r = windows.window_action("42", "close")  # no such window
    assert r["ok"] is False and "no such window" in r["error"]
    # an unmanaged window (no WM_CLASS, e.g. the desktop) is refused
    fake_x.wins = {"42": dict(inst="", cls="", name="", x=0, y=0, w=1280, h=800)}
    r = windows.window_action("42", "close")
    assert r["ok"] is False and "not a managed" in r["error"]


def test_close_uses_xdotool_without_wmctrl(fake_x, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="Terminal", x=0, y=0, w=800, h=600)}
    r = windows.window_action("42", "close")
    assert r["ok"] is True
    assert any(c[:2] == ["xdotool", "windowkill"] and c[2] == "42" for c in fake_x.calls)


def test_close_via_wm_delete_with_wmctrl(fake_x, monkeypatch, xtmp):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None)
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="Terminal", x=0, y=0, w=800, h=600)}
    r = windows.window_action("42", "close")
    assert r["ok"] is True
    assert any(c[:2] == ["wmctrl", "-ic"] for c in fake_x.calls)
    assert not any(c[:2] == ["xdotool", "windowkill"] for c in fake_x.calls)


def test_minimize_and_maximize(fake_x, monkeypatch, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="T", x=0, y=0, w=800, h=600)}
    assert windows.window_action("42", "minimize")["ok"] is True
    assert any(c[:2] == ["xdotool", "windowminimize"] for c in fake_x.calls)
    r = windows.window_action("42", "maximize")  # no wmctrl -> documented fallback
    assert r["ok"] is True and r["via"] == "fallback"
    assert any(c[:2] == ["xdotool", "windowsize"] and c[-2:] == ["1280", "800"] for c in fake_x.calls)


def test_move_and_resize_validate_ints(fake_x, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="T", x=0, y=0, w=800, h=600)}
    assert windows.window_action("42", "move", x="bad", y=0)["ok"] is False
    assert windows.window_action("42", "resize", w=10, h=None)["ok"] is False
    assert windows.window_action("42", "move", x=100, y=50)["ok"] is True
    assert windows.window_action("42", "resize", w=400, h=300)["ok"] is True
    assert any(c[:2] == ["xdotool", "windowmove"] and c[-2:] == ["100", "50"] for c in fake_x.calls)


# ------------------------------------------------------------------ #61


def test_layout_slots_compute_from_display(fake_x, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="T", x=0, y=0, w=800, h=600)}
    r = windows.window_layout("42", "right")
    assert r["ok"] is True and r == {"ok": True, "slot": "right", "x": 640, "y": 0, "w": 640, "h": 800}
    assert windows.window_layout("42", "left-third")["w"] == 427  # round(1280/3)


def test_layout_unknown_slot_and_bounds(fake_x, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="T", x=0, y=0, w=800, h=600)}
    r = windows.window_layout("42", "diagonal")
    assert r["ok"] is False and "unknown slot" in r["error"]
    # clamped inside the display
    r = windows.window_layout("42", "br")
    assert r["x"] + r["w"] <= 1280 and r["y"] + r["h"] <= 800


def test_layout_remembered_per_display_size(fake_x, xtmp):
    fake_x.wins = {"42": dict(inst="xterm", cls="XTerm", name="T", x=0, y=0, w=800, h=600)}
    windows.window_layout("42", "left")
    assert windows.last_layout(1280, 800) == "left"
    assert windows.last_layout(1024, 768) is None


# ------------------------------------------------------------------ #76


def test_geometry_remember_and_restore(fake_x, xtmp):
    fake_x.wins = {"77": dict(inst="gedit", cls="Gedit", name="Editor", x=15, y=25, w=700, h=500)}
    assert windows.remember_geometry("77") is True
    data = (xtmp / "geometry.json").read_text()
    assert "Gedit" in data
    # a second window of the same class picks the geometry up
    fake_x.calls.clear()
    assert windows.restore_geometry("88", "Gedit") is True
    mv = next(c for c in fake_x.calls if c[:2] == ["xdotool", "windowmove"])
    sz = next(c for c in fake_x.calls if c[:2] == ["xdotool", "windowsize"])
    assert mv[-2:] == ["15", "25"] and sz[-2:] == ["700", "500"]


def test_geometry_toggle_stops_recording(fake_x, xtmp):
    fake_x.wins = {"77": dict(inst="gedit", cls="Gedit", name="Editor", x=15, y=25, w=700, h=500)}
    s = windows.geometry_settings(enabled=False)
    assert s["enabled"] is False
    assert windows.remember_geometry("77") is False
    assert windows.geometry_settings()["apps"] == 0
    windows.geometry_settings(enabled=True)
    assert windows.remember_geometry("77") is True


def test_geometry_reset_clears_entries(fake_x, xtmp):
    fake_x.wins = {"77": dict(inst="gedit", cls="Gedit", name="Editor", x=15, y=25, w=700, h=500)}
    windows.remember_geometry("77")
    s = windows.geometry_settings(reset=True)
    assert s["apps"] == 0


def test_geometry_restored_after_launch(fake_x, xtmp, monkeypatch):
    """restore_after_launch applies the saved geometry to the new window."""
    fake_x.wins = {"77": dict(inst="gedit", cls="Gedit", name="Editor", x=15, y=25, w=700, h=500)}
    windows.remember_geometry("77")
    fake_x.wins = {"88": dict(inst="gedit", cls="Gedit", name="Editor", x=0, y=0, w=800, h=600)}
    import threading

    t = threading.Thread(target=windows.restore_after_launch, args=("Gedit",), kwargs={"tries": 1})
    t.start()
    t.join(timeout=10)
    assert any(c[:2] == ["xdotool", "windowmove"] and c[-2:] == ["15", "25"] for c in fake_x.calls)


def test_focus_records_geometry(fake_x, xtmp):
    fake_x.wins = {"77": dict(inst="gedit", cls="Gedit", name="Editor", x=1, y=2, w=700, h=500)}
    r = windows.window_action("77", "focus")
    assert r["ok"] is True
    assert "Gedit" in (xtmp / "geometry.json").read_text()
