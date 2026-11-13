"""Clipboard: X selection access and multi-item clipboard history.

Ownership: xclip get/set (CLIPBOARD + PRIMARY), the persisted history
store (.cliphistory.json) and the background selection watcher. Clipboard
CONTENTS are never logged (#24 privacy) — only item counts and ids.
"""

import hashlib
import json
import os
import subprocess
import threading
import time

from . import config, logging as log

CLIP_HISTORY_FILE = os.path.join(config.BASE_DIR, ".cliphistory.json")
CLIP_HISTORY_MAX = 100
CLIP_ITEM_MAX = 20000

_clip_history = []
_clip_lock = threading.Lock()
_clip_last_sel = {"clipboard": None, "primary": None}


def _clip_get(selection):
    rc, out, _ = _run_xclip(["xclip", "-o", "-selection", selection], timeout=4)
    return out if rc == 0 else ""


def _run_xclip(cmd, timeout=4):
    try:
        p = subprocess.run(cmd, env=config._env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def _clip_set(selection, text):
    # NOTE: xclip -i forks a background process that OWNS the selection and
    # stays alive. If we give it pipes, subprocess waits for the fork to
    # close them and times out. So: no pipes, detached, wait only for the
    # immediate parent (which exits after handing off to the owner).
    try:
        p = subprocess.Popen(
            ["xclip", "-selection", selection, "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=config._env(),
            start_new_session=True,
            text=True,
        )
        p.communicate(text, timeout=4)
        return p.returncode == 0
    except Exception:
        return False


def clipboard_get():
    """Read both X selections. VNC only syncs CLIPBOARD, but terminals use PRIMARY."""
    cb = _clip_get("clipboard")
    prim = _clip_get("primary")
    return {"clipboard": cb[:100000], "primary": prim[:100000], "text": (cb or prim)[:100000]}


def clipboard_set(text):
    """Write the text into BOTH X selections so any app can paste it
    (Ctrl+V apps read CLIPBOARD, terminals/middle-click read PRIMARY)."""
    if not text:
        return {"ok": False, "error": "empty text"}
    ok_cb = _clip_set("clipboard", text)
    ok_prim = _clip_set("primary", text)
    clip_add(text, "phone")
    return {"ok": ok_cb or ok_prim}


def clip_add(text, source):
    if not text:
        return
    text = text[:CLIP_ITEM_MAX]
    with _clip_lock:
        if _clip_history and _clip_history[0]["text"] == text:
            _clip_history[0]["time"] = int(time.time())  # bump recency
        else:
            _clip_history.insert(
                0,
                {
                    "id": hashlib.sha1(f"{time.time_ns()}:{text[:64]}".encode()).hexdigest()[:12],
                    "text": text,
                    "time": int(time.time()),
                    "source": source,
                },
            )
        del _clip_history[CLIP_HISTORY_MAX:]
        _clip_save()


def _clip_save():
    """Atomic write (#17): dump to a temp file in the same directory, fsync,
    then rename over the live file so a crash or full disk can never
    truncate .cliphistory.json."""
    try:
        tmp = CLIP_HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_clip_history, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CLIP_HISTORY_FILE)
        os.chmod(CLIP_HISTORY_FILE, 0o600)
    except Exception:
        pass


def _clip_load():
    """Load the persisted history defensively (#17): validate the JSON
    structure, coerce/skip bad entries, deduplicate, trim to the maximum,
    and quarantine a corrupt file instead of crashing or losing the good
    copy."""
    global _clip_history
    loaded = []
    try:
        with open(CLIP_HISTORY_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            seen = set()
            for it in raw:
                if not isinstance(it, dict) or not isinstance(it.get("text"), str):
                    continue
                cid = it.get("id") or hashlib.sha1(f"{time.time_ns()}:{it['text'][:64]}".encode()).hexdigest()[:12]
                if cid in seen:
                    continue
                seen.add(cid)
                loaded.append(
                    {
                        "id": str(cid)[:64],
                        "text": it["text"][:CLIP_ITEM_MAX],
                        "time": int(it.get("time") or 0),
                        "source": str(it.get("source") or "desktop")[:16],
                    }
                )
    except (ValueError, OSError):
        try:
            os.replace(CLIP_HISTORY_FILE, CLIP_HISTORY_FILE + f".corrupt-{int(time.time())}")
            log.log("clipboard history was corrupt; quarantined and starting empty")
        except Exception:
            pass
    _clip_history = loaded[:CLIP_HISTORY_MAX]


def clip_history():
    with _clip_lock:
        return [
            {
                "id": it["id"],
                "preview": (it["text"][:160] + ("…" if len(it["text"]) > 160 else "")),
                "len": len(it["text"]),
                "time": it["time"],
                "source": it["source"],
            }
            for it in _clip_history
        ]


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


def flush():
    """Persist pending state before shutdown (#25)."""
    with _clip_lock:
        _clip_save()


def clipboard_watcher(stop_event=None):
    """Background: watch the X selections so anything copied inside the remote
    desktop (Ctrl+C in apps, plain selection in terminals) lands in history."""
    while not (stop_event and stop_event.is_set()):
        time.sleep(1.5)
        try:
            cb = _clip_get("clipboard")
            prim = _clip_get("primary")
            if _clip_last_sel["clipboard"] is None and _clip_last_sel["primary"] is None and not cb and not prim:
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


def prime_watcher():
    """Prime the watcher with whatever is currently selected so a restart
    doesn't re-record the existing clipboard as a "new" history entry."""
    try:
        _clip_last_sel["clipboard"] = _clip_get("clipboard")
        _clip_last_sel["primary"] = _clip_get("primary")
    except Exception:
        pass
