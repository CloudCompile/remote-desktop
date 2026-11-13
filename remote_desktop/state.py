"""Runtime state: pins, feature settings, launch history, custom apps,
display geometry state and versioned backups (#30).

Ownership: everything the server persists between runs.
- Pins: BASE_DIR/.pins.json — v2 format with named folders (#64); the flat
  v1 list migrates on first load (existing pins appear under "Unfiled").
- Settings: STATE_DIR/settings.json — opt-in toggles (#72 clipboard sync,
  #76 geometry remembering).
- Launch history: STATE_DIR/launch-history.json — recency ranking (#62).
- Custom apps: STATE_DIR/custom-apps.json — user-defined launcher entries
  (#63); rides along in backups/exports.
- Display state: STATE_DIR/display.json — last client-requested geometry
  and DPI, bounded, consumed by session.sh on the next session start (#31).
- Backups: STATE_DIR/backups/<version>-<timestamp>-<reason>/ snapshots of
  the live state files, taken at startup and via POST /api/backup; the
  newest 5 are kept. Export/import archives (tar.gz + manifest) move state
  between hosts; the password hash is NEVER included (#30 acceptance:
  secrets excluded) and archives newer than STATE_VERSION are refused.
"""

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time

from . import clipboard as _clipboard
from . import config, logging as log

STATE_VERSION = 1
BACKUP_KEEP = 5

PINS_FILE = os.path.join(config.BASE_DIR, ".pins.json")

# relative names inside backups/archives
_PIN_NAME = ".pins.json"
_CLIP_NAME = ".cliphistory.json"
_CUSTOM_NAME = "custom-apps.json"  # #63: custom apps ride along in state backups

PIN_FOLDERS_MAX = 24
PIN_NAME_MAX = 40
UNFILED = "Unfiled"  # virtual folder: pins not assigned anywhere (#64)


def state_dir():
    return config.STATE_DIR


