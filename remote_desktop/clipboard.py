"""Clipboard: X selection access and multi-item clipboard history.

Ownership: xclip get/set (CLIPBOARD + PRIMARY), the persisted history
store (.cliphistory.json) and the background selection watcher. Clipboard
CONTENTS are never logged (#24 privacy) — only item counts and ids.

History items (#70 #71): {id, text, time, source, kind ("text"|"image"|
"uri"), pinned: bool, tags: [..]} plus, for images, {path, preview}
(a ≤ 64 KB data URI). The format is additive — missing fields default on
load, so v2.2 histories read cleanly.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

from . import config, logging as log

CLIP_HISTORY_FILE = os.path.join(config.BASE_DIR, ".cliphistory.json")
CLIP_HISTORY_MAX = 100
CLIP_ITEM_MAX = 20000
CLIP_TAGS_MAX = 8  # #70: tags per item
CLIP_TAG_MAX = 24  # #70: characters per tag
CLIP_IMAGE_DIR = os.path.join(config.HOME, ".cache", "remote-clipboard")  # #71
CLIP_IMAGE_MAX = 5 * 1024 * 1024  # #71: 5 MB cap, larger images are skipped
CLIP_PREVIEW_MAX = 64 * 1024  # #71: data-URI budget per image item
CLIP_SYNC_MAX = 10  # #72: items per sync poll
HISTORIES_MAX = 64  # in-memory per-user histories (#120); least-recently used evicted

_clip_history = []  # the LEGACY/default history (default admin; None user key)
_histories = {None: _clip_history}  # per-user in-memory histories (#120)
_hist_touch = {}  # user -> last access (LRU for eviction)
_clip_lock = threading.Lock()
_clip_last_sel = {"clipboard": None, "primary": None}
_clip_last_image_sha = None
_clip_last_uri = None


def _ns(user):
    """Namespace key for a request's user (#120): the default admin (and
    legacy callers) share the original paths; everyone else gets a
    per-user history."""
    from . import users

    if not user or users.is_default_admin(user):
        return None
    return str(user)[:64]


def _hist(user):
    """The in-memory history for `user` (loaded from disk on first use)."""
    ns = _ns(user)
    if ns is None:
        return _clip_history
    h = _histories.get(ns)
    if h is None:
        _histories[ns] = h = _clip_load_file(_hist_file(user))
    _hist_touch[ns] = time.time()
    if len(_histories) > HISTORIES_MAX:  # evict the least-recently used
        for k in sorted(_hist_touch, key=_hist_touch.get)[: len(_histories) - HISTORIES_MAX]:
            if k is not None and k in _histories:
                with _clip_lock:
                    stale = _histories.pop(k)
                    _clip_save_file(_hist_file(k), stale)
                _hist_touch.pop(k, None)
    return h


def _hist_file(user):
    """Persistent history file for `user` (legacy path for the default admin)."""
    if _ns(user) is None:
        return CLIP_HISTORY_FILE
    from . import users

    return os.path.join(users.user_dir(user), "cliphistory.json")


def _run_xclip(cmd, timeout=4):
    try:
        p = subprocess.run(cmd, env=config._env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def _run_capture_bytes(cmd, timeout=8):
    """subprocess.run for BINARY output (xclip -t image/png, ffmpeg)."""
    try:
        p = subprocess.run(cmd, env=config._env(), capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, b"", str(e).encode()


def _clip_get(selection):
    rc, out, _ = _run_xclip(["xclip", "-o", "-selection", selection], timeout=4)
    return out if rc == 0 else ""


def _clip_targets(selection="clipboard"):
    """The selection's offered TARGETS (#71). Empty list when unavailable."""
    rc, out, _ = _run_xclip(["xclip", "-selection", selection, "-t", "TARGETS", "-o"], timeout=4)
    return out.split() if rc == 0 else []


def _clip_get_bytes(selection, target):
    rc, out, _ = _run_capture_bytes(["xclip", "-selection", selection, "-t", target, "-o"], timeout=4)
    return rc, out


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


def clipboard_set(text, user=None):
    """Write the text into BOTH X selections so any app can paste it
    (Ctrl+V apps read CLIPBOARD, terminals/middle-click read PRIMARY)."""
    if not text:
        return {"ok": False, "error": "empty text"}
    ok_cb = _clip_set("clipboard", text)
    ok_prim = _clip_set("primary", text)
    clip_add(text, "phone", user=user)
    return {"ok": ok_cb or ok_prim}


# ------------------------------------------------------------------ images
# #71: screenshots live on the clipboard as image/png. Stored under
# ~/.cache/remote-clipboard/<sha>.png (5 MB cap) with a small data-URI
# preview in the history item.


