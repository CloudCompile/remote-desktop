"""v2.3.0 app tests (#62 #63 #65 #73 #75 #77): flatpak/snap discovery,
custom entries (no-shell validation), recency ranking, launch hints and
the ETag cache — with faked xdotool output for focus matching (#73)."""

import json

import pytest

from remote_desktop import apps, config, state


@pytest.fixture
def sdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_backends(monkeypatch):
    """Keep flatpak/snap discovery silent unless a test opts in."""
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setattr(apps, "_apps_cache", {"fp": None, "etag": None, "body": b""})


# ----------------------------------------------------------------- #63


def test_custom_app_validation(sdir):
    ok, err = apps.validate_custom_app({"label": "My Tool", "cmd": ["mytool", "--flag"]})
    assert err is None and ok["id"] and ok["name"] == "My Tool" and ok["source"] == "custom"
    # label limits
    assert apps.validate_custom_app({"label": "", "cmd": ["x"]})[1] is not None
    assert apps.validate_custom_app({"label": "x" * 61, "cmd": ["x"]})[1] is not None
    # cmd array limits
    assert apps.validate_custom_app({"label": "x", "cmd": []})[1] is not None
    assert apps.validate_custom_app({"label": "x", "cmd": ["a"] * 9})[1] is not None
    assert apps.validate_custom_app({"label": "x", "cmd": ["a" * 201]})[1] is not None
    assert apps.validate_custom_app({"label": "x", "cmd": "not-a-list"})[1] is not None
    assert apps.validate_custom_app({"label": "x", "cmd": ["a\x00b"]})[1] is not None


def test_custom_app_no_shell(sdir):
    for wrapper in ("sh", "bash", "/bin/bash", "zsh"):
        r = apps.validate_custom_app({"label": "X", "cmd": [wrapper, "-c", "rm -rf /"]})
        assert r[1] and "no shell" in r[1], wrapper


def test_custom_app_crud_roundtrip(sdir, no_backends):
    r = apps.add_custom_app({"label": "My Tool", "cmd": ["mytool", "--flag"], "wmclass": "Mytool"})
    assert r["ok"] is True
    r2 = apps.add_custom_app({"label": "Edited", "cmd": ["mytool"], "id": r["app"]["id"]})
    assert r2["ok"] is True  # same id replaces
    got = apps.list_custom_apps()
    assert len(got) == 1 and got[0]["label"] == "Edited"
    assert any(a["id"] == r["app"]["id"] and a["source"] == "custom" for a in apps.all_apps())
    assert apps.delete_custom_app(r["app"]["id"])["ok"] is True
    assert apps.delete_custom_app("nope")["ok"] is False
    assert apps.list_custom_apps() == []


def test_custom_launch_uses_argv_vector_not_shell(sdir, no_backends, monkeypatch):
    """#63 acceptance: user input can never reach a shell invocation."""
    seen = {}

    def fake_launch(cmd):
        seen["cmd"] = cmd
        return {"ok": True}

    monkeypatch.setattr(apps, "launch_process", fake_launch)
    monkeypatch.setattr(state, "load_settings", lambda: {"geo_remember": False})
    entry = {"id": "c1", "name": "Evil", "label": "Evil", "cmd": ["evil-binary", "arg; rm -rf /"], "source": "custom"}
    apps._launch_entry(entry)
    assert seen["cmd"] == ["evil-binary", "arg; rm -rf /"]  # passed through as argv, unmodified


# ----------------------------------------------------------------- #65


def test_flatpak_parsing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/flatpak" if n == "flatpak" else None)
    monkeypatch.setattr(
        "remote_desktop.procs._run",
        lambda cmd, timeout=6, env=None: (
            (0, "org.mozilla.firefox\tFirefox\tfirefox\norg.videolan.VLC\tVLC\tvlc\n", "") if cmd[0] == "flatpak" else (1, "", "")
        ),
    )
    got = apps.list_flatpak_apps()
    assert [a["id"] for a in got] == ["org.mozilla.firefox", "org.videolan.VLC"]
    assert all(a["source"] == "flatpak" for a in got)
    assert got[0]["cmd"] == "flatpak run org.mozilla.firefox"
    assert got[0]["wmclass"] == "firefox"  # last component


def test_snap_parsing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/snap" if n == "snap" else None)
    out = "Name    Version    Rev\nspotify 1.2.3  99\ncore22  1.0    1\nsnapd   2.0    5\n"
    monkeypatch.setattr(
        "remote_desktop.procs._run", lambda cmd, timeout=6, env=None: (0, out, "") if cmd[0] == "snap" else (1, "", "")
    )
    got = apps.list_snap_apps()
    assert [a["id"] for a in got] == ["spotify"]  # core/snapd runtime snaps skipped
    assert got[0]["source"] == "snap" and got[0]["cmd"] == "snap run spotify"


def test_missing_backends_skip_silently(no_backends):
    assert apps.list_flatpak_apps() == []
    assert apps.list_snap_apps() == []