def _atomic_json(path, data):
    """Atomic bounded-JSON write (#17 pattern): temp file in the same
    directory, fsync, rename. Never raises."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception:
        return False


def backups_dir():
    return os.path.join(state_dir(), "backups")


def display_state_file():
    return os.path.join(state_dir(), "display.json")


def audio_state_file():
    return os.path.join(state_dir(), "audio.json")


def load_audio_state():
    """Persisted audio preferences (#94/#95): selected sink, sink volume
    and mute, per-app defaults. Defensive and bounded like settings.json."""
    out = {"sink": "", "volume": None, "mute": None, "app_prefs": {}}
    try:
        with open(audio_state_file()) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if isinstance(raw.get("sink"), str) and len(raw["sink"]) <= 120:
                out["sink"] = raw["sink"]
            if isinstance(raw.get("volume"), (int, float)):
                out["volume"] = max(0, min(150, int(raw["volume"])))
            if isinstance(raw.get("mute"), bool):
                out["mute"] = raw["mute"]
            prefs = raw.get("app_prefs")
            if isinstance(prefs, dict):
                ap = {}
                for k, v in list(prefs.items())[:50]:
                    if not isinstance(k, str) or not k or len(k) > 120 or not isinstance(v, dict):
                        continue
                    e = {}
                    if isinstance(v.get("volume"), (int, float)):
                        e["volume"] = max(0, min(150, int(v["volume"])))
                    if isinstance(v.get("mute"), bool):
                        e["mute"] = v["mute"]
                    if e:
                        ap[k] = e
                out["app_prefs"] = ap
    except Exception:
        pass
    return out


def save_audio_state(data):
    """Persist audio preferences (never raises)."""
    return _atomic_json(audio_state_file(), data)


# ------------------------------------------------------------------ pins
#
# #64: pins grow named folders. File format v2 is
#   {"version": 2, "order": [app id, ...], "folders": [{"name": ..., "apps": [...]}]}
# v1 (a flat JSON list) is migrated on first load: existing pins surface in
# the virtual "Unfiled" folder without any data change. A pin lives in the
# `order` list exactly once and belongs to at most one folder.


def _clean_folder(f):
    """Validate one folder record; None when it should be dropped."""
    if not isinstance(f, dict):
        return None
    name = str(f.get("name") or "").strip()[:PIN_NAME_MAX]
    if not name or name.lower() == UNFILED.lower():
        return None  # "Unfiled" is virtual, never stored
    apps = []
    for a in f.get("apps", []):
        if isinstance(a, str) and a and len(a) < 200 and a not in apps:
            apps.append(a)
    return {"name": name, "apps": apps[:100]}


def _load_pins_v2():
    try:
        with open(PINS_FILE) as f:
            raw = json.load(f)
    except Exception:
        return {"version": 2, "order": [], "folders": []}
    if isinstance(raw, list):  # v1 flat list — appear under "Unfiled" at once
        order = [p for p in raw if isinstance(p, str) and p and len(p) < 200][:100]
        return {"version": 2, "order": order, "folders": []}
    if isinstance(raw, dict) and raw.get("version") == 2:
        order = [p for p in raw.get("order", []) if isinstance(p, str) and p and len(p) < 200][:100]
        seen_names = set()
        folders = []
        for f in raw.get("folders", []):
            cf = _clean_folder(f)
            if cf and cf["name"].lower() not in seen_names:
                seen_names.add(cf["name"].lower())
                folders.append(cf)
        return {"version": 2, "order": order, "folders": folders[:PIN_FOLDERS_MAX]}
    return {"version": 2, "order": [], "folders": []}


def _save_pins_v2(data):
    _atomic_json(PINS_FILE, data)


def _folder_of(data, app_id):
    for f in data["folders"]:
        if app_id in f["apps"]:
            return f["name"]
    return None


def load_pins():
    """Back-compat flat ordered pin list."""
    return _load_pins_v2()["order"]


def pins_snapshot():
    """Pins with folders (#64); "Unfiled" is computed, never stored."""
    data = _load_pins_v2()
    unfiled = [p for p in data["order"] if _folder_of(data, p) is None]
    folders = [{"name": UNFILED, "apps": unfiled, "virtual": True}]
    folders += [{"name": f["name"], "apps": f["apps"]} for f in data["folders"]]
    return {"pins": data["order"], "folders": folders}


def save_pins(pins):
    """Replace the flat pin order (keeps folders that still reference valid pins)."""
    pins = [p for p in pins if isinstance(p, str) and len(p) < 200][:100]
    data = _load_pins_v2()
    data["order"] = pins
    for f in data["folders"]:
        f["apps"] = [a for a in f["apps"] if a in set(pins)]
    _save_pins_v2(data)


def pins_toggle(app_id):
    """Add/remove a pin. Returns the new snapshot or None for a bad id."""
    if not app_id or len(app_id) > 200:
        return None
    data = _load_pins_v2()
    if app_id in data["order"]:
        data["order"].remove(app_id)
        for f in data["folders"]:
            f["apps"] = [a for a in f["apps"] if a != app_id]
    else:
        data["order"].insert(0, app_id)
    _save_pins_v2(data)
    return pins_snapshot()


def pins_reorder(ids):
    """Set the pin order (#64). Unknown ids are appended, missing ones kept."""
    if not isinstance(ids, list):
        return None
    ids = [str(i)[:200] for i in ids if isinstance(i, str) and i]
    data = _load_pins_v2()
    known = [p for p in data["order"] if p not in set(ids)]
    data["order"] = (ids + known)[:100]
    _save_pins_v2(data)
    return pins_snapshot()


def pins_folder_create(name):
    """Create a pin folder (#64). Returns the snapshot or an error key."""
    if not isinstance(name, str):
        return {"error": "invalid name"}
    name = name.strip()[:PIN_NAME_MAX]
    if not name or name.lower() == UNFILED.lower():
        return {"error": "invalid name"}
    data = _load_pins_v2()
    if any(f["name"].lower() == name.lower() for f in data["folders"]):
        return {"error": "exists"}
    if len(data["folders"]) >= PIN_FOLDERS_MAX:
        return {"error": "too many folders"}
    data["folders"].append({"name": name, "apps": []})
    _save_pins_v2(data)
    return pins_snapshot()


def pins_folder_delete(name):
    """Delete a folder; its pins stay pinned (back under Unfiled) (#64)."""
    data = _load_pins_v2()
    data["folders"] = [f for f in data["folders"] if f["name"] != name]
    _save_pins_v2(data)
    return pins_snapshot()


def pins_assign(app_id, folder):
    """Assign a pin to at most one folder ("" = unfile) (#64)."""
    if not app_id or len(app_id) > 200 or not isinstance(folder, str):
        return {"error": "invalid"}
    data = _load_pins_v2()
    if app_id not in data["order"]:
        return {"error": "not pinned"}
    folder = folder.strip()[:PIN_NAME_MAX]
    if folder and folder.lower() != UNFILED.lower():
        if not any(f["name"] == folder for f in data["folders"]):
            r = pins_folder_create(folder)
            if r.get("error"):
                return r
        data = _load_pins_v2()
        for f in data["folders"]:
            if app_id in f["apps"]:
                f["apps"].remove(app_id)
        for f in data["folders"]:
            if f["name"] == folder:
                f["apps"].insert(0, app_id)
    else:
        for f in data["folders"]:
            if app_id in f["apps"]:
                f["apps"].remove(app_id)
    _save_pins_v2(data)
    return pins_snapshot()


# ------------------------------------------------------------- display state

GEOMETRY_MIN = (320, 240)
GEOMETRY_MAX = (7680, 4320)


def save_display_state(w, h, dpi=None):
    """Persist the last client-requested size, bounded (#31)."""
    w = max(GEOMETRY_MIN[0], min(GEOMETRY_MAX[0], int(w)))
    h = max(GEOMETRY_MIN[1], min(GEOMETRY_MAX[1], int(h)))
    data = {"geometry": f"{w}x{h}", "dpi": int(dpi or config.DPI), "updated": int(time.time())}
    try:
        os.makedirs(state_dir(), exist_ok=True)
        tmp = display_state_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, display_state_file())
        return data
    except Exception as e:
        log.log(f"display state save failed: {e}")
        return None


def load_display_state():
    try:
        with open(display_state_file()) as f:
            d = json.load(f)
        if isinstance(d.get("geometry"), str) and "x" in d["geometry"]:
            return d
    except Exception:
        pass
    return None


# ------------------------------------------------------------- app settings
#
# #72/#76: small opt-in feature toggles, persisted server-side so every
# client (and the server itself) agrees on them. Booleans only.

SETTINGS_DEFAULTS = {"clip_sync": False, "geo_remember": True}
SETTINGS_KEYS = tuple(SETTINGS_DEFAULTS)


def settings_file():
    return os.path.join(state_dir(), "settings.json")


def load_settings():
    out = dict(SETTINGS_DEFAULTS)
    try:
        with open(settings_file()) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in SETTINGS_DEFAULTS:
                if isinstance(raw.get(k), bool):
                    out[k] = raw[k]
    except Exception:
        pass
    return out


def save_setting(key, value):
    """Set one boolean toggle. Returns the new settings dict or None."""
    if key not in SETTINGS_DEFAULTS or not isinstance(value, bool):
        return None
    cur = load_settings()
    cur[key] = value
    _atomic_json(settings_file(), cur)
    return cur


# ------------------------------------------------------------ launch history
#
# #62: launch timestamps feed the launcher ranking. Capped, atomic, purely
# local (privacy-safe: app ids only, no arguments, no timestamps beyond
# last-used).

LAUNCH_HISTORY_MAX = 500
LAUNCH_RECENT_DAYS = 7


def launch_history_file():
    return os.path.join(state_dir(), "launch-history.json")


def load_launches():
    try:
        with open(launch_history_file()) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            out = []
            for e in raw:
                if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]:
                    try:
                        ts = int(e.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0
                    out.append({"id": e["id"][:200], "ts": ts})
            return out
    except Exception:
        pass
    return []


def record_launch(app_id):
    """Record one launch (most recent first, capped at 500 entries) (#62)."""
    if not isinstance(app_id, str) or not app_id or len(app_id) > 200:
        return False
    items = [e for e in load_launches() if e["id"] != app_id]
    items.insert(0, {"id": app_id, "ts": int(time.time())})
    return _atomic_json(launch_history_file(), items[:LAUNCH_HISTORY_MAX])


def clear_launches():
    """Wipe launch history; pins and the app list are untouched (#62)."""
    return _atomic_json(launch_history_file(), [])


def recent_launches(days=LAUNCH_RECENT_DAYS):
    """app id -> last launch ts within the window, newest-first deduped (#62)."""
    cutoff = time.time() - days * 86400
    out = {}
    for e in load_launches():
        if e["ts"] >= cutoff and e["id"] not in out:
            out[e["id"]] = e["ts"]
    return out


# --------------------------------------------------------------- custom apps
#
# #63: user-defined launcher entries (no .desktop file needed). Commands are
# argv VECTORS launched without a shell — validation lives in apps.py; this
# module only stores validated entries.

CUSTOM_APPS_MAX = 50


def custom_apps_file():
    return os.path.join(state_dir(), "custom-apps.json")


def load_custom_apps():
    try:
        with open(custom_apps_file()) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [e for e in raw if isinstance(e, dict) and isinstance(e.get("cmd"), list)]
    except Exception:
        pass
    return []


def save_custom_apps(entries):
    return _atomic_json(custom_apps_file(), entries[:CUSTOM_APPS_MAX])


def _file_sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def snapshot(reason="startup"):
    """Copy the live state files into a versioned backup dir and prune old
    ones (keep BACKUP_KEEP). Returns the backup dir path or None."""
    srcs = {
        name: path
        for name, path in (
            ("pins", PINS_FILE),
            ("cliphistory", _clipboard.CLIP_HISTORY_FILE),
            ("custom-apps", custom_apps_file()),  # #63: custom entries travel with backups
        )
        if os.path.isfile(path)
    }
    arc_names = {"pins": _PIN_NAME, "cliphistory": _CLIP_NAME, "custom-apps": _CUSTOM_NAME}
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(backups_dir(), f"v{STATE_VERSION}-{ts}-{reason}")
    try:
        os.makedirs(bdir, exist_ok=True)
        manifest = {
            "state_version": STATE_VERSION,
            "created": int(time.time()),
            "reason": reason,
            "app_version": config.VERSION,
            "files": {},
        }
        for name, src in srcs.items():
            dst = os.path.join(bdir, arc_names[name])
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            manifest["files"][name] = {
                "file": os.path.basename(dst),
                "bytes": os.path.getsize(dst),
                "sha256": _file_sha256(dst),
            }
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as f:
            json.dump(manifest, f, indent=1)
        os.chmod(mpath, 0o600)
        _prune_backups()
        return bdir
    except Exception as e:
        log.log(f"state snapshot failed: {e}")
        return None


def _prune_backups(keep=BACKUP_KEEP):
    bdir = backups_dir()
    try:
        entries = sorted(e for e in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, e)))
    except OSError:
        return
    if len(entries) > keep:
        for e in entries[: len(entries) - keep]:
            shutil.rmtree(os.path.join(bdir, e), ignore_errors=True)


