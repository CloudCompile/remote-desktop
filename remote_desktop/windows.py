"""Windows: xdotool-based window management and the xrandr resize fallback.

Ownership: window listing, fit-to-display, focus/restore helpers, the
display resize fallback, window thumbnails via ffmpeg x11grab (#59),
EWMH/xdotool window actions (#60), layout slots (#61) and per-app
geometry remembering (#76). resize_display reports xrandr failures
instead of pretending success (#31); reported geometry also feeds
/api/stats.
"""

import json
import os
import re
import shutil
import threading
import time

from . import config, logging as log, procs, state

FIT_CLASSES = "discord|Discord|chrome|Google-chrome|firefox|Navigator|xterm|nautilus|Nautilus|gedit|vlc|gimp|evince"

THUMB_MAX_W = 320  # #59: cap thumbnail width; height scales proportionally
THUMB_TIMEOUT = 5  # seconds; a slow host must never stall the desktop
GEOMETRY_MAX_ENTRIES = 200  # #76: capped wmclass -> geometry map
PROTECTED_IDS = set()  # extra ids actions must never touch (tests may extend)


def resize_display(w, h):
    """Best-effort xrandr fallback (VNC SetDesktopSize is preferred)."""
    rc, _, err = procs._run(["xrandr", "--size", f"{w}x{h}"])
    ok = rc == 0
    if not ok:
        log.log(f"xrandr --size {w}x{h} failed: {err[:120]}")
    return {"ok": ok, "error": None if ok else err[:200]}


def _xdo(*args):
    _, out, _ = procs._run(["xdotool", *args])
    return out.split()


def fit_windows():
    """Move/resize the main app windows to fill the display (no WM keeps them in view)."""
    g = _xdo("getdisplaygeometry")
    if len(g) != 2:
        return 0
    W, H = g
    n = 0
    for wid in _xdo("search", "--onlyvisible", "--class", FIT_CLASSES):
        _, geo, _ = procs._run(["xdotool", "getwindowgeometry", "--shell", wid])
        d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
        try:
            if int(d.get("WIDTH", 0)) < 200 or int(d.get("HEIGHT", 0)) < 150:
                continue  # skip helper/tray windows
        except ValueError:
            continue
        procs._run(["xdotool", "windowmove", wid, "0", "0", "windowsize", wid, W, H])
        n += 1
    return n


def fit_after_launch(tries=30):
    """Background thread: wait for a freshly launched app, then fit it."""
    for _ in range(tries):
        time.sleep(1)
        try:
            if fit_windows():
                time.sleep(2)
                fit_windows()  # apps often reposition themselves once loaded
                return
        except Exception:
            pass


def _window_geometry(wid):
    _, geo, _ = procs._run(["xdotool", "getwindowgeometry", "--shell", wid])
    d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
    try:
        return int(d.get("WIDTH", 0)), int(d.get("HEIGHT", 0))
    except ValueError:
        return 0, 0


def _big(wid):
    w, h = _window_geometry(wid)
    return w >= 200 and h >= 150


def _win_area(wid):
    w, h = _window_geometry(wid)
    return w * h


def _win_title(wid):
    _, out, _ = procs._run(["xdotool", "getwindowname", wid])
    return out.strip()


def _is_chrome_browser(wid):
    # Chrome's normal browser windows end with " - Google Chrome"; --app
    # windows do not (their titles are just the page/app name).
    return _win_title(wid).endswith(" - Google Chrome")


def _activate_window(win):
    """Raise and focus a window, unmapping it first if needed.

    Apps that hide to the tray (Legcord/Discord, many messengers) keep their
    main window unmapped; `windowactivate` alone does nothing for those.
    `windowmap` is harmless on already-mapped windows.
    """
    procs._run(["xdotool", "windowmap", win])
    rc, _, _ = procs._run(["xdotool", "windowactivate", "--sync", win])
    procs._run(["xdotool", "windowraise", win])
    if rc == 0:
        remember_geometry(win)  # #76: persist geometry on every focus change
    return rc == 0


def _pick_window(wids, chrome=False):
    """Pick the real top-level window from xdotool matches.

    Chrome creates several tiny helper windows that share WM_CLASS with the
    browser, so taking the first match focuses something invisible. Prefer
    big windows and, for Chrome, require a browser (not --app) title.
    """
    wids = [w for w in wids if _big(w)]
    if chrome:
        wids = [w for w in wids if _is_chrome_browser(w)]
    if not wids:
        return None
    return max(wids, key=_win_area)