def test_dedupe_against_desktop(sdir, no_backends):
    desktop = [{"id": "firefox.desktop", "name": "Firefox", "desc": "", "cmd": "firefox", "wmclass": "firefox",
                "terminal": False, "categories": "", "source": "desktop"}]
    flatpak = [
        {"id": "org.mozilla.firefox", "name": "Firefox", "wmclass": "firefox", "source": "flatpak"},  # dup by name+class
        {"id": "org.videolan.VLC", "name": "VLC", "wmclass": "vlc", "source": "flatpak"},  # kept
    ]
    merged = apps._dedupe({"desktop": desktop, "flatpak": flatpak, "snap": [], "custom": []})
    assert [a["source"] for a in merged] == ["desktop", "flatpak"]
    assert [a["name"] for a in merged] == ["Firefox", "VLC"]


# ----------------------------------------------------------------- #73


def test_wmclass_candidates():
    flatpak = {"id": "org.mozilla.firefox", "name": "Firefox", "wmclass": "", "source": "flatpak"}
    cands = apps._wmclass_candidates(flatpak)
    assert "org.mozilla.firefox" in cands and "firefox" in cands and "Firefox" in cands
    snap = {"id": "spotify", "name": "Spotify", "wmclass": "", "source": "snap"}
    assert "Spotify" in apps._wmclass_candidates(snap)
    # explicit override always first
    custom = {"id": "c1", "name": "X", "wmclass": "Bind", "source": "custom"}
    assert apps._wmclass_candidates(custom)[0] == "Bind"


class FakeX:
    """Scripted xdotool/xprop responder over a window table."""

    def __init__(self, wins):
        self.wins = wins

    def __call__(self, cmd, timeout=6, env=None):
        import re as _re

        if cmd[0] == "xdotool":
            if cmd[1] == "search":
                args, only = cmd[2:], False
                if args[0] == "--onlyvisible":
                    only, args = True, args[1:]
                kind, pat = args[0], args[1]
                field = {"--classname": "inst", "--class": "cls", "--name": "name"}[kind]
                hit = [wid for wid, w in self.wins.items() if (not only or w.get("visible", True))
                       and _re.search(pat, w[field])]
                return 0, "\n".join(hit), ""
            if cmd[1] == "getwindowgeometry":
                w = self.wins[cmd[-1]]
                return 0, f"X={w['x']}\nY={w['y']}\nWIDTH={w['w']}\nHEIGHT={w['h']}\n", ""
            if cmd[1] == "getwindowname":
                return 0, self.wins[cmd[-1]]["name"], ""
            if cmd[1] == "getdisplaygeometry":
                return 0, "1280 800", ""
        if cmd[0] == "xprop":
            w = self.wins[cmd[2]]
            return 0, f'WM_CLASS(STRING) = "{w["inst"]}", "{w["cls"]}"', ""
        return 0, "", ""


def test_find_app_window_flatpak_id(monkeypatch):
    """#73: a Flatpak app whose WM_CLASS equals the flatpak id is focused,
    not launched twice."""
    monkeypatch.setattr(
        "remote_desktop.procs._run",
        FakeX({"9001": dict(inst="org.mozilla.firefox", cls="org.mozilla.firefox", name="Firefox",
                            x=0, y=0, w=800, h=600)}),
    )
    a = {"id": "org.mozilla.firefox", "name": "Firefox", "wmclass": "firefox", "source": "flatpak", "cmd": "flatpak run org.mozilla.firefox"}
    assert apps.find_app_window(a) == "9001"


def test_find_app_window_snap_capitalised(monkeypatch):
    monkeypatch.setattr(
        "remote_desktop.procs._run",
        FakeX({"9002": dict(inst="Vlc", cls="Vlc", name="VLC media player", x=0, y=0, w=800, h=600)}),
    )
    a = {"id": "vlc", "name": "VLC", "wmclass": "", "source": "snap", "cmd": "snap run vlc"}
    assert apps.find_app_window(a) == "9002"


def test_find_app_window_title_fallback(monkeypatch):
    """#73: Wayland-native windows often expose only a title."""
    monkeypatch.setattr(
        "remote_desktop.procs._run",
        FakeX({"9003": dict(inst="", cls="", name="Some App — Notes", x=0, y=0, w=800, h=600)}),
    )
    a = {"id": "some.desktop", "name": "Some App", "wmclass": "nothing-matches", "source": "desktop", "cmd": "someapp"}
    assert apps.find_app_window(a) == "9003"


def test_find_app_window_no_match(monkeypatch):
    monkeypatch.setattr("remote_desktop.procs._run", FakeX({}))
    a = {"id": "x.desktop", "name": "Xyzzy", "wmclass": "xyzzy", "source": "desktop", "cmd": "xyzzy"}
    assert apps.find_app_window(a) is None


def test_process_match_guard(monkeypatch):
    """#73: a running packaged app without a window must not be relaunched;
    malformed exe names never reach pgrep."""
    monkeypatch.setattr(
        "remote_desktop.procs._run",
        lambda cmd, timeout=6, env=None: (0, "1234\n", "") if cmd[:2] == ["pgrep", "-f"] else (1, "", ""),
    )
    assert apps.app_is_running({"cmd": "flatpak run org.videolan.VLC"}) is True
    assert apps.app_is_running({"cmd": "flatpak run org.videolan.VLC;"}) is False  # pattern sanitised
    assert apps.app_is_running({"cmd": ""}) is False