def clip_image_path(clip_id, user=None):
    """Validated on-disk path of a stored image item, or None (the item
    must be in THIS user's history, #120)."""
    it = clip_item(clip_id, user)
    if not it or it.get("kind") != "image":
        return None
    p = it.get("path") or ""
    if not p:
        return None
    real = os.path.realpath(p)
    if os.path.dirname(real) != os.path.realpath(CLIP_IMAGE_DIR) or not os.path.isfile(real):
        return None
    return real


def clipboard_set_image(item):
    """Paste an image back to the X clipboard (#71): xclip -t image/png."""
    path = clip_image_path(item.get("id", "")) if isinstance(item, dict) else None
    if not path:
        return {"ok": False, "error": "image no longer available"}
    ok = True
    try:
        proc = subprocess.Popen(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=config._env(),
            start_new_session=True,
        )
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            ok = False
        ok = ok and proc.returncode == 0
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": ok}


def _ffmpeg_png_preview(path, width):
    rc, out, _ = _run_capture_bytes(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            path,
            "-vf",
            f"scale={width}:-2",
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-q:v",
            "7",
            "-f",
            "image2",
            "-",
        ],
        timeout=8,
    )
    return out if rc == 0 else b""


def _image_preview(path, raw):
    """≤ 64 KB data-URI preview of a stored PNG (#71); "" when nothing
    workable can be produced (text-only deployments)."""
    try:
        if raw and len(raw) <= 48 * 1024:
            return "data:image/png;base64," + base64.b64encode(raw).decode()
        if shutil.which("ffmpeg") is None:
            return ""
        for width in (320, 160, 96):
            data = _ffmpeg_png_preview(path, width)
            if data:
                uri = "data:image/jpeg;base64," + base64.b64encode(data).decode()
                if len(uri) <= CLIP_PREVIEW_MAX:
                    return uri
        return ""
    except Exception:
        return ""