def focus_or_launch(cls, launch):
    """Bring an app to the front (even if minimized); launch it if not running."""
    W, H = (_xdo("getdisplaygeometry") + ["0", "0"])[:2]
    chrome = "chrome" in cls.lower()
    win = _pick_window(_xdo("search", "--class", cls), chrome=chrome)
    if win:
        _activate_window(win)
        _xdo("windowmove", win, "0", "0")
        _xdo("windowsize", win, W, H)
    time.sleep(0.6)
    if not win:
        procs.run_detached(launch)
        threading.Thread(target=fit_after_launch, daemon=True).start()
    return {"ok": True, "shown": bool(win)}


def list_windows():
    vis = set(_xdo("search", "--onlyvisible", "--name", ""))
    out = []
    for wid in _xdo("search", "--name", ""):
        _, geo, _ = procs._run(["xdotool", "getwindowgeometry", "--shell", wid])
        d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
        try:
            if int(d.get("WIDTH", 0)) < 100:
                continue
        except ValueError:
            continue
        _, cls, _ = procs._run(["xprop", "-id", wid, "WM_CLASS"])
        name = " ".join(_xdo("getwindowname", wid))
        out.append(
            {
                "id": wid,
                "visible": wid in vis,
                "w": d.get("WIDTH"),
                "h": d.get("HEIGHT"),
                "x": d.get("X", "0"),  # #59: switcher thumbnails grab at X,Y
                "y": d.get("Y", "0"),
                "class": cls.strip()[:80],
                "name": name[:60],
            }
        )
    out.sort(key=lambda w: (not w["visible"], w["name"].lower()))
    return out


def display_geometry():
    """Live display size via xdotool, e.g. {"width":1280,"height":800};
    falls back to the configured RD_GEOMETRY when xdotool is unavailable."""
    g = _xdo("getdisplaygeometry")
    if len(g) == 2:
        try:
            return {"width": int(g[0]), "height": int(g[1]), "source": "xrandr"}
        except ValueError:
            pass
    w, h = config.geometry_wh()
    if w:
        return {"width": w, "height": h, "source": "config"}
    return None


# ---------------------------------------------------------------- thumbnail
# #59: a single-frame JPEG of one window, captured by ffmpeg's x11grab.
# Strictly bounded (5 s timeout, capped size) and never raises — the UI
# degrades to the app icon when capture fails or ffmpeg is missing.


def _window_geom_xywh(hid):
    """(x, y, w, h) of a window, or None. Never raises."""
    _, geo, _ = procs._run(["xdotool", "getwindowgeometry", "--shell", str(hid)], timeout=3)
    d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
    try:
        return int(d.get("X", 0)), int(d.get("Y", 0)), int(d.get("WIDTH", 0)), int(d.get("HEIGHT", 0))
    except ValueError:
        return None


def window_thumbnail(hid):
    """Single-frame JPEG bytes of one window (#59). Returns (bytes, None) or
    (None, error). Never raises; bounded by THUMB_TIMEOUT."""
    try:
        hid = str(hid or "").strip()
        if not re.fullmatch(r"\d{1,16}", hid):
            return None, "invalid window id"
        if shutil.which("ffmpeg") is None:
            return None, "ffmpeg not installed"
        rc, _, _ = procs._run(["xdotool", "getwindowname", hid], timeout=3)
        if rc != 0:
            return None, "no such window"
        g = _window_geom_xywh(hid)
        if not g:
            return None, "could not read window geometry"
        x, y, w, h = g
        w, h = max(2, w), max(2, h)
        if w * h > 64_000_000:  # absurd geometry: refuse rather than grab
            return None, "window geometry out of range"
        disp = display_geometry() or {}
        dw, dh = int(disp.get("width", 0)), int(disp.get("height", 0))
        if dw and dh:  # clamp to the visible screen, x11grab refuses the rest
            x, y = max(0, min(x, dw - 2)), max(0, min(y, dh - 2))
            w, h = min(w, dw - x), min(h, dh - y)
        w -= w % 2  # x11grab wants even sizes
        h -= h % 2
        if w < 2 or h < 2:
            return None, "window too small to capture"
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-f", "x11grab", "-video_size", f"{w}x{h}", "-i", f"{config.ENV['DISPLAY']}+{x},{y}",
            "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "5",
            "-f", "image2", "-",
        ]
        if w > THUMB_MAX_W:  # cap width, keep aspect
            cmd[9:9] = ["-vf", f"scale={THUMB_MAX_W}:-2"]
        rc, out, err = procs._run(cmd, timeout=THUMB_TIMEOUT)
        if rc != 0 or not out:
            return None, (err or "thumbnail capture failed")[:120]
        return out, None
    except Exception as e:
        return None, str(e)[:120]


