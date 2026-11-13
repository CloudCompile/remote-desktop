"""Apps: .desktop discovery, the legacy catalog and launching.

Ownership: scanning application directories, parsing .desktop entries,
resolving icons, and launch-or-focus behaviour (window matching lives in
windows.py). Nothing here starts threads at import time (#22).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import config, procs, state, store, windows

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
                    "source": "desktop",  # #65: merged lists carry their origin
                }
            )
            seen.add(fn)
    for fb in FALLBACK_APPS:
        if not any(fb["cmd"] in a["cmd"] for a in apps):
            apps.append({**fb, "source": "desktop"})
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


def list_flatpak_apps():
    """Installed Flatpak apps (#65). Missing backend -> empty list, no error."""
    if not shutil.which("flatpak"):
        return []
    rc, out, _ = procs._run(["flatpak", "list", "--app", "--columns=application,name,icon"], timeout=20)
    apps = []
    if rc != 0 or not out.strip():
        return apps
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if not cols or not cols[0]:
            continue
        app_id = cols[0]
        name = cols[1] if len(cols) > 1 and cols[1] else app_id
        icon = cols[2] if len(cols) > 2 else ""
        apps.append(
            {
                "id": app_id,
                "name": name,
                "desc": "Flatpak app",
                "cmd": f"flatpak run {app_id}",
                "terminal": False,
                "categories": "",
                # WM_CLASS is usually the flatpak id or its last component (#73)
                "wmclass": app_id.rsplit(".", 1)[-1] if "." in app_id else app_id,
                "icon": icon,
                "source": "flatpak",
            }
        )
    return apps


def list_snap_apps():
    """Installed Snap apps (#65). Missing backend -> empty list, no error."""
    if not shutil.which("snap"):
        return []
    rc, out, _ = procs._run(["snap", "list"], timeout=20)
    apps = []
    if rc != 0 or not out.strip():
        return apps
    lines = out.strip().splitlines()[1:]  # header: Name Version Rev ...
    runtime_snaps = {"core", "core18", "core20", "core22", "core24", "core26", "snapd", "bare"}
    for line in lines:
        cols = line.split()
        if not cols or cols[0] in runtime_snaps or cols[0].endswith("-kernel"):
            continue
        name = cols[0]
        apps.append(
            {
                "id": name,
                "name": name.capitalize(),
                "desc": "Snap app",
                "cmd": f"snap run {name}",
                "terminal": False,
                "categories": "",
                # snap WM_CLASS is usually the snap name (capitalized variant too, #73)
                "wmclass": name,
                "source": "snap",
            }
        )
    return apps


# ------------------------------------------------------------- custom apps
# #63: user-defined entries; commands are argv VECTORS launched without a
# shell — validation here is the security boundary.


CUSTOM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}")


SHELL_WRAPPERS = {"sh", "bash", "dash", "zsh", "fish", "ksh", "powershell", "cmd", "cmd.exe"}


