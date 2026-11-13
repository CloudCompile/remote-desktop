"""Runtime state: pins, display geometry state and versioned backups (#30).

Ownership: everything the server persists between runs.
- Pins: BASE_DIR/.pins.json (file-backed state, ADR-0005).
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
from . import config, log

STATE_VERSION = 1
BACKUP_KEEP = 5

PINS_FILE = os.path.join(config.BASE_DIR, ".pins.json")

# relative names inside backups/archives
_PIN_NAME = ".pins.json"
_CLIP_NAME = ".cliphistory.json"


def state_dir():
    return config.STATE_DIR


def backups_dir():
    return os.path.join(state_dir(), "backups")


def display_state_file():
    return os.path.join(state_dir(), "display.json")


# ------------------------------------------------------------------ pins


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


# ------------------------------------------------------------------ backups


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
        for name, path in (("pins", PINS_FILE), ("cliphistory", _clipboard.CLIP_HISTORY_FILE))
        if os.path.isfile(path)
    }
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
            dst = os.path.join(bdir, _PIN_NAME if name == "pins" else _CLIP_NAME)
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
            for name, target in (("pins", PINS_FILE), ("cliphistory", _clipboard.CLIP_HISTORY_FILE)):
                if name not in members:
                    continue
                data = tar.extractfile(name).read()
                json.loads(data.decode())  # raises on corrupt payloads
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