def list_backups():
    bdir = backups_dir()
    out = []
    try:
        for e in sorted(os.listdir(bdir), reverse=True):
            mpath = os.path.join(bdir, e, "manifest.json")
            info = {"dir": e}
            try:
                with open(mpath) as f:
                    info["manifest"] = json.load(f)
            except Exception:
                info["manifest"] = None
            out.append(info)
    except OSError:
        pass
    return out


def export_state(path, include_secrets=False):
    """Write a versioned, atomic, 0600 state archive (#30).

    Includes pins and clipboard history only; secrets (the password hash)
    are never included — move .passhash by hand if you truly mean to."""
    del include_secrets  # accepted for CLI symmetry; secrets stay excluded
    tmpd = tempfile.mkdtemp(prefix="rd-export-")
    try:
        copied = []
        if os.path.isfile(PINS_FILE):
            shutil.copy2(PINS_FILE, os.path.join(tmpd, _PIN_NAME))
            copied.append(_PIN_NAME)
        clip = _clipboard.CLIP_HISTORY_FILE
        if os.path.isfile(clip):
            shutil.copy2(clip, os.path.join(tmpd, _CLIP_NAME))
            copied.append(_CLIP_NAME)
        if os.path.isfile(custom_apps_file()):  # #63
            shutil.copy2(custom_apps_file(), os.path.join(tmpd, _CUSTOM_NAME))
            copied.append(_CUSTOM_NAME)
        manifest = {
            "state_version": STATE_VERSION,
            "created": int(time.time()),
            "app_version": config.VERSION,
            "includes_secrets": False,
            "files": {n: {"sha256": _file_sha256(os.path.join(tmpd, n))} for n in copied},
        }
        with open(os.path.join(tmpd, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)
        abs_out = os.path.abspath(path)
        fd, tmp_out = tempfile.mkstemp(dir=os.path.dirname(abs_out), prefix=".export-", suffix=".tar.gz")
        os.close(fd)
        with tarfile.open(tmp_out, "w:gz") as tar:
            for n in copied + ["manifest.json"]:
                tar.add(os.path.join(tmpd, n), arcname=n)
        os.chmod(tmp_out, 0o600)
        os.replace(tmp_out, abs_out)  # atomic
        return {"ok": True, "path": abs_out, "files": copied}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def import_state(path):
    """Import a state archive written by export_state (#30).

    Refuses — without touching live state — a newer schema version, a
    corrupt archive or a missing manifest. Returns {"ok", "imported": [...]}."""
    try:
        if not os.path.isfile(path):
            return {"ok": False, "error": f"no such archive: {path}"}
        staged = []
        with tarfile.open(path, "r:gz") as tar:
            m = tar.extractfile("manifest.json")
            if m is None:
                return {"ok": False, "error": "archive has no manifest"}
            manifest = json.loads(m.read().decode())
            try:
                version = int(manifest.get("state_version", -1))
            except (TypeError, ValueError):
                version = -1
            if version > STATE_VERSION:
                return {
                    "ok": False,
                    "error": f"archive schema v{version} is newer than this server (v{STATE_VERSION}); upgrade first",
                }
            if version < 1:
                return {"ok": False, "error": "archive manifest has no valid state_version"}
            members = {mm.name for mm in tar.getmembers()}
            for arcname, target in (
                (_PIN_NAME, PINS_FILE),
                (_CLIP_NAME, _clipboard.CLIP_HISTORY_FILE),
                (_CUSTOM_NAME, custom_apps_file()),  # #63
            ):
                if arcname not in members:
                    continue
                data = tar.extractfile(arcname).read()
                json.loads(data.decode())  # raises on corrupt payloads
                name = {_PIN_NAME: "pins", _CLIP_NAME: "cliphistory", _CUSTOM_NAME: "custom-apps"}.get(
                    arcname, "custom-apps"
                )
                staged.append((data, target, name))
        # everything validated — replace live state atomically
        imported = []
        for data, target, name in staged:
            tmp = target + ".import.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                os.fsync(f.fileno())
            os.replace(tmp, target)
            os.chmod(target, 0o600)
            imported.append(name)
        return {"ok": True, "imported": imported}
    except Exception as e:
        return {"ok": False, "error": f"archive rejected: {str(e)[:160]}"}