# ------------------------------------------------------------ WM awareness
# #60: honest WM capability reporting — actions report what failed instead
# of pretending success.


def wm_name():
    """The running window manager's name ("" when undetectable)."""
    if shutil.which("wmctrl"):
        rc, out, _ = procs._run(["wmctrl", "-m"], timeout=3)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0][:60]
    rc, out, _ = procs._run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], timeout=3)
    m = re.search(r"0x[0-9a-fA-F]+", out or "")
    if m:
        rc, out, _ = procs._run(["xprop", "-id", m.group(0), "_NET_WM_NAME"], timeout=3)
        m2 = re.search(r'"(.*)"', out or "")
        if rc == 0 and m2:
            return m2.group(1)[:60]
    return ""


def _ewmh_check_wid():
    rc, out, _ = procs._run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], timeout=3)
    m = re.search(r"0x[0-9a-fA-F]+", out or "")
    return int(m.group(0), 16) if m else None


def _is_actionable(hid):
    """Guard (#60): only real client windows may be acted on. The root
    window is never an xdotool match, unmanaged windows have no WM_CLASS,
    and the WM's own supporting-check window is off limits — so closing a
    window can never take down the desktop session."""
    if int(hid) in PROTECTED_IDS:
        return False, "protected window"
    rc, _, _ = procs._run(["xdotool", "getwindowname", str(hid)], timeout=3)
    if rc != 0:
        return False, "no such window"
    chk = _ewmh_check_wid()
    if chk is not None and chk == int(hid):
        return False, "protected window"
    _, cls, _ = procs._run(["xprop", "-id", str(hid), "WM_CLASS"], timeout=3)
    m = re.search(r'=\s*"([^"]*)",\s*"([^"]*)"', cls or "")
    if not m or not (m.group(1) or m.group(2)):
        return False, "not a managed application window"
    return True, None


def _win_close(hid):
    """Close politely via WM_DELETE when wmctrl is available, else xdotool
    windowkill (XKillClient) (#60)."""
    if shutil.which("wmctrl"):
        rc, _, err = procs._run(["wmctrl", "-ic", str(hid)], timeout=3)
        if rc == 0:
            return {"ok": True}
        return {"ok": False, "error": f"wmctrl close failed: {(err or 'unknown')[:120]}"}
    rc, _, err = procs._run(["xdotool", "windowkill", str(hid)], timeout=3)
    if rc == 0:
        return {"ok": True}
    return {"ok": False, "error": f"windowkill failed: {(err or 'unknown')[:120]}"}


def _win_maximize(hid, on):
    """Maximize/restore via EWMH when wmctrl exists, else a move+size
    fallback sized to the display (#60). The reply says which ran."""
    prop = "add" if on else "remove"
    if shutil.which("wmctrl"):
        rc, _, err = procs._run(
            ["wmctrl", "-ir", str(hid), "-b", f"{prop},maximized_vert,maximized_horz"], timeout=3
        )
        if rc == 0:
            return {"ok": True, "via": "ewmh"}
        return {"ok": False, "error": f"wmctrl maximize failed: {(err or 'unknown')[:120]}"}
    disp = display_geometry()
    if not disp:
        return {"ok": False, "error": "no window manager supports maximize (wmctrl not installed)"}
    if on:
        procs._run(["xdotool", "windowmove", str(hid), "0", "0"], timeout=3)
        procs._run(["xdotool", "windowsize", str(hid), str(disp["width"]), str(disp["height"])], timeout=3)
    else:
        _activate_window(str(hid))
    return {"ok": True, "via": "fallback"}


# ------------------------------------------------------------- layout slots
# #61: half/third/quadrant/full presets computed from the live display.

LAYOUT_SLOTS = {
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 0.5),
    "tl": (0.0, 0.0, 0.5, 0.5),
    "tr": (0.5, 0.0, 0.5, 0.5),
    "bl": (0.0, 0.5, 0.5, 0.5),
    "br": (0.5, 0.5, 0.5, 0.5),
    "left-third": (0.0, 0.0, 1 / 3, 1.0),
    "center-third": (1 / 3, 0.0, 1 / 3, 1.0),
    "right-third": (2 / 3, 0.0, 1 / 3, 1.0),
    "full": (0.0, 0.0, 1.0, 1.0),
}