def test_flatpak_running_not_duplicated(monkeypatch, sdir, no_backends):
    monkeypatch.setattr(apps, "find_app_window", lambda a: None)
    monkeypatch.setattr(apps, "app_is_running", lambda a: True)
    started = []
    monkeypatch.setattr(apps, "launch_process", lambda cmd: started.append(cmd) or {"ok": True})
    a = {"id": "org.x.Y", "name": "Y", "wmclass": "y", "source": "flatpak", "cmd": "flatpak run org.x.Y"}
    monkeypatch.setattr(apps, "all_apps", lambda: [a])
    r = apps.launch_desktop("org.x.Y")
    assert r == {"ok": True, "focused": False, "running": True}
    assert started == []  # no duplicate launch


# ----------------------------------------------------------------- #75


def test_launch_hint_display_failure():
    assert "display" in apps._launch_hint(1, "Cannot open display :1", ["app"])


def test_launch_hint_not_installed():
    assert "Store" in apps._launch_hint(127, "sh: 1: foo: command not found", ["foo"])


def test_launch_hint_generic():
    r = apps._launch_hint(3, "segfault somewhere", ["app"])
    assert "exit code 3" in r


def test_launch_process_missing_binary():
    r = apps.launch_process(["definitely-not-a-real-binary-xyz"])
    assert r["ok"] is False and r["error"] == "missing binary"
    assert "try the Store" in r["hint"]


def test_launch_process_immediate_exit():
    r = apps.launch_process(["true"])  # exits at once with rc 0
    assert r["ok"] is False and "exited immediately" in r["hint"]


def test_launch_process_slow_gui_is_ok(monkeypatch):
    monkeypatch.setattr(apps, "LAUNCH_PROBE_SECONDS", 0.2)
    r = apps.launch_process(["sleep", "5"])
    assert r == {"ok": True}


def test_launch_entry_records_and_hints(sdir, no_backends, monkeypatch):
    monkeypatch.setattr(apps, "LAUNCH_PROBE_SECONDS", 0.05)
    monkeypatch.setattr(apps, "_after_launch", lambda a: None)
    r = apps._launch_entry({"id": "ok-app.desktop", "name": "Ok", "cmd": "sleep 5", "source": "desktop"})
    assert r == {"ok": True}
    assert state.load_launches()[0]["id"] == "ok-app.desktop"
    r = apps._launch_entry({"id": "bad-app.desktop", "name": "Bad", "cmd": "definitely-not-real-xyz", "source": "desktop"})
    assert r["ok"] is False and r["hint"] and r["cmd"]  # structured failure (#75)
    assert state.load_launches()[0]["id"] == "ok-app.desktop"  # failures not recorded


def test_launch_desktop_unknown_id(sdir, no_backends, monkeypatch):
    monkeypatch.setattr(apps, "all_apps", lambda: [])
    r = apps.launch_desktop("totally-unknown")
    assert r["ok"] is False  # falls through to self_launch_name -> no executable


# ----------------------------------------------------------------- #62


def test_rank_pinned_recent_name(sdir, monkeypatch):
    monkeypatch.setattr(state, "load_pins", lambda: ["c.desktop"])
    monkeypatch.setattr(state, "recent_launches", lambda: {"b.desktop": 1000, "a.desktop": 2000})
    apps_list = [{"id": n, "name": n} for n in ("a.desktop", "b.desktop", "c.desktop", "d.desktop")]
    assert [a["id"] for a in apps._rank(apps_list)] == ["c.desktop", "a.desktop", "b.desktop", "d.desktop"]


def test_record_launch_cap_and_dedupe(sdir, monkeypatch):
    monkeypatch.setattr(state, "LAUNCH_HISTORY_MAX", 3)
    for i in range(5):
        state.record_launch(f"app{i}.desktop")
    state.record_launch("app4.desktop")  # dedupe: bumps, no duplicate
    hist = state.load_launches()
    assert len(hist) == 3 and hist[0]["id"] == "app4.desktop"
    state.clear_launches()
    assert state.load_launches() == []


# ----------------------------------------------------------------- #77


def test_apps_response_etag_stable(sdir, no_backends, monkeypatch):
    monkeypatch.setattr(state, "load_pins", lambda: [])
    monkeypatch.setattr(state, "recent_launches", lambda: {})
    b1, e1 = apps.apps_response()
    b2, e2 = apps.apps_response()
    assert e1 == e2 and b1 is b2  # warm path served from the cache
    j = json.loads(b2)
    assert isinstance(j["apps"], list)


def test_apps_response_etag_changes(sdir, no_backends, monkeypatch):
    b1, e1 = apps.apps_response()
    apps.add_custom_app({"label": "New App", "cmd": ["newapp"]})
    apps._apps_cache.update({"fp": None})  # fingerprint file mtime may be same-second
    b2, e2 = apps.apps_response()
    assert e1 != e2
    assert any(a.get("source") == "custom" for a in json.loads(b2)["apps"])