def validate_custom_app(data):
    """Validate a custom app entry. Returns (entry, None) or (None, error).
    Commands are argv vectors launched WITHOUT a shell (#63 acceptance):
    no wrapper may smuggle a shell string through."""
    if not isinstance(data, dict):
        return None, "body must be an object"
    label = str(data.get("label") or "").strip()
    if not label or len(label) > 60:
        return None, "label must be 1-60 characters"
    cmd = data.get("cmd")
    if not isinstance(cmd, list) or not (1 <= len(cmd) <= 8):
        return None, "cmd must be an array of 1-8 strings"
    clean = []
    for c in cmd:
        if not isinstance(c, str) or not c.strip() or len(c) > 200:
            return None, "each cmd entry must be a non-empty string of at most 200 characters"
        if "\x00" in c or "\n" in c or "\r" in c:
            return None, "cmd entries may not contain control characters"
        clean.append(c.strip())
    if os.path.basename(clean[0]).lower() in SHELL_WRAPPERS:
        return None, "no shell wrappers — enter the binary and its arguments directly"
    # never interpolated into a shell (#63 acceptance): entries launch as an
    # argv vector via execvp — there is no shell to escape out of.
    entry = {
        "id": "",
        "name": label,  # the shared app pipeline matches/ranks on `name`
        "label": label,
        "cmd": clean,
        "desc": str(data.get("desc") or "").strip()[:120],
        "wmclass": str(data.get("wmclass") or "").strip()[:60],  # "binds to window" override (#73)
        "icon": str(data.get("icon") or "").strip()[:200],
        "terminal": bool(data.get("terminal")),
        "source": "custom",
    }
    app_id = str(data.get("id") or "").strip()
    if app_id:
        if not CUSTOM_ID_RE.fullmatch(app_id):
            return None, "id must be 1-60 characters: letters, digits, space, dot, underscore, dash"
        entry["id"] = app_id
    else:
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", label)[:40].strip("-") or "app"
        entry["id"] = f"{slug}-{hashlib.sha1(json.dumps(clean).encode()).hexdigest()[:6]}"
    return entry, None


def list_custom_apps():
    out = []
    for e in state.load_custom_apps():
        if not e.get("name"):
            e["name"] = e.get("label") or e.get("id") or "custom app"
        out.append(e)
    return out


def add_custom_app(data):
    entry, err = validate_custom_app(data)
    if err:
        return {"ok": False, "error": err}
    entries = state.load_custom_apps()
    if len(entries) >= state.CUSTOM_APPS_MAX:
        return {"ok": False, "error": f"too many custom apps (max {state.CUSTOM_APPS_MAX})"}
    if any(e.get("id") == entry["id"] for e in entries):
        # same id = replace (edit), so the editor can update entries
        entries = [e for e in entries if e.get("id") != entry["id"]]
    entries.append(entry)
    state.save_custom_apps(entries)
    return {"ok": True, "app": entry}


def delete_custom_app(app_id):
    entries = state.load_custom_apps()
    kept = [e for e in entries if e.get("id") != app_id]
    if len(kept) == len(entries):
        return {"ok": False, "error": "no such custom app"}
    state.save_custom_apps(kept)
    return {"ok": True}


# --------------------------------------------------------------- merged list
# /api/apps (#62 #63 #65 #77): every source merged, deduped against the
# .desktop list, sorted pinned -> recently launched -> name, served with an
# ETag so warm responses skip the scan entirely.


def _dedupe(sources):
    """Merge app lists; flatpak/snap entries that shadow a .desktop entry
    (same name or WM_CLASS) are dropped (#65). Custom entries always win
    over discovered ones with the same id."""
    desktop = [dict(a, source="desktop") for a in sources.get("desktop", [])]
    names = {(a.get("name") or "").strip().lower() for a in desktop}
    classes = {(a.get("wmclass") or "").strip().lower() for a in desktop if a.get("wmclass")}
    merged = list(desktop)
    for src in ("flatpak", "snap"):
        for a in sources.get(src, []):
            nm = (a.get("name") or "").strip().lower()
            wm = (a.get("wmclass") or "").strip().lower()
            if not nm or nm in names or (wm and wm in classes):
                continue  # already discoverable as a .desktop entry
            merged.append(a)
            names.add(nm)
            if wm:
                classes.add(wm)
    for a in sources.get("custom", []):
        merged = [x for x in merged if x.get("id") != a["id"]]
        merged.append(a)
    return merged


def _rank(apps):
    """pinned (pin order) -> launched within 7 days (newest first) -> name (#62)."""
    pin_pos = {pid: i for i, pid in enumerate(state.load_pins())}
    recents = state.recent_launches()

    def key(a):
        aid = a["id"]
        nm = (a.get("name") or aid).lower()
        pin = pin_pos.get(aid)
        ts = recents.get(aid)
        if pin is not None:
            return (0, pin, 0, nm)
        if ts is not None:
            return (1, -ts, 0, nm)
        return (2, 0, 0, nm)

    return sorted(apps, key=key)