def window_layout(hid, slot):
    """Place a window into a named slot (#61). Clamped to the display;
    the last layout per display size is remembered in geometry.json."""
    f = LAYOUT_SLOTS.get(str(slot or ""))
    if not f:
        return {"ok": False, "error": f"unknown slot (one of: {', '.join(sorted(LAYOUT_SLOTS))})"}
    disp = display_geometry()
    if not disp:
        return {"ok": False, "error": "display size unavailable"}
    W, H = disp["width"], disp["height"]
    x = max(0, round(f[0] * W))
    y = max(0, round(f[1] * H))
    w = min(W - x, max(1, round(f[2] * W)))
    h = min(H - y, max(1, round(f[3] * H)))
    rc1, _, e1 = procs._run(["xdotool", "windowmove", str(hid), str(x), str(y)], timeout=3)
    rc2, _, e2 = procs._run(["xdotool", "windowsize", str(hid), str(w), str(h)], timeout=3)
    if rc1 != 0 or rc2 != 0:
        return {"ok": False, "error": f"window move/size failed: {((e1 or e2) or 'unknown')[:120]}"}
    _remember_layout(str(hid), slot, W, H)
    remember_geometry(str(hid))  # an explicit placement is a geometry worth keeping (#76)
    return {"ok": True, "slot": slot, "x": x, "y": y, "w": w, "h": h}


# ------------------------------------------------------- per-app geometry
# #76: remember each app's preferred window geometry keyed by WM_CLASS in
# STATE_DIR/geometry.json; restore after relaunch; toggleable, capped.


def geometry_file():
    return os.path.join(config.STATE_DIR, "geometry.json")


def _geo_load():
    try:
        with open(geometry_file()) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            raw.setdefault("enabled", True)
            raw.setdefault("apps", {})
            if not isinstance(raw["apps"], dict):
                raw["apps"] = {}
            return raw
    except Exception:
        pass
    return {"enabled": True, "apps": {}}


def _geo_save(data):
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        tmp = geometry_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, geometry_file())
        os.chmod(geometry_file(), 0o600)
    except Exception as e:
        log.log(f"geometry save failed: {e}")


def _wmclass_of(hid):
    _, out, _ = procs._run(["xprop", "-id", str(hid), "WM_CLASS"], timeout=3)
    m = re.search(r'=\s*"(?P<inst>[^"]*)",\s*"(?P<cls>[^"]*)"', out or "")
    if not m:
        return None
    return m.group("cls") or m.group("inst") or None


def remember_geometry(hid):
    """Snapshot one window's x/y/w/h under its WM_CLASS (#76). Skips when
    the toggle is off or the geometry is unchanged."""
    try:
        data = _geo_load()
        if not data.get("enabled", True):
            return False
        g = _window_geom_xywh(hid)
        if not g:
            return False
        x, y, w, h = g
        if w < 100 or h < 80:  # helper/tray windows are not real geometry
            return False
        cls = _wmclass_of(hid)
        if not cls:
            return False
        apps = data["apps"]
        prev = apps.get(cls)
        if prev and (prev.get("x"), prev.get("y"), prev.get("w"), prev.get("h")) == (x, y, w, h):
            prev["ts"] = int(time.time())
            return True
        apps[cls] = {"x": x, "y": y, "w": w, "h": h, "ts": int(time.time())}
        if len(apps) > GEOMETRY_MAX_ENTRIES:  # expire the stalest entries
            for k in sorted(apps, key=lambda k: apps[k].get("ts", 0))[: len(apps) - GEOMETRY_MAX_ENTRIES]:
                del apps[k]
        _geo_save(data)
        return True
    except Exception:
        return False


def restore_geometry(hid, cls):
    """Best-effort apply the saved geometry for a WM_CLASS to a window (#76)."""
    try:
        data = _geo_load()
        if not data.get("enabled", True):
            return False
        g = data["apps"].get(cls or "")
        if not g:
            return False
        disp = display_geometry() or {}
        W, H = disp.get("width", 0), disp.get("height", 0)
        x, y, w, h = g.get("x", 0), g.get("y", 0), g.get("w", 0), g.get("h", 0)
        if W and H:  # never place a window off-screen after a display change
            x, y = max(0, min(x, W - 40)), max(0, min(y, H - 40))
            w, h = min(w, W), min(h, H)
        procs._run(["xdotool", "windowmove", str(hid), str(x), str(y)], timeout=3)
        procs._run(["xdotool", "windowsize", str(hid), str(w), str(h)], timeout=3)
        return True
    except Exception:
        return False