def clip_add_image(data):
    """Store clipboard image bytes as a history item (#71). Returns the
    item, or a visible 'skipped' notice for oversized images, or None."""
    global _clip_last_image_sha
    if not data:
        return None
    sha = hashlib.sha256(data).hexdigest()[:16]
    _clip_last_image_sha = sha
    if len(data) > CLIP_IMAGE_MAX:
        return {
            "id": sha,
            "kind": "image",
            "skipped": True,
            "text": "",
            "len": len(data),
            "time": int(time.time()),
            "source": "desktop",
            "pinned": False,
            "tags": [],
        }
    try:
        os.makedirs(CLIP_IMAGE_DIR, exist_ok=True)
        path = os.path.join(CLIP_IMAGE_DIR, sha + ".png")
        if not os.path.isfile(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        preview = _image_preview(path, data)
    except Exception as e:
        log.log(f"clipboard image store failed: {e}")
        return None
    return {
        "id": sha,
        "kind": "image",
        "path": path,
        "preview": preview,
        "text": "",
        "len": len(data),
        "time": int(time.time()),
        "source": "desktop",
        "pinned": False,
        "tags": [],
    }


def clip_add(text, source, kind="text", user=None):
    """Record an item. `user=None` is the legacy/desktop history; the X
    watcher always records there (the desktop stream), while API access
    is scoped per user (#120)."""
    if not text:
        return
    text = text[:CLIP_ITEM_MAX]
    with _clip_lock:
        h = _hist(user)
        if h and h[0]["text"] == text and h[0].get("kind", "text") == kind:
            h[0]["time"] = int(time.time())  # bump recency
        else:
            h.insert(
                0,
                {
                    "id": hashlib.sha1(f"{time.time_ns()}:{text[:64]}".encode()).hexdigest()[:12],
                    "text": text,
                    "time": int(time.time()),
                    "source": source,
                    "kind": kind,
                    "pinned": False,
                    "tags": [],
                },
            )
        _trim_locked(h)
        _clip_save_file(_hist_file(user), h)


def _trim_locked(h):
    """Rolling history limit; pinned items are exempt (#70)."""
    if len(h) <= CLIP_HISTORY_MAX:
        return
    kept, unpinned = [], 0
    for it in h:  # newest first
        if it.get("pinned"):
            kept.append(it)
        elif unpinned < CLIP_HISTORY_MAX:
            kept.append(it)
            unpinned += 1
    h[:] = kept


def _clip_save_file(path, items):
    """Atomic write (#17): dump to a temp file in the same directory, fsync,
    then rename over the live file so a crash or full disk can never
    truncate the history file."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        pass


def _clip_save(user=None):
    _clip_save_file(_hist_file(user), _hist(user))


def _clip_load_file(path):
    """Load one history file defensively (#17): validate the JSON
    structure, coerce/skip bad entries, default the additive fields
    (#70/#71), deduplicate, trim to the maximum, and quarantine a corrupt
    file instead of crashing or losing the good copy."""
    loaded = []
    try:
        with open(path) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            seen = set()
            for it in raw:
                if not isinstance(it, dict):
                    continue
                kind = it.get("kind") or "text"
                text = it.get("text") if isinstance(it.get("text"), str) else ""
                if kind in ("text", "uri") and not text:
                    continue
                cid = it.get("id") or hashlib.sha1(f"{time.time_ns()}:{text[:64]}".encode()).hexdigest()[:12]
                if cid in seen:
                    continue
                seen.add(cid)
                tags = [str(t)[:CLIP_TAG_MAX] for t in it.get("tags", []) if isinstance(t, str) and t.strip()][
                    :CLIP_TAGS_MAX
                ]
                entry = {
                    "id": str(cid)[:64],
                    "text": text[:CLIP_ITEM_MAX],
                    "time": int(it.get("time") or 0),
                    "source": str(it.get("source") or "desktop")[:16],
                    "kind": kind if kind in ("text", "image", "uri") else "text",
                    "pinned": bool(it.get("pinned")),
                    "tags": tags,
                }
                if entry["kind"] == "image":
                    entry["path"] = str(it.get("path") or "")[:400]
                    entry["preview"] = str(it.get("preview") or "")[: CLIP_PREVIEW_MAX + 16]
                    if it.get("skipped"):
                        entry["skipped"] = True
                    entry["len"] = int(it.get("len") or 0)
                    if not entry["path"] and not entry["preview"] and not entry.get("skipped"):
                        continue  # nothing left to show for this image
                loaded.append(entry)
    except (ValueError, OSError):
        try:
            os.replace(path, path + f".corrupt-{int(time.time())}")
            log.log("clipboard history was corrupt; quarantined and starting empty")
        except Exception:
            pass
    return loaded


def _clip_load(user=None):
    """(Re)load a history from disk into memory (#17)."""
    global _clip_history
    ns = _ns(user)
    loaded = _clip_load_file(_hist_file(user))
    with _clip_lock:
        if ns is None:
            _clip_history[:] = loaded
            _trim_locked(_clip_history)
        else:
            _histories[ns] = loaded
            _trim_locked(_histories[ns])
            _hist_touch[ns] = time.time()


def clip_history(q=None, tag=None, user=None):
    """History for the UI (#70): pinned items first (then newest), filtered
    by a full-text substring (searches the WHOLE text, not just the preview)
    and by tag. Image previews ride along for the newest few image items.
    Scoped to the request's user (#120)."""
    with _clip_lock:
        items = list(_hist(user))
    if tag:
        tl = str(tag).lower().lstrip("#")
        items = [it for it in items if tl in [t.lower() for t in it.get("tags", [])]]
    if q:
        ql = str(q).lower()
        items = [it for it in items if ql in (it.get("text") or "").lower()]
    items.sort(key=lambda it: (0 if it.get("pinned") else 1, -int(it.get("time") or 0)))
    out, previews = [], 0
    for it in items:
        d = {
            "id": it["id"],
            "preview": (it["text"][:160] + ("…" if len(it["text"]) > 160 else "")) if it.get("text") else "",
            "len": it.get("len") if it.get("len") is not None else len(it.get("text") or ""),
            "time": it["time"],
            "source": it["source"],
            "kind": it.get("kind", "text"),
            "pinned": bool(it.get("pinned")),
            "tags": it.get("tags", []),
        }
        if d["kind"] == "image":
            if d["preview"] == "":
                d["preview"] = "(image skipped — too large)" if it.get("skipped") else "(image)"
            if previews < 4:  # bounded response: inline previews for the newest few
                d["preview_image"] = it.get("preview", "")
                previews += 1
            else:
                d["preview_image"] = ""  # UI lazy-loads /api/clipboard/image instead
        out.append(d)
    return out


def clip_tags(user=None):
    """All tags in use, for the filter chips (#70)."""
    with _clip_lock:
        tags = []
        for it in _hist(user):
            for t in it.get("tags", []):
                if t.lower() not in [x.lower() for x in tags]:
                    tags.append(t)
    return tags


def clip_item(clip_id, user=None):
    with _clip_lock:
        return next((dict(it) for it in _hist(user) if it["id"] == clip_id), None)


def clip_pin(clip_id, pinned, user=None):
    """Pin/unpin one item; pinned items survive trimming (#70)."""
    with _clip_lock:
        it = next((i for i in _hist(user) if i["id"] == clip_id), None)
        if not it:
            return False
        it["pinned"] = bool(pinned)
        _clip_save(user)
        return True


def clip_tag(clip_id, tags, user=None):
    """Replace an item's tag list (#70). Accepts a list or a comma string."""
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        return False
    clean, lower = [], set()
    for t in tags:
        t = str(t).strip().lstrip("#")[:CLIP_TAG_MAX]
        if t and t.lower() not in lower:
            clean.append(t)
            lower.add(t.lower())
        if len(clean) >= CLIP_TAGS_MAX:
            break
    with _clip_lock:
        it = next((i for i in _hist(user) if i["id"] == clip_id), None)
        if not it:
            return False
        it["tags"] = clean
        _clip_save(user)
        return True


def clip_remove(clip_id, user=None):
    with _clip_lock:
        h = _hist(user)
        h[:] = [it for it in h if it["id"] != clip_id]
        _clip_save(user)


def clip_clear(keep_pinned=True, user=None):
    """Bulk clear (#70): pinned items survive by default. Scoped per user (#120)."""
    with _clip_lock:
        h = _hist(user)
        if keep_pinned:
            h[:] = [it for it in h if it.get("pinned")]
        else:
            h.clear()
        _clip_save(user)


def clip_sync(since_id, user=None):
    """Desktop→phone push channel (#72): served only while the opt-in
    toggle is on (consent lives in the More sheet, stored in state).
    Returns the newest items after `since_id` for the phone to write into
    navigator.clipboard (best-effort, poll every 5 s while visible).
    Scoped per user (#120)."""
    try:
        from . import state

        enabled = state.load_settings().get("clip_sync", False)
    except Exception:
        enabled = False
    if not enabled:
        return {"enabled": False, "items": []}
    with _clip_lock:
        items = list(_hist(user))
    if since_id:
        idx = next((i for i, it in enumerate(items) if it["id"] == since_id), None)
        # an unknown/trimmed since id falls back to the newest item so the
        # phone can never deadlock on a stale cursor
        new = items[:idx] if idx is not None else items[:1]
    else:
        new = items[:1]  # first poll: at most the newest item
    out = [
        {"id": it["id"], "text": it["text"], "kind": it.get("kind", "text")}
        for it in new
        if it.get("kind", "text") == "text" and it["text"]
    ][:CLIP_SYNC_MAX]
    return {"enabled": True, "items": out}


def flush():
    """Persist every live history before shutdown (#25)."""
    with _clip_lock:
        for ns, h in _histories.items():
            user = None if ns is None else ns
            _clip_save_file(_hist_file(user), h)


def clipboard_watcher(stop_event=None):
    """Background: watch the X selections so anything copied inside the remote
    desktop (Ctrl+C in apps, plain selection in terminals) lands in history —
    including images and file URI lists (#71)."""
    global _clip_last_uri
    while not (stop_event and stop_event.is_set()):
        time.sleep(1.5)
        try:
            cb = _clip_get("clipboard")
            prim = _clip_get("primary")
            if _clip_last_sel["clipboard"] is None and _clip_last_sel["primary"] is None and not cb and not prim:
                # first look on an empty desktop: nothing to record
                _clip_last_sel["clipboard"] = cb
                _clip_last_sel["primary"] = prim
            else:
                # record each selection that changed (both can change at once)
                if cb and cb != _clip_last_sel["clipboard"]:
                    clip_add(cb, "desktop")
                if prim and prim != _clip_last_sel["primary"] and prim != cb:
                    clip_add(prim, "desktop")
                _clip_last_sel["clipboard"] = cb
                _clip_last_sel["primary"] = prim
            # images (#71): the clipboard may hold a screenshot even when the
            # text targets are empty
            if "image/png" in _clip_targets():
                rc, data = _clip_get_bytes("clipboard", "image/png")
                if rc == 0 and data:
                    item = clip_add_image(data)
                    if item:
                        with _clip_lock:
                            _clip_history.insert(0, item)
                            _trim_locked()
                            _clip_save()
            # file URIs (#71): copied files arrive as text/uri-list
            rc, out, _ = _run_xclip(["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-o"], timeout=4)
            txt = out.strip() if rc == 0 else ""
            if txt and txt != _clip_last_uri:
                dup = False
                with _clip_lock:
                    dup = bool(_clip_history) and _clip_history[0].get("text", "") == txt[:CLIP_ITEM_MAX]
                if not dup:
                    _clip_last_uri = txt
                    clip_add(txt, "desktop", kind="uri")
                else:
                    _clip_last_uri = txt
        except Exception:
            pass


def prime_watcher():
    """Prime the watcher with whatever is currently selected so a restart
    doesn't re-record the existing clipboard as a "new" history entry."""
    global _clip_last_image_sha
    try:
        _clip_last_sel["clipboard"] = _clip_get("clipboard")
        _clip_last_sel["primary"] = _clip_get("primary")
        if "image/png" in _clip_targets():
            rc, data = _clip_get_bytes("clipboard", "image/png")
            if rc == 0 and data:
                _clip_last_image_sha = hashlib.sha256(data).hexdigest()[:16]
    except Exception:
        pass