def all_apps():
    return _rank(
        _dedupe(
            {
                "desktop": list_desktop_apps(),
                "flatpak": list_flatpak_apps(),
                "snap": list_snap_apps(),
                "custom": list_custom_apps(),
            }
        )
    )


_apps_cache = {"fp": None, "etag": None, "body": b""}


def _fingerprint():
    """Cheap change detector for /api/apps (#77): directory mtimes + entry
    counts + every state file the ranking depends on. One listdir per dir —
    no .desktop parsing, no icon globbing."""
    parts = []
    for d in APP_DIRS:
        try:
            st = os.stat(d)
            parts.append(f"{d}:{int(st.st_mtime)}:{len(os.listdir(d))}")
        except OSError:
            parts.append(f"{d}:missing")
    for tool in ("flatpak", "snap"):
        parts.append(f"{tool}:{1 if shutil.which(tool) else 0}")
    for p in (state.custom_apps_file(), state.PINS_FILE, state.launch_history_file()):
        try:
            parts.append(f"{p}:{int(os.stat(p).st_mtime)}")
        except OSError:
            parts.append(f"{p}:missing")
    return "|".join(parts)


def apps_response():
    """Serialized {"apps": [...]} body + ETag; cached until the
    fingerprint changes (#77). Returns (body, etag)."""
    fp = _fingerprint()
    if _apps_cache["etag"] and _apps_cache["fp"] == fp:
        return _apps_cache["body"], _apps_cache["etag"]
    merged = all_apps()
    body = json.dumps({"apps": merged}).encode()
    etag = '"apps-' + hashlib.sha1(fp.encode() + str(len(merged)).encode()).hexdigest()[:16] + '"'
    _apps_cache.update({"fp": fp, "etag": etag, "body": body})
    return body, etag


def find_app_entry(app_id):
    """Any-source lookup by id (desktop file, flatpak id, snap name, custom)."""
    for a in all_apps():
        if a["id"] == app_id:
            return a
    return None


# ---------------------------------------------------------- focus matching
# #73: WM_CLASS/title heuristics for Flatpak (WM_CLASS often equals the
# flatpak id), Snap and Wayland-window titles. Pure functions over faked
# xdotool output in the tests — no X needed.


def _wmclass_candidates(a):
    """WM_CLASS strings to try for an app entry, most specific first (#73)."""
    out, seen = [], set()

    def add(v):
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(a.get("wmclass"))  # user override / StartupWMClass wins
    if a.get("source") in ("flatpak", "snap"):
        # flatpak ids (org.mozilla.Firefox): WM_CLASS is often the id itself
        # or its last component; snap classes usually capitalize the name
        for raw in (a.get("id") or "", a.get("flatpak_id") or ""):
            if not raw:
                continue
            add(raw)
            last = raw.rsplit(".", 1)[-1]
            add(last)
            add(last.capitalize())
    if a.get("name"):
        add(a["name"])
        add(a["name"].split()[0].capitalize())  # "Firefox Web Browser" -> Firefox
    return out


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
    else:
        # #73: packaged apps — every WM_CLASS variant (flatpak id, snap
        # capitalisation, custom "binds to window" override), instance first
        for v in _wmclass_candidates(a):
            cands.append(("classname", re.escape(v)))
            cands.append(("class", v))
    # visible windows first
    for kind, pat in cands:
        rc, out, _ = procs._run(["xdotool", "search", "--onlyvisible", "--" + kind, pat])
        if rc == 0 and out.strip():
            win = windows._pick_window(out.split(), chrome=is_chrome)
            if win:
                return win
    # fallback: apps whose titles do start with their name (also covers
    # Wayland-native windows whose WM_CLASS is missing but whose title
    # carries the app name, #73); terminal apps get an exact -title
    if a.get("name"):
        rc, out, _ = procs._run(
            ["xdotool", "search", "--onlyvisible", "--name", "^" + re.escape(a["name"])]
        )
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


