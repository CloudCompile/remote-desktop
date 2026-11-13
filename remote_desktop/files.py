"""File transfer: the shared upload/download directory.

Ownership: FILES_DIR, listing, upload-path sanitisation (_safe_file_path)
and stale partial-upload cleanup. Upload/download/delete HTTP routes live
in server.py; upload streaming limits are enforced there.
"""

import os
import time

from . import config

FILES_DIR = config.FILES_DIR
UPLOAD_LIMIT = 2 * 1024**3


def list_files():
    os.makedirs(FILES_DIR, exist_ok=True)
    out = []
    for fn in os.listdir(FILES_DIR):
        if fn.startswith("."):  # skip partial uploads and dotfiles
            continue
        p = os.path.join(FILES_DIR, fn)
        try:
            if os.path.isfile(p) and not os.path.islink(p):
                st = os.stat(p)
                out.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def cleanup_partial_uploads(max_age=24 * 3600):
    """Remove stale hidden partial-upload files left by crashed uploads (#15)."""
    try:
        os.makedirs(FILES_DIR, exist_ok=True)
        now = time.time()
        removed = 0
        for fn in os.listdir(FILES_DIR):
            if not fn.startswith(".upload-") or not fn.endswith(".part"):
                continue
            p = os.path.join(FILES_DIR, fn)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.remove(p)
                    removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0


def _safe_file_path(name):
    """basename-only, no traversal, no hidden files — resolve inside the files dir."""
    name = os.path.basename(name or "")
    if not name or name.startswith(".") or "\x00" in name or len(name) > 255:
        return None
    p = os.path.realpath(os.path.join(FILES_DIR, name))
    if os.path.dirname(p) != os.path.realpath(FILES_DIR):
        return None
    return p
