#!/usr/bin/env python3
"""
Remote Desktop control server.

One process, stdlib only:
  - serves the web/ frontend (noVNC) as static files
  - JSON control API (window management, app launch, clipboard, audio)
  - low-latency raw PCM audio streaming from the PulseAudio monitor

Listens on 127.0.0.1:8090. Put an HTTPS reverse proxy (caddy/nginx) in front
for remote access, and websockify on /websockify for the VNC stream.
"""

import base64
import hashlib
import hmac
import http.cookies
import json
import mimetypes
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import crypt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ------------------------------------------------------------------ config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
LOG_FILE = "/tmp/remote-control.log"
HOME = os.path.expanduser("~")

ENV = {
    "DISPLAY": os.environ.get("RD_DISPLAY", ":1"),
    "XAUTHORITY": os.environ.get("XAUTHORITY", os.path.join(HOME, ".Xauthority")),
    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
}

AUTH_USER = os.environ.get("RD_USER", "admin")
PASS_FILE = os.environ.get("RD_PASSFILE", os.path.join(BASE_DIR, ".passhash"))
try:
    PASS_HASH = open(PASS_FILE).read().strip()
except Exception:
    PASS_HASH = ""  # logins disabled if the hash is missing
SECRET = hashlib.sha256((PASS_HASH + "rd-secret-v2").encode()).digest()

TOKEN_TTL = 7 * 24 * 3600  # 7 days
MAX_BODY = 1 << 20         # 1 MiB request bodies
LOGIN_ATTEMPTS = {}        # ip -> [timestamps]  (simple rate limit)

APP_CATALOG = [
    {"name": "chrome",   "label": "Chrome",     "pkg": "google-chrome-stable",
     "cmd": ["google-chrome", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
             "--autoplay-policy=no-user-gesture-required"], "desc": "Web browser"},
    {"name": "firefox",  "label": "Firefox",    "pkg": "firefox",           "cmd": ["firefox"], "desc": "Web browser"},
    {"name": "discord",  "label": "Discord",    "pkg": "discord",           "cmd": ["discord"], "desc": "Chat"},
    {"name": "terminal", "label": "Terminal",   "pkg": "xterm",             "cmd": ["xterm"],  "desc": "Xterm"},
    {"name": "files",    "label": "Files",      "pkg": "nautilus",          "cmd": ["nautilus"], "desc": "File manager"},
    {"name": "editor",   "label": "Text Editor","pkg": "gedit",             "cmd": ["gedit"],  "desc": "Simple text editor"},
    {"name": "vlc",      "label": "VLC",        "pkg": "vlc",               "cmd": ["vlc"],    "desc": "Media player"},
    {"name": "gimp",     "label": "GIMP",       "pkg": "gimp",              "cmd": ["gimp"],   "desc": "Image editor"},
    {"name": "evince",   "label": "Docs Viewer","pkg": "evince",            "cmd": ["evince"], "desc": "PDF viewer"},
]
# window classes that should be auto-fitted to the display
FIT_CLASSES = "discord|Discord|chrome|Google-chrome|firefox|Navigator|xterm|nautilus|Nautilus|gedit|vlc|gimp|evince"

AUDIO_LOG = "/tmp/remote_audio.log"


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ------------------------------------------------------------------ env / helpers

def _env():
    return {**os.environ, **ENV}


def _pa_env():
    e = _env()
    sock = os.path.join(ENV["XDG_RUNTIME_DIR"], "pulse", "native")
    if os.path.exists(sock):
        e["PULSE_SERVER"] = "unix:" + sock
    cookie = os.path.join(HOME, ".config", "pulse", "cookie")
    if os.path.exists(cookie):
        e["PULSE_COOKIE"] = cookie
    return e


