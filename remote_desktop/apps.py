"""Apps: .desktop discovery, the legacy catalog and launching.

Ownership: scanning application directories, parsing .desktop entries,
resolving icons, and launch-or-focus behaviour (window matching lives in
windows.py). Nothing here starts threads at import time (#22).
"""

import os
import re
import shutil
import threading

from . import config, procs, store, windows

APP_CATALOG = [
    {
        "name": "chrome",
        "label": "Chrome",
        "pkg": "google-chrome-stable",
        "cmd": [
            "google-chrome",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
        ],
        "desc": "Web browser",
    },
    {"name": "firefox", "label": "Firefox", "pkg": "firefox", "cmd": ["firefox"], "desc": "Web browser"},
    {"name": "discord", "label": "Discord", "pkg": "discord", "cmd": ["discord"], "desc": "Chat"},
    {"name": "terminal", "label": "Terminal", "pkg": "xterm", "cmd": ["xterm"], "desc": "Xterm"},
    {"name": "files", "label": "Files", "pkg": "nautilus", "cmd": ["nautilus"], "desc": "File manager"},
    {"name": "editor", "label": "Text Editor", "pkg": "gedit", "cmd": ["gedit"], "desc": "Simple text editor"},
    {"name": "vlc", "label": "VLC", "pkg": "vlc", "cmd": ["vlc"], "desc": "Media player"},
    {"name": "gimp", "label": "GIMP", "pkg": "gimp", "cmd": ["gimp"], "desc": "Image editor"},
    {"name": "evince", "label": "Docs Viewer", "pkg": "evince", "cmd": ["evince"], "desc": "PDF viewer"},
]

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
                    k = k.split("[", 1)[0].strip()  # Name[de] -> Name
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
            apps.append(
                {
                    "id": fn,
                    "name": data.get("Name") or base_id,
                    "desc": data.get("Comment") or data.get("GenericName") or "",
                    "cmd": cmd,
                    "terminal": data.get("Terminal", "false").lower() in ("true", "1"),
                    "categories": data.get("Categories", ""),
                    "wmclass": wmclass or base_id or os.path.basename(exe_name),
                }
            )
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
    cands = []  # (xdotool match kind, pattern)
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
        rc, out, _ = procs._run(["xdotool", "search", "--onlyvisible", "--" + kind, pat])
        if rc == 0 and out.strip():
            win = windows._pick_window(out.split(), chrome=is_chrome)
            if win:
                return win
    # fallback: apps whose titles do start with their name; terminal apps
    # get an exact -title
    if is_web or a.get("terminal"):
        rc, out, _ = procs._run(["xdotool", "search", "--onlyvisible", "--name", "^" + re.escape(a["name"])])
        if rc == 0 and out.strip():
            win = windows._pick_window(out.split(), chrome=is_chrome)
            if win:
                return win
    # minimized/iconic windows are not "visible" — try the full list and restore
    for kind, pat in cands:
        rc, out, _ = procs._run(["xdotool", "search", "--" + kind, pat])
        if rc == 0 and out.strip():
            win = windows._pick_window(out.split(), chrome=is_chrome)
            if win:
                windows._activate_window(win)
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
                windows._activate_window(win)
                return {"ok": True, "focused": True}
            if a["terminal"]:
                procs.run_detached(["xterm", "-title", a["name"], "-e", "sh", "-c", a["cmd"]])
            else:
                procs.run_detached(["sh", "-c", a["cmd"]])
            threading.Thread(target=windows.fit_after_launch, daemon=True).start()
            return {"ok": True}
    # also accept catalog names / bare commands for compatibility
    return self_launch_name(app_id)


def self_launch_name(name):
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", name):
        return {"ok": False, "error": "invalid name"}
    app = next((a for a in APP_CATALOG if a["name"] == name), None)
    if app:
        if not store.is_installed(app["pkg"]):
            return {"ok": False, "error": "not installed"}
        procs.run_detached(app["cmd"])
        threading.Thread(target=windows.fit_after_launch, daemon=True).start()
        return {"ok": True}
    exe = shutil.which(name, path=config._env().get("PATH"))
    if not exe:
        return {"ok": False, "error": "no executable"}
    procs.run_detached([exe])
    return {"ok": True}


def find_icon(name):
    """Resolve an Icon= value to a real image file (theme-aware, png preferred)."""
    if not name:
        return None
    if name.startswith("/"):  # absolute path in the .desktop
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
    for p in glob.glob(f"/usr/share/icons/*/*/apps/{name}.png"):  # other themes
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