def app_is_running(a):
    """Best-effort process match (#73): is the app's binary running even
    though no window matched? Used to avoid duplicate launches of
    single-instance packaged apps."""
    cmd = (a.get("cmd") or "").split()
    if not cmd:
        return False
    # match the actual binary (skip wrappers like flatpak/snap run)
    exe = cmd[-1] if cmd[0] in ("flatpak", "snap") else cmd[0]
    exe = os.path.basename(exe)
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", exe):
        return False
    rc, out, _ = procs._run(["pgrep", "-f", exe], timeout=3)
    return rc == 0 and bool(out.strip())


# ------------------------------------------------------------ launching
# #75: launches capture the child's early exit (first seconds) and return
# structured errors with actionable hints; a slow-starting GUI is NOT a
# failure. #62: successful launches are recorded for the ranking.

LAUNCH_PROBE_SECONDS = 2.0


def _launch_hint(rc, err, cmd):
    """Actionable remediation for a failed launch (#75)."""
    low = (err or "").lower()
    if "cannot open display" in low or "cannot connect to x server" in low or "authorization required" in low:
        return "could not connect to the display — is the desktop session running?"
    if "command not found" in low or "no such file or directory" in low:
        return f"'{os.path.basename(cmd[0])}' is not installed — try the Store"
    if rc:
        return f"the app exited immediately (exit code {rc}) — check its command line"
    return "the app exited immediately — check its command line"


def launch_process(cmd):
    """Launch an argv vector (never a shell) and watch the first seconds
    (#75). Returns {"ok": True} while the child keeps running, or
    {"ok": False, "error", "hint"} when it exits early."""
    try:
        with tempfile.TemporaryFile() as errf:
            try:
                p = subprocess.Popen(
                    cmd, env=config._pa_env(), stdout=subprocess.DEVNULL, stderr=errf, start_new_session=True
                )
            except FileNotFoundError:
                exe = os.path.basename(cmd[0]) if cmd else "?"
                return {
                    "ok": False,
                    "error": "missing binary",
                    "hint": f"'{exe}' is not installed — try the Store",
                }
            except PermissionError:
                return {"ok": False, "error": "not executable", "hint": f"'{cmd[0]}' is not executable"}
            time.sleep(LAUNCH_PROBE_SECONDS)
            rc = p.poll()
            if rc is None:
                return {"ok": True}  # still running: a slow-starting GUI, not a failure
            errf.seek(0)
            err = errf.read().decode(errors="replace")[:400]
            return {"ok": False, "error": f"exit code {rc}", "hint": _launch_hint(rc, err, cmd)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "hint": "could not start the app"}


def _after_launch(a):
    """Post-launch: fit or restore geometry in the background (#76)."""
    settings = state.load_settings()
    if settings.get("geo_remember", True) and a.get("wmclass"):

        def _restore():
            if not windows.restore_after_launch(a["wmclass"], tries=15):
                windows.fit_after_launch(tries=15)

        threading.Thread(target=_restore, daemon=True).start()
    else:
        threading.Thread(target=windows.fit_after_launch, daemon=True).start()


def launch_desktop(app_id):
    """Launch an app by id (as returned by /api/apps): a .desktop entry, a
    Flatpak id, a Snap name or a custom entry. If the app already has a
    window, focus/restore it instead of launching a duplicate."""
    a = find_app_entry(app_id)
    if a:
        win = find_app_window(a)
        if win:
            windows._activate_window(win)
            return {"ok": True, "focused": True}
        # #73: packaged apps are single-instance — if the process is alive
        # without a window (tray apps), never launch a duplicate.
        if a.get("source") in ("flatpak", "snap") and app_is_running(a):
            return {"ok": True, "focused": False, "running": True}
        return _launch_entry(a)
    # also accept catalog names / bare commands for compatibility
    return self_launch_name(app_id)


