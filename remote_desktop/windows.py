"""Windows: xdotool-based window management and the xrandr resize fallback.

Ownership: window listing, fit-to-display, focus/restore helpers and the
display resize fallback. resize_display reports xrandr failures instead of
pretending success (#31); reported geometry also feeds /api/stats.
"""

import threading
import time

from . import config, log, procs, state

FIT_CLASSES = "discord|Discord|chrome|Google-chrome|firefox|Navigator|xterm|nautilus|Nautilus|gedit|vlc|gimp|evince"


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
                "class": cls.strip()[:80],
                "name": name[:60],
            }
        )
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