def restore_after_launch(wmclass, tries=20):
    """Background: when a relaunched app's window appears, put it back at
    its remembered geometry instead of the default fit (#76)."""
    for _ in range(tries):
        time.sleep(1)
        try:
            wids = _xdo("search", "--onlyvisible", "--class", wmclass) or _xdo(
                "search", "--onlyvisible", "--classname", wmclass
            )
            win = _pick_window(wids) if wids else None
            if win and restore_geometry(win, wmclass):
                return True
        except Exception:
            pass
    return False


def geometry_settings(enabled=None, reset=False):
    """Toggle / reset / report geometry remembering (#76)."""
    data = _geo_load()
    if reset:
        data["apps"] = {}
        _geo_save(data)
    if isinstance(enabled, bool):
        data["enabled"] = enabled
        _geo_save(data)
    return {"enabled": bool(data.get("enabled", True)), "apps": len(data.get("apps", {}))}


# ------------------------------------------------- remembered layouts (#61)

_LAYOUTS_MAX = 8  # display sizes to remember


def _remember_layout(hid, slot, W, H):
    try:
        data = _geo_load()
        layouts = data.setdefault("layouts", {})
        layouts[f"{W}x{H}"] = {"hid": str(hid), "slot": slot, "ts": int(time.time())}
        if len(layouts) > _LAYOUTS_MAX:
            for k in sorted(layouts, key=lambda k: layouts[k].get("ts", 0))[: len(layouts) - _LAYOUTS_MAX]:
                del layouts[k]
        _geo_save(data)
    except Exception:
        pass


def last_layout(W, H):
    """The slot last assigned on this display size, if any (#61)."""
    data = _geo_load()
    entry = data.get("layouts", {}).get(f"{W}x{H}")
    return entry.get("slot") if entry else None


# ------------------------------------------------------------ window actions
# #60: one endpoint for every common WM action, with honest errors.

_ACTIONS = ("focus", "close", "minimize", "maximize", "restore", "move", "resize", "layout")


def window_action(hid, action, x=None, y=None, w=None, h=None, slot=None):
    """Apply one WM action to one window (#60, #61). Returns {"ok", "error",
    ...extras}. Never raises; unsupported WMs get actionable errors."""
    try:
        hid = str(hid or "").strip()
        if not re.fullmatch(r"\d{1,16}", hid):
            return {"ok": False, "error": "invalid window id"}
        if action not in _ACTIONS:
            return {"ok": False, "error": f"unknown action (one of: {', '.join(_ACTIONS)})"}
        ok, why = _is_actionable(hid)
        if not ok:
            return {"ok": False, "error": why}

        if action == "focus":
            if _activate_window(hid):
                return {"ok": True}
            return {"ok": False, "error": "could not activate the window (is a window manager running?)"}

        if action == "close":
            return _win_close(hid)

        if action == "minimize":
            rc, _, err = procs._run(["xdotool", "windowminimize", hid], timeout=3)
            if rc == 0:
                return {"ok": True}
            return {"ok": False, "error": f"minimize failed: {(err or 'unknown')[:120]}"}

        if action == "maximize":
            return _win_maximize(hid, True)

        if action == "restore":
            r = _win_maximize(hid, False)
            if r.get("ok"):
                _activate_window(hid)  # also unminimize + raise
            return r

        if action == "move":
            try:
                xx, yy = int(x), int(y)
            except (TypeError, ValueError):
                return {"ok": False, "error": "move needs integer x and y"}
            rc, _, err = procs._run(["xdotool", "windowmove", hid, str(xx), str(yy)], timeout=3)
            if rc == 0:
                remember_geometry(hid)
                return {"ok": True}
            return {"ok": False, "error": f"move failed: {(err or 'unknown')[:120]}"}

        if action == "resize":
            try:
                ww, hh = int(w), int(h)
            except (TypeError, ValueError):
                return {"ok": False, "error": "resize needs integer w and h"}
            ww, hh = max(1, ww), max(1, hh)
            rc, _, err = procs._run(["xdotool", "windowsize", hid, str(ww), str(hh)], timeout=3)
            if rc == 0:
                remember_geometry(hid)
                return {"ok": True}
            return {"ok": False, "error": f"resize failed: {(err or 'unknown')[:120]}"}

        if action == "layout":
            return window_layout(hid, slot)

        return {"ok": False, "error": "unknown action"}
    except Exception as e:
        log.log(f"window_action {action} failed: {e}")
        return {"ok": False, "error": str(e)[:160]}