def _run(cmd, timeout=6, env=None):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, env=env or _env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def run_detached(cmd):
    """Launch a GUI app detached from the request handler."""
    try:
        subprocess.Popen(cmd, env=_pa_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ audio

def _pa(*args):
    rc, out, _ = _run(["pactl", *args], env=_pa_env())
    return out.strip() if rc == 0 else ""


def _die_with_parent():
    """preexec_fn: kill this child if the server process dies (no orphaned ffmpeg)."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGHUP)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def audio_source():
    """PulseAudio monitor of the default sink = what apps are playing (not the mic)."""
    if not _pa("list", "short", "sinks"):
        _pa("load-module", "module-null-sink", "sink_name=remote_out",
            "sink_properties=device.description=RemoteOut")
        _pa("set-default-sink", "remote_out")
        for line in _pa("list", "short", "sink-inputs").splitlines():
            _pa("move-sink-input", line.split()[0], "remote_out")
    sink = _pa("get-default-sink") or "remote_out"
    return sink + ".monitor"


def audio_status():
    log_tail = ""
    try:
        with open(AUDIO_LOG) as f:
            log_tail = "".join(f.readlines()[-12:])
    except Exception:
        pass
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "pactl": bool(shutil.which("pactl")),
            "pulse_server": _pa_env().get("PULSE_SERVER"), "info": _pa("info")[:400],
            "sinks": _pa("list", "short", "sinks"), "sources": _pa("list", "short", "sources"),
            "chosen_source": audio_source(), "ffmpeg_log": log_tail}


# ------------------------------------------------------------------ display / windows

def resize_display(w, h):
    """Best-effort xrandr fallback (VNC SetDesktopSize is preferred)."""
    rc, _, err = _run(["xrandr", "--size", f"{w}x{h}"])
    return {"ok": rc == 0, "error": None if rc == 0 else err[:200]}


def _xdo(*args):
    _, out, _ = _run(["xdotool", *args])
    return out.split()


def fit_windows():
    """Move/resize the main app windows to fill the display (no WM keeps them in view)."""
    g = _xdo("getdisplaygeometry")
    if len(g) != 2:
        return 0
    W, H = g
    n = 0
    for wid in _xdo("search", "--onlyvisible", "--class", FIT_CLASSES):
        _, geo, _ = _run(["xdotool", "getwindowgeometry", "--shell", wid])
        d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
        try:
            if int(d.get("WIDTH", 0)) < 200 or int(d.get("HEIGHT", 0)) < 150:
                continue  # skip helper/tray windows
        except ValueError:
            continue
        _run(["xdotool", "windowmove", wid, "0", "0", "windowsize", wid, W, H])
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


def _big(wid):
    _, geo, _ = _run(["xdotool", "getwindowgeometry", "--shell", wid])
    d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
    try:
        return int(d.get("WIDTH", 0)) >= 200 and int(d.get("HEIGHT", 0)) >= 150
    except ValueError:
        return False


def _win_area(wid):
    _, geo, _ = _run(["xdotool", "getwindowgeometry", "--shell", wid])
    d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
    try:
        return int(d.get("WIDTH", 0)) * int(d.get("HEIGHT", 0))
    except ValueError:
        return 0


def _win_title(wid):
    _, out, _ = _run(["xdotool", "getwindowname", wid])
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
    _run(["xdotool", "windowmap", win])
    rc, _, _ = _run(["xdotool", "windowactivate", "--sync", win])
    _run(["xdotool", "windowraise", win])
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
        run_detached(launch)
        threading.Thread(target=fit_after_launch, daemon=True).start()
    return {"ok": True, "shown": bool(win)}


def list_windows():
    vis = set(_xdo("search", "--onlyvisible", "--name", ""))
    out = []
    for wid in _xdo("search", "--name", ""):
        _, geo, _ = _run(["xdotool", "getwindowgeometry", "--shell", wid])
        d = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
        try:
            if int(d.get("WIDTH", 0)) < 100:
                continue
        except ValueError:
            continue
        _, cls, _ = _run(["xprop", "-id", wid, "WM_CLASS"])
        name = " ".join(_xdo("getwindowname", wid))
        out.append({"id": wid, "visible": wid in vis, "w": d.get("WIDTH"), "h": d.get("HEIGHT"),
                    "class": cls.strip()[:80], "name": name[:60]})
    return out


# ------------------------------------------------------------------ app discovery
#
# Instead of a hardcoded catalog, enumerate every installed application from
# its .desktop file (what a Linux app launcher shows). Fallback CLI entries
# are appended if their desktop file is missing.

APP_DIRS = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")]

FALLBACK_APPS = [
    {"id": "xterm", "name": "Terminal (xterm)", "desc": "Xterm", "cmd": "xterm", "terminal": False},
    {"id": "nautilus", "name": "Files", "desc": "Nautilus file manager", "cmd": "nautilus", "terminal": False},
]


def _parse_desktop(path):
    """Minimal .desktop parser: first plain value per key wins (locale keys ignored)."""
    data = {}
    in_entry = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if in_entry and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.split("[", 1)[0].strip()      # Name[de] -> Name
                    if k not in data:
                        data[k] = v.strip()
    except Exception:
        return None
    return data


def list_desktop_apps():
    apps, seen = [], set()
    for d in APP_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in files:
            if not fn.endswith(".desktop") or fn in seen:
                continue
            data = _parse_desktop(os.path.join(d, fn))
            if not data:
                continue
            if data.get("NoDisplay", "false").lower() in ("true", "1"):
                continue
            if data.get("Hidden", "false").lower() in ("true", "1"):
                continue
            exec_str = data.get("Exec", "").strip()
            if not exec_str:
                continue
            # strip desktop-file field codes (%f %u %i %c ...) and their args
            cmd = re.sub(r"\s*%[a-zA-Z]", "", exec_str).strip()
            base_id = fn[:-8]
            # WM_CLASS hint so launches can focus an existing window instead of
            # starting a duplicate (StartupWMClass, else our --class=, else
            # desktop id, else Exec basename)
            wmclass = data.get("StartupWMClass", "").strip()
            m = re.search(r"--class=([^\s]+)", cmd)
            if m:
                wmclass = wmclass or m.group(1)
            exe_name = cmd.split()[0] if cmd else ""
            apps.append({
                "id": fn,
                "name": data.get("Name") or base_id,
                "desc": data.get("Comment") or data.get("GenericName") or "",
                "cmd": cmd,
                "terminal": data.get("Terminal", "false").lower() in ("true", "1"),
                "categories": data.get("Categories", ""),
                "wmclass": wmclass or base_id or os.path.basename(exe_name),
            })
            seen.add(fn)
    for fb in FALLBACK_APPS:
        if not any(fb["cmd"] in a["cmd"] for a in apps):
            apps.append(dict(fb))
    apps.sort(key=lambda a: a["name"].lower())
    return apps


def _webapp_host(a):
    m = re.search(r"--app=\w+://([^/\s]+)", a.get("cmd") or "")
    return re.sub(r"^www\.", "", m.group(1)) if m else None


def _shared_webapp_hosts():
    """Hosts used by more than one web-app entry (e.g. docs.google.com is
    shared by Docs, Sheets and Slides)."""
    hosts = {}
    for a in list_desktop_apps():
        h = _webapp_host(a)
        if h:
            hosts[h] = hosts.get(h, 0) + 1
    return {h for h, n in hosts.items() if n > 1}


def find_app_window(a):
    """Find an existing window belonging to app entry a. Returns window id or None."""
    cmd = a.get("cmd") or ""
    is_web = cmd.startswith("google-chrome --app")
    is_chrome = not is_web and "chrome" in (((a.get("wmclass") or "") + cmd).lower())
    cands = []                                   # (xdotool match kind, pattern)
    # chrome names app-mode windows' WM_CLASS instance after the URL HOST and
    # ignores --class when joining an already-running browser — so hosts
    # shared by several webapps (docs.google.com) can't be told apart that
    # way; matching one of them would focus e.g. the Docs window when the
    # user asked for Sheets. For those, match the window-title suffix
    # ("{doc name} - Google Sheets") instead.
    if is_web:
        host = _webapp_host(a)
        if host and host not in _shared_webapp_hosts():
            cands.append(("classname", host))
        # web app titles end with " - {App name}", they don't start with it
        cands.append(("name", re.escape(" - " + a["name"]) + "$"))
    elif a.get("wmclass"):
        # Prefer the WM_CLASS *instance* (xdotool --classname): Chrome's normal
        # browser window is ("google-chrome", "Google-chrome"), but every
        # --app window shares the class "Google-chrome" with instance set to
        # the site host ("www.reddit.com", "x.com", ...). Matching on the
        # class alone would focus whichever app window came first (Reddit,
        # Instagram, ...) instead of the Chrome window the user clicked.
        cands.append(("classname", re.escape(a["wmclass"])))
        cands.append(("class", a["wmclass"]))
    # visible windows first
    for kind, pat in cands:
        rc, out, _ = _run(["xdotool", "search", "--onlyvisible", "--" + kind, pat])
        if rc == 0 and out.strip():
            win = _pick_window(out.split(), chrome=is_chrome)
            if win:
                return win
    # fallback: apps whose titles do start with their name; terminal apps
    # get an exact -title
    if is_web or a.get("terminal"):
        rc, out, _ = _run(["xdotool", "search", "--onlyvisible", "--name",
                           "^" + re.escape(a["name"])])
        if rc == 0 and out.strip():
            win = _pick_window(out.split(), chrome=is_chrome)
            if win:
                return win
    # minimized/iconic windows are not "visible" — try the full list and restore
    for kind, pat in cands:
        rc, out, _ = _run(["xdotool", "search", "--" + kind, pat])
        if rc == 0 and out.strip():
            win = _pick_window(out.split(), chrome=is_chrome)
            if win:
                _activate_window(win)
                return win
    return None


def launch_desktop(app_id):
    """Launch an app by .desktop id (as returned by /api/apps).
    If the app already has a window, focus/restore it instead of launching a
    duplicate."""
    for a in list_desktop_apps():
        if a["id"] == app_id:
            win = find_app_window(a)
            if win:
                _activate_window(win)
                return {"ok": True, "focused": True}
            if a["terminal"]:
                run_detached(["xterm", "-title", a["name"], "-e", "sh", "-c", a["cmd"]])
            else:
                run_detached(["sh", "-c", a["cmd"]])
            threading.Thread(target=fit_after_launch, daemon=True).start()
            return {"ok": True}
    # also accept catalog names / bare commands for compatibility
    return self_launch_name(app_id)


def self_launch_name(name):
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", name):
        return {"ok": False, "error": "invalid name"}
    app = next((a for a in APP_CATALOG if a["name"] == name), None)
    if app:
        if not is_installed(app["pkg"]):
            return {"ok": False, "error": "not installed"}
        run_detached(app["cmd"])
        threading.Thread(target=fit_after_launch, daemon=True).start()
        return {"ok": True}
    exe = shutil.which(name, path=_env().get("PATH"))
    if not exe:
        return {"ok": False, "error": "no executable"}
    run_detached([exe])
    return {"ok": True}


# ------------------------------------------------------------------ app store

_INSTALL_CACHE = {}
_INSTALL_CACHE_TTL = 30  # seconds; dpkg -s on every request made the store sluggish


def is_installed(pkg):
    now = time.time()
    hit = _INSTALL_CACHE.get(pkg)
    if hit and now - hit[0] < _INSTALL_CACHE_TTL:
        return hit[1]
    rc, _, _ = _run(["dpkg", "-s", pkg], timeout=4)
    val = rc == 0
    if not val:
        val = shutil.which(pkg, path=_env().get("PATH")) is not None
    _INSTALL_CACHE[pkg] = (now, val)
    return val


def store_search(q):
    rows = []
    rc, out, _ = _run(["appstreamcli", "search", q], timeout=30)
    if rc == 0:
        cur = {}
        blocks = []
        for line in out.splitlines():
            if line.strip() == "---":
                blocks.append(cur); cur = {}
            elif ":" in line:
                k, v = line.split(":", 1)
                cur[k.strip()] = v.strip()
        blocks.append(cur)
        for cur in blocks:
            if not cur.get("Package"):
                continue
            label = cur.get("Name", cur["Package"])
            desc = cur.get("Summary", "")
            if q.lower() in (label + " " + desc + " " + cur["Package"]).lower():
                rows.append({"name": cur["Package"], "label": label, "desc": desc,
                             "installed": is_installed(cur["Package"])})
    if not rows:  # fall back to apt-cache
        rc, out, _ = _run(["apt-cache", "search", q], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                if " - " in line:
                    name, desc = line.split(" - ", 1)
                    name = name.strip()
                    rows.append({"name": name, "label": name, "desc": desc.strip(),
                                 "installed": is_installed(name)})
    return rows[:60]


def install_pkg(pkg):
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", pkg or ""):
        return {"ok": False, "error": "invalid package name"}
    _INSTALL_CACHE.pop(pkg, None)
    _INSTALLING[pkg] = time.time()
    subprocess.Popen(["sudo", "apt-get", "install", "-y", pkg], env=_env(),
                     stdout=subprocess.DEVNULL, stderr=open("/tmp/remote-install.log", "ab"),
                     start_new_session=True)
    return {"ok": True, "status": "installing"}


_INSTALLING = {}  # pkg -> install start time (for progress reporting)


def install_status(pkg):
    installed = is_installed(pkg)
    if installed:
        _INSTALLING.pop(pkg, None)
        return {"installed": True, "installing": False}
    installing = pkg in _INSTALLING and time.time() - _INSTALLING[pkg] < 900
    return {"installed": False, "installing": installing}


# ------------------------------------------------------------------ clipboard

def _clip_get(selection):
    rc, out, _ = _run(["xclip", "-o", "-selection", selection], timeout=4)
    return out if rc == 0 else ""


def _clip_set(selection, text):
    # NOTE: xclip -i forks a background process that OWNS the selection and
    # stays alive. If we give it pipes, subprocess waits for the fork to
    # close them and times out. So: no pipes, detached, wait only for the
    # immediate parent (which exits after handing off to the owner).
    try:
        p = subprocess.Popen(["xclip", "-selection", selection, "-i"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=_env(),
                             start_new_session=True, text=True)
        p.communicate(text, timeout=4)
        return p.returncode == 0
    except Exception:
        return False


def clipboard_get():
    """Read both X selections. VNC only syncs CLIPBOARD, but terminals use PRIMARY."""
    cb = _clip_get("clipboard")
    prim = _clip_get("primary")
    return {"clipboard": cb[:100000], "primary": prim[:100000],
            "text": (cb or prim)[:100000]}


def clipboard_set(text):
    """Write the text into BOTH X selections so any app can paste it
    (Ctrl+V apps read CLIPBOARD, terminals/middle-click read PRIMARY)."""
    if not text:
        return {"ok": False, "error": "empty text"}
    ok_cb = _clip_set("clipboard", text)
    ok_prim = _clip_set("primary", text)
    clip_add(text, "phone")
    return {"ok": ok_cb or ok_prim}


# ------------------------------------------------------------------ clipboard history
#
# Multi-item clipboard: everything copied on the remote desktop (watched from
# the X selections) or pushed from the phone is recorded here, and any entry
# can be pasted back. Survives control-server restarts via a json file.

CLIP_HISTORY_FILE = os.path.join(BASE_DIR, ".cliphistory.json")
CLIP_HISTORY_MAX = 100
CLIP_ITEM_MAX = 20000

_clip_history = []
_clip_lock = threading.Lock()
_clip_last_sel = {"clipboard": None, "primary": None}


def clip_add(text, source):
    if not text:
        return
    text = text[:CLIP_ITEM_MAX]
    with _clip_lock:
        if _clip_history and _clip_history[0]["text"] == text:
            _clip_history[0]["time"] = int(time.time())   # bump recency
        else:
            _clip_history.insert(0, {
                "id": hashlib.sha1(f"{time.time_ns()}:{text[:64]}".encode()).hexdigest()[:12],
                "text": text, "time": int(time.time()), "source": source,
            })
        del _clip_history[CLIP_HISTORY_MAX:]
        _clip_save()


def _clip_save():
    try:
        with open(CLIP_HISTORY_FILE, "w") as f:
            json.dump(_clip_history, f)
        os.chmod(CLIP_HISTORY_FILE, 0o600)
    except Exception:
        pass


def _clip_load():
    global _clip_history
    try:
        with open(CLIP_HISTORY_FILE) as f:
            _clip_history = json.load(f)[:CLIP_HISTORY_MAX]
    except Exception:
        _clip_history = []


def clip_history():
    with _clip_lock:
        return [{"id": it["id"], "preview": (it["text"][:160] + ("…" if len(it["text"]) > 160 else "")),
                 "len": len(it["text"]), "time": it["time"], "source": it["source"]}
                for it in _clip_history]


def clip_item(clip_id):
    with _clip_lock:
        return next((it for it in _clip_history if it["id"] == clip_id), None)


def clip_remove(clip_id):
    with _clip_lock:
        _clip_history[:] = [it for it in _clip_history if it["id"] != clip_id]
        _clip_save()


def clip_clear():
    with _clip_lock:
        _clip_history.clear()
        _clip_save()


def clipboard_watcher():
    """Background: watch the X selections so anything copied inside the remote
    desktop (Ctrl+C in apps, plain selection in terminals) lands in history."""
    while True:
        time.sleep(1.5)
        try:
            cb = _clip_get("clipboard")
            prim = _clip_get("primary")
            if _clip_last_sel["clipboard"] is None and _clip_last_sel["primary"] is None \
               and not cb and not prim:
                # first look on an empty desktop: nothing to record
                _clip_last_sel["clipboard"] = cb
                _clip_last_sel["primary"] = prim
                continue
            # record each selection that changed (both can change at once)
            if cb and cb != _clip_last_sel["clipboard"]:
                clip_add(cb, "desktop")
            if prim and prim != _clip_last_sel["primary"] and prim != cb:
                clip_add(prim, "desktop")
            _clip_last_sel["clipboard"] = cb
            _clip_last_sel["primary"] = prim
        except Exception:
            pass


# ------------------------------------------------------------------ auth

def _make_token(user):
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{user}:{exp}".encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()


def _verify_token(tok):
    if not tok or not PASS_HASH:
        return False
    try:
        dec = base64.urlsafe_b64decode(tok.encode() + b"==")
        payload, sig = dec.rsplit(b":", 1)
        user, exp = payload.decode().rsplit(":", 1)
        if user != AUTH_USER or int(exp) < int(time.time()):
            return False
        expect = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.decode(), expect)
    except Exception:
        return False


def _verify_password(pw):
    try:
        return bool(PASS_HASH) and crypt.crypt(pw, PASS_HASH) == PASS_HASH
    except Exception:
        return False


def _parse_cookie(handler, name):
    try:
        c = http.cookies.SimpleCookie()
        c.load(handler.headers.get("Cookie", ""))
        return c[name].value
    except Exception:
        return None


def _client_ip(handler):
    return handler.headers.get("X-Forwarded-For", handler.client_address[0]).split(",")[0].strip()


def _login_allowed(ip):
    now = time.time()
    LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 600]
    return len(LOGIN_ATTEMPTS[ip]) < 5


# ------------------------------------------------------------------ static files

MIME = mimetypes.MimeTypes()
MIME.add_type("application/javascript", ".js")
MIME.add_type("application/wasm", ".wasm")
MIME.add_type("image/svg+xml", ".svg")


def send_file(handler, path, cache="no-cache, must-revalidate"):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        handler._json(404, {"error": "not found"})
        return
    etag = f'"{int(st.st_mtime):x}-{st.st_size:x}"'
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.end_headers()
        return
    ctype = MIME.guess_type(path)[0] or "application/octet-stream"
    head_only = getattr(handler, "_head_only", False)
    with open(path, "rb") as f:
        data = b"" if head_only else f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache)  # ETag revalidation; changes show up immediately
    handler.end_headers()
    handler.wfile.write(data)


# ------------------------------------------------------------------ websocket proxy
#
# Minimal RFC6455 WebSocket -> TCP proxy for the VNC stream (/websockify).
# Built in so the whole stack is one process on one port: static files,
# control API, audio streaming and VNC all live on :8090. Each websocket
# gets its own thread, so a slow mobile client can never stall the server.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
VNC_HOST = os.environ.get("VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.environ.get("VNC_PORT", "5901"))


def _read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_read_frame(rfile):
    """Read one client frame. Returns (opcode, payload) or None on EOF/error."""
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    op = b1 & 0x0f
    ln = b2 & 0x7f
    if ln == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        ln = struct.unpack(">H", ext)[0]
    elif ln == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        ln = struct.unpack(">Q", ext)[0]
    if ln > 64 * 1024 * 1024:
        return None
    mask = rfile.read(4) if b2 & 0x80 else b""
    data = _read_exact(rfile, ln) if ln else b""
    if data is None:
        return None
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return op, data


def _ws_frame(data):
    """Wrap raw bytes as a binary server->client frame."""
    ln = len(data)
    if ln < 126:
        return bytes((0x82, ln)) + data
    if ln < 65536:
        return bytes((0x82, 126)) + struct.pack(">H", ln) + data
    return bytes((0x82, 127)) + struct.pack(">Q", ln) + data


def websocket_proxy(handler):
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or "upgrade" not in (handler.headers.get("Connection", "") + "").lower():
        handler._json(400, {"error": "expected websocket upgrade"})
        return
    try:
        target = socket.create_connection((VNC_HOST, VNC_PORT), timeout=5)
    except Exception as e:
        log(f"VNC connect failed ({VNC_HOST}:{VNC_PORT}): {e}")
        handler._json(502, {"error": f"cannot reach VNC server {VNC_HOST}:{VNC_PORT}"})
        return

    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.close_connection = True
    log(f"ws proxy open  {handler.client_address[0]} -> {VNC_HOST}:{VNC_PORT}")

    stop = threading.Event()
    rfile, wfile = handler.rfile, handler.wfile

    def client_to_target():
        """Reader thread: parse client frames, forward payload to the VNC server."""
        try:
            while not stop.is_set():
                fr = _ws_read_frame(rfile)
                if fr is None:
                    break
                op, payload = fr
                if op == 0x8:                      # close
                    try:
                        wfile.write(bytes((0x88, 0)))
                        wfile.flush()
                    except Exception:
                        pass
                    break
                if op == 0x9:                      # ping -> pong
                    try:
                        wfile.write(bytes((0x8A, len(payload))) + payload)
                        wfile.flush()
                        continue
                    except Exception:
                        break
                if op in (0x1, 0x2, 0x0) and payload:   # text/binary/continuation
                    target.sendall(payload)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                target.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    reader = threading.Thread(target=client_to_target, daemon=True)
    reader.start()
    try:
        while not stop.is_set():
            ready, _, _ = select.select([target], [], [], 0.5)
            if not ready:
                continue
            data = target.recv(65536)
            if not data:
                break
            wfile.write(_ws_frame(data))
            wfile.flush()
    except Exception:
        pass
    finally:
        stop.set()
        try:
            target.close()
        except Exception:
            pass
        reader.join(timeout=2)
        log(f"ws proxy close {handler.client_address[0]}")


# ------------------------------------------------------------------ icons

def find_icon(name):
    """Resolve an Icon= value to a real image file (theme-aware, png preferred)."""
    if not name:
        return None
    if name.startswith("/"):                      # absolute path in the .desktop
        return name if os.path.isfile(name) else None
    import glob
    patterns = []
    for base in (os.path.expanduser("~/.local/share/icons/hicolor"), "/usr/share/icons/hicolor"):
        for sz in ("128x128", "96x96", "64x64", "72x72", "48x48", "256x256", "scalable"):
            patterns.append(f"{base}/{sz}/apps/{name}.png")
            patterns.append(f"{base}/{sz}/apps/{name}.svg")
    patterns.append(f"/usr/share/pixmaps/{name}.png")
    patterns.append(f"/usr/share/pixmaps/{name}.svg")
    patterns.append(f"/usr/share/pixmaps/{name}.xpm")
    for p in patterns:
        if os.path.isfile(p):
            return p
    for p in glob.glob(f"/usr/share/icons/*/*/apps/{name}.png"):   # other themes
        return p
    for p in glob.glob(f"/usr/share/icons/*/*/apps/{name}.svg"):
        return p
    return None


def icon_path_for_app(app_id):
    """Desktop id ('org.gnome.Nautilus.desktop') -> resolved icon file."""
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", app_id or ""):
        return None
    for d in APP_DIRS:
        p = os.path.join(d, app_id)
        if os.path.isfile(p):
            data = _parse_desktop(p)
            if data:
                return find_icon(data.get("Icon", "").strip())
            return None
    return None


# ------------------------------------------------------- stats / files / pins

def system_stats():
    """CPU / memory / disk / uptime snapshot for the More sheet."""
    def cpu_sample():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        return sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)  # total, idle
    t0, i0 = cpu_sample()
    time.sleep(0.25)
    t1, i1 = cpu_sample()
    dt = t1 - t0
    cpu = max(0.0, min(100.0, 100 * (1 - (i1 - i0) / dt))) if dt else 0.0
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.strip().split()[0]) * 1024
    du = shutil.disk_usage(os.path.expanduser("~"))
    try:
        with open("/proc/uptime") as f:
            uptime = int(float(f.read().split()[0]))
    except Exception:
        uptime = 0
    return {
        "cpu_pct": round(cpu, 1),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cores": os.cpu_count() or 1,
        "mem_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
        "mem_total": mem.get("MemTotal", 0),
        "disk_used": du.used, "disk_total": du.total,
        "uptime_s": uptime,
    }


FILES_DIR = os.environ.get("RD_FILES_DIR", os.path.expanduser("~/Downloads"))
UPLOAD_LIMIT = 2 * 1024**3


def list_files():
    os.makedirs(FILES_DIR, exist_ok=True)
    out = []
    for fn in os.listdir(FILES_DIR):
        p = os.path.join(FILES_DIR, fn)
        try:
            if os.path.isfile(p) and not os.path.islink(p):
                st = os.stat(p)
                out.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _safe_file_path(name):
    """basename-only, no traversal — resolve inside ~/Downloads."""
    name = os.path.basename(name or "")
    if not name or name in (".", "..") or "\x00" in name or len(name) > 255:
        return None
    p = os.path.realpath(os.path.join(FILES_DIR, name))
    if os.path.dirname(p) != os.path.realpath(FILES_DIR):
        return None
    return p


PINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pins.json")


def load_pins():
    try:
        with open(PINS_FILE) as f:
            pins = json.load(f)
        if isinstance(pins, list):
            return [p for p in pins if isinstance(p, str)]
    except Exception:
        pass
    return []


def save_pins(pins):
    pins = [p for p in pins if isinstance(p, str) and len(p) < 200][:100]
    tmp = PINS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pins, f)
    os.replace(tmp, PINS_FILE)
    try:
        os.chmod(PINS_FILE, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ request handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RemoteControl/2.0"

    # ---- plumbing
    def _json(self, code, obj, extra=None):
        data = b"" if getattr(self, "_head_only", False) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(json.dumps(obj).encode())))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _authed(self):
        return _verify_token(_parse_cookie(self, "rd_session"))

    def log_message(self, fmt, *args):
        log(f"{_client_ip(self)} {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- streaming audio (needs custom response, not _json)
    def _stream_audio(self, mime, codec_args):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        cmd = ["ffmpeg", "-loglevel", "warning", "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "pulse", "-i", audio_source(), *codec_args]
        try:
            proc = subprocess.Popen(cmd, env=_pa_env(), stdout=subprocess.PIPE,
                                    stderr=open(AUDIO_LOG, "ab"), preexec_fn=_die_with_parent)
        except Exception as e:
            log(f"ffmpeg start failed: {e}")
            return
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; normal on mobile when the tab is backgrounded
        finally:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    # ---- GET
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        # websocket upgrade for the VNC stream (same origin, same port)
        if path == "/websockify" or path.startswith("/websockify/"):
            return websocket_proxy(self)

        # public endpoints
        if path == "/api/health":
            return self._json(200, {"ok": True, "time": int(time.time())})
        if path == "/api/login":
            return self._login()
        if path == "/login":
            return send_file(self, os.path.join(WEB_DIR, "login.html"))

        if path.startswith("/api/"):
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})

            if path == "/api/auth_check":
                return self._json(200, {"ok": True})
            if path == "/api/windows":
                try:
                    return self._json(200, {"ok": True, "windows": list_windows()})
                except Exception as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            if path == "/api/apps":
                return self._json(200, {"apps": list_desktop_apps()})
            if path == "/api/store":
                return self._json(200, {"apps": [{**a, "installed": is_installed(a["pkg"])} for a in APP_CATALOG]})
            if path == "/api/store/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()
                if not q or len(q) > 80:
                    return self._json(200, {"apps": []})
                return self._json(200, {"apps": store_search(q)})
            if path == "/api/store/status":
                q = parse_qs(url.query)
                pkg = q.get("pkg", [""])[0]
                if not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", pkg or ""):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, install_status(pkg))
            if path == "/api/audio/status":
                return self._json(200, audio_status())
            if path == "/api/clipboard":
                return self._json(200, clipboard_get())
            if path == "/api/clipboard/history":
                return self._json(200, {"items": clip_history()})
            if path == "/api/audio/test":
                sink = _pa("get-default-sink") or "remote_out"
                run_detached(["ffmpeg", "-loglevel", "error", "-f", "lavfi",
                              "-i", "sine=frequency=440:duration=4", "-f", "pulse", "-device", sink, "remote-test"])
                return self._json(200, {"ok": True, "msg": "playing a 4s 440Hz tone into " + sink})
            if path == "/api/audio/raw":
                return self._stream_audio(
                    "application/octet-stream",
                    ["-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-f", "s16le", "-"])
            if path == "/api/audio.opus":
                return self._stream_audio(
                    'audio/webm; codecs="opus"',
                    ["-c:a", "libopus", "-b:a", "96k", "-vbr", "on",
                     "-frame_duration", "20", "-f", "webm", "-"])
            if path == "/api/stats":
                return self._json(200, system_stats())
            if path == "/api/files":
                return self._json(200, {"files": list_files(), "dir": FILES_DIR})
            if path == "/api/download":
                name = parse_qs(url.query).get("name", [""])[0]
                p = _safe_file_path(name)
                if not p or not os.path.isfile(p):
                    return self._json(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            if path == "/api/pins":
                return self._json(200, {"pins": load_pins()})
            return self._json(404, {"error": "not found"})

        # static files
        if path == "/":
            if not self._authed():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return send_file(self, os.path.join(WEB_DIR, "index.html"))

        # icons for the Apps menu (resolved from the app's own .desktop Icon=)
        if path.startswith("/icon/"):
            app_id = unquote(path[6:])
            if not self._authed():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            p = icon_path_for_app(app_id)
            if not p or not os.path.isfile(p):
                return self._json(404, {"error": "no icon"})
            return send_file(self, p, cache="max-age=86400")

        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            return self._json(403, {"error": "forbidden"})
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        return send_file(self, full)

    # ---- POST
    def do_POST(self):
        url = urlparse(self.path)
        path = url.path

        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._json(200, {"ok": True}, extra={"Set-Cookie":
                           "rd_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})

        if not self._authed():
            return self._json(401, {"ok": False, "error": "unauthorized"})

        try:
            if path == "/api/fit":
                return self._json(200, {"ok": True, "windows": fit_windows()})
            if path.startswith("/api/resize"):
                q = parse_qs(url.query)
                return self._json(200, resize_display(q["w"][0], q["h"][0]))
            if path.startswith("/api/open/"):
                name = path.split("/", 3)[3]
                return self._json(200, self._open_app(name))
            if path == "/api/paste":
                return self._paste()
            if path == "/api/clipboard/set":
                data = self._body()
                text = data.get("text", "")
                if len(text) > 100_000:
                    return self._json(400, {"ok": False, "error": "text too long"})
                return self._json(200, clipboard_set(text))
            if path == "/api/clipboard/paste":
                data = self._body()
                it = clip_item(data.get("id", ""))
                if not it:
                    return self._json(404, {"ok": False, "error": "no such clipboard item"})
                return self._json(200, clipboard_set(it["text"]))
            if path == "/api/clipboard/item":
                it = clip_item(parse_qs(url.query).get("id", [""])[0])
                if not it:
                    return self._json(404, {"ok": False, "error": "no such item"})
                return self._json(200, {"id": it["id"], "text": it["text"]})
            if path == "/api/clipboard/remove":
                clip_remove(self._body().get("id", ""))
                return self._json(200, {"ok": True})
            if path == "/api/clipboard/clear":
                clip_clear()
                return self._json(200, {"ok": True})
            if path == "/api/store/install":
                data = self._body()
                pkg = data.get("pkg", "")
                if not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", pkg or ""):
                    return self._json(400, {"ok": False, "error": "invalid package name"})
                return self._json(200, install_pkg(pkg))
            if path == "/api/launch":
                data = self._body()
                app_id = data.get("id", "")
                if not app_id or len(app_id) > 200 or ".." in app_id or app_id.startswith("/"):
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                return self._json(200, launch_desktop(app_id))
            if path == "/api/store/launch":
                return self._json(200, self_launch_name(self._body().get("name", "")))
            if path == "/api/upload":
                name = os.path.basename(parse_qs(url.query).get("name", [""])[0])[:200]
                p = _safe_file_path(name)
                if not p:
                    return self._json(400, {"ok": False, "error": "invalid filename"})
                try:
                    remaining = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    remaining = 0
                os.makedirs(FILES_DIR, exist_ok=True)
                n = 0
                try:
                    with open(p, "wb") as f:
                        while remaining > 0:
                            chunk = self.rfile.read(min(1 << 16, remaining))
                            if not chunk:
                                break
                            n += len(chunk)
                            remaining -= len(chunk)
                            if n > UPLOAD_LIMIT:
                                raise OverflowError
                            f.write(chunk)
                except OverflowError:
                    try: os.remove(p)
                    except OSError: pass
                    return self._json(413, {"ok": False, "error": "file too large (2GB limit)"})
                except Exception as e:
                    try: os.remove(p)
                    except OSError: pass
                    return self._json(500, {"ok": False, "error": str(e)[:200]})
                return self._json(200, {"ok": True, "name": name, "size": n})
            if path == "/api/files/delete":
                p = _safe_file_path(self._body().get("name", ""))
                if not p or not os.path.isfile(p):
                    return self._json(404, {"ok": False, "error": "not found"})
                os.remove(p)
                return self._json(200, {"ok": True})
            if path == "/api/pins/toggle":
                app_id = self._body().get("id", "")
                if not app_id or len(app_id) > 200:
                    return self._json(400, {"ok": False, "error": "invalid app id"})
                pins = load_pins()
                if app_id in pins:
                    pins.remove(app_id)
                else:
                    pins.insert(0, app_id)
                save_pins(pins)
                return self._json(200, {"ok": True, "pins": pins})
            return self._json(404, {"error": "not found"})
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log(f"POST {path} error: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    # ---- endpoint bodies
    OPEN_MAP = {
        "discord":  lambda: focus_or_launch("discord|Discord", ["discord"]),
        "chrome":   lambda: focus_or_launch("Google-chrome",
                                            ["google-chrome", "--no-sandbox", "--disable-gpu",
                                             "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"]),
        "firefox":  lambda: focus_or_launch("Navigator|firefox", ["firefox"]),
        "nautilus": lambda: focus_or_launch("nautilus|Nautilus", ["nautilus"]),
        "terminal": lambda: run_detached(["xterm"]),
    }

    def _open_app(self, name):
        fn = self.OPEN_MAP.get(name)
        if fn:
            return fn()
        return launch_desktop(name)

    def _paste(self):
        text = self._body().get("text", "")
        if not text:
            return self._json(200, {"ok": True})
        if len(text) > 100_000:
            return self._json(400, {"ok": False, "error": "text too long"})
        rc, _, err = _run(["xdotool", "type", "--clearmodifiers", "--delay", "3", text], timeout=30)
        return self._json(200 if rc == 0 else 500,
                          {"ok": rc == 0, "error": None if rc == 0 else err[:200]})

    def _login(self):
        ip = _client_ip(self)
        if not _login_allowed(ip):
            log(f"login rate-limited: {ip}")
            return self._json(429, {"ok": False, "error": "too many attempts, wait 10 minutes"})
        try:
            data = self._body()
        except Exception:
            return self._json(400, {"ok": False, "error": "bad request"})
        LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())
        if data.get("username") == AUTH_USER and _verify_password(data.get("password", "")):
            log(f"login ok: {ip}")
            return self._json(200, {"ok": True}, extra={"Set-Cookie":
                f"rd_session={_make_token(AUTH_USER)}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TOKEN_TTL}"})
        log(f"login failed: {ip}")
        return self._json(401, {"ok": False})


def main():
    ThreadingHTTPServer.allow_reuse_address = True
    _clip_load()
    # prime the watcher with whatever is currently selected so a restart
    # doesn't re-record the existing clipboard as a "new" history entry
    try:
        _clip_last_sel["clipboard"] = _clip_get("clipboard")
        _clip_last_sel["primary"] = _clip_get("primary")
    except Exception:
        pass
    threading.Thread(target=clipboard_watcher, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", 8090), Handler)
    httpd.daemon_threads = True
    log("control server starting on 127.0.0.1:8090")
    print("Remote control server: http://127.0.0.1:8090  (log: " + LOG_FILE + ")")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