def _launch_entry(a):
    """Start an app entry (any source), report structured failures (#75).

    .desktop Exec strings keep their historical `sh -c` launch (the file is
    part of the scanned system); custom/flatpak/snap entries are argv
    vectors exec'd directly — user input never reaches a shell (#63)."""
    raw = a.get("cmd")
    if isinstance(raw, str):  # .desktop Exec string
        if not raw.strip():
            return {"ok": False, "error": "empty command", "hint": "the app entry has no launch command"}
        cmd = ["xterm", "-title", a["name"], "-e", "sh", "-c", raw] if a.get("terminal") else ["sh", "-c", raw]
    else:  # argv vector (custom / flatpak / snap): no shell, ever (#63)
        cmd = list(raw or [])
        if not cmd:
            return {"ok": False, "error": "empty command", "hint": "the app entry has no launch command"}
        if a.get("terminal"):
            cmd = ["xterm", "-title", a["name"], "-e", *cmd]
    r = launch_process(cmd)
    if not r.get("ok"):
        r["cmd"] = " ".join(cmd)
        return r
    state.record_launch(a["id"])
    _after_launch(a)
    return {"ok": True}


def self_launch_name(name):
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", name):
        return {"ok": False, "error": "invalid name"}
    app = next((a for a in APP_CATALOG if a["name"] == name), None)
    if app:
        if not store.is_installed(app["pkg"]):
            # #75: actionable, not a dead end
            return {
                "ok": False,
                "error": "not installed",
                "hint": f"'{app['pkg']}' is not installed — try the Store",
                "cmd": app["pkg"],
            }
        r = launch_process(app["cmd"])
        if r.get("ok"):
            state.record_launch(name)
            threading.Thread(target=windows.fit_after_launch, daemon=True).start()
        else:
            r["cmd"] = " ".join(app["cmd"])
        return r
    exe = shutil.which(name, path=config._env().get("PATH"))
    if not exe:
        return {
            "ok": False,
            "error": "no executable",
            "hint": f"'{name}' is not installed — try the Store",
            "cmd": name,
        }
    r = launch_process([exe])
    if r.get("ok"):
        state.record_launch(name)
    else:
        r["cmd"] = exe
    return r


def find_icon(name):
    """Resolve an Icon= value to a real image file (theme-aware, png preferred).
    Includes the Flatpak/Snap export dirs (#65)."""
    if not name:
        return None
    if name.startswith("/"):  # absolute path in the .desktop
        return name if os.path.isfile(name) else None
    import glob

    exports = [  # flatpak/snap expose icons through their own exports (#65)
        os.path.expanduser("~/.local/share/flatpak/exports/share/icons/hicolor"),
        "/var/lib/flatpak/exports/share/icons/hicolor",
        "/var/lib/snapd/desktop/icons/hicolor",
    ]
    patterns = []
    for base in (
        os.path.expanduser("~/.local/share/icons/hicolor"),
        "/usr/share/icons/hicolor",
        *exports,
    ):
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
    """App id (as returned by /api/apps) -> resolved icon file. Handles
    .desktop ids, custom entries with an Icon=/path (#63) and flatpak ids (#65)."""
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", app_id or ""):
        return None
    for e in list_custom_apps():  # #63: custom entries may name an icon/path
        if e.get("id") == app_id:
            icon = e.get("icon") or ""
            if icon and os.path.isfile(icon):
                return icon
            return find_icon(icon) if icon else None
    for d in APP_DIRS:
        p = os.path.join(d, app_id)
        if os.path.isfile(p):
            data = _parse_desktop(p)
            if data:
                return find_icon(data.get("Icon", "").strip())
            return None
    # flatpak ids resolve through the hicolor exports (#65)
    if "." in app_id:
        return find_icon(app_id)
    return None
