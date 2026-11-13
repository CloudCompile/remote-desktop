"""Files & transfer: shared roots, containment, file operations and transfers.

Ownership: everything on the filesystem side of file transfer —

- Shared roots (#84): ``RD_FILES_ROOTS`` / ``RD_FILES_DIR`` parsing, labels
  and per-root read-only flags. The default single-root install behaves
  exactly as before v2.4.0.
- Path containment (#91): every path is opened openat-style — the root is
  opened once per operation as a directory descriptor and every component
  is opened relative to the previous one with ``O_NOFOLLOW``, so a symlink
  (including one swapped in mid-request) can never redirect an operation
  outside the root. Names are validated before any filesystem call (#86):
  no separators, no control characters, no null bytes.
- Listing and operations (#79/#81): directory listing with sizes, times,
  types and permissions, mkdir/rename/move/copy/delete (single or bulk),
  sort/filter and bounded recursive search.
- Uploads (#78): the legacy single-shot upload (atomic, #15) plus chunked
  resumable sessions with per-chunk SHA-256 verification and GC.
- Downloads (#83): HTTP range requests, bounded streaming, safe headers.
- Transfer queue (#88), quota/free space (#89), chmod (#90), audit log
  (#92), archives (#85) and thumbnails (#82).

HTTP routes live in server.py; this module owns the filesystem logic and
never binds sockets or starts threads at import time.
"""

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time
import unicodedata
import zipfile

from . import config, logging as log

FILES_DIR = config.FILES_DIR
UPLOAD_LIMIT = 2 * 1024**3  # 2 GB per file, unchanged since v2.0.1
CHUNK_MAX = 8 * 1024**2  # largest single upload chunk we accept
NAME_MAX = 255
SESSIONS_MAX = 16  # active chunked upload sessions at once
SEARCH_SCAN_MAX = 4000  # entries examined per search request
SEARCH_RESULTS_MAX = 200
SEARCH_TIME_MAX = 2.0  # seconds per search request
AUDIT_MAX = 512 * 1024  # audit.jsonl rotate threshold
AUDIT_KEEP = 3  # rotated audit files kept

SESSION_TTL = 24 * 3600  # abandoned upload sessions are GC'd after this
TRANSFER_RETENTION = 24 * 3600  # completed transfer jobs kept for the UI
TRANSFER_GC_PERIOD = 900


class FilesError(Exception):
    """A file operation was refused or failed; the message is safe to show.
    Optional HTTP status for the route layer (default 400)."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- containment (#91)

_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_root_fd(root_path):
    """Open the root directory itself.

    O_NOFOLLOW refuses a symlink swapped in at the root path; the returned
    descriptor pins the real directory for the whole operation, so later
    renames underneath us cannot redirect the walk (#91 TOCTOU)."""
    try:
        return os.open(root_path, _O_DIR | _O_NOFOLLOW)
    except OSError as e:
        raise FilesError("root unavailable: " + _oserr(e)) from None


def _openat_dir(fd, name):
    try:
        return os.open(name, _O_DIR | _O_NOFOLLOW, dir_fd=fd)
    except OSError as e:
        # classify for the client (the refusal itself already happened):
        # a symlinked component reports as ENOTDIR/ELOOP — check atomically
        try:
            st = os.lstat(name, dir_fd=fd)
            if stat.S_ISLNK(st.st_mode):
                raise FilesError("refused: symlink", 403) from None
        except FileNotFoundError:
            raise FilesError("not found", 404) from None
        except OSError:
            if e.errno == errno.ELOOP:
                raise FilesError("refused: symlink", 403) from None
            raise FilesError("cannot open: " + _oserr(e)) from None
        if e.errno == errno.ENOTDIR:
            raise FilesError("not a directory") from None
        if e.errno == errno.ELOOP:
            raise FilesError("refused: symlink", 403) from None
        raise FilesError("cannot open: " + _oserr(e)) from None


def _openat_file(fd, name, flags, mode=0o600):
    """Open one file component with O_NOFOLLOW; the kernel-atomic antidote
    to the classic verify-then-open symlink race (#91)."""
    try:
        return os.open(name, flags | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=fd)
    except FileExistsError:
        raise FilesError("already exists", 409) from None
    except FileNotFoundError:
        raise FilesError("not found", 404) from None
    except IsADirectoryError:
        raise FilesError("is a directory") from None
    except OSError as e:
        if e.errno == errno.ELOOP:  # final component is a symlink
            raise FilesError("refused: symlink", 403) from None
        raise FilesError("cannot open: " + _oserr(e)) from None


def _oserr(e):
    return e.strerror or str(e)


def split_parts(relpath):
    """Split a client-supplied relative path into validated components.

    Empty components, ".", "..", separators and control characters are all
    rejected (#86/#91); the result is safe to feed to the openat helpers."""
    if relpath in (None, "", "."):
        return []
    if "\x00" in relpath:
        raise FilesError("invalid path: null byte")
    parts = []
    for comp in str(relpath).split("/"):
        if comp in ("", "."):
            continue
        if comp == ".." or os.sep in comp or (os.altsep and os.altsep in comp):
            raise FilesError("invalid path component")
        valid_name(comp)
        parts.append(comp)
    return parts


def open_dir_fd(root_path, parts):
    """Open the directory root_path/parts; raises FilesError if any
    component is a symlink or missing. Caller owns the returned fd."""
    fd = open_root_fd(root_path)
    try:
        for p in parts:
            fd2 = _openat_dir(fd, p)
            os.close(fd)
            fd = fd2
    except Exception:
        os.close(fd)
        raise
    return fd


def open_file_fd(root_path, parts, flags, mode=0o600):
    """Open (or create) the file at root_path/parts with O_NOFOLLOW on the
    final component. Parent directories are walked with dirfds, so there is
    no window where a verified path is swapped before use (#91)."""
    if not parts:
        raise FilesError("invalid path")
    dirfd = open_dir_fd(root_path, parts[:-1])
    try:
        return _openat_file(dirfd, parts[-1], flags, mode)
    finally:
        os.close(dirfd)


def stat_contained(root_path, parts, follow=False):
    """fstat-style stat of a contained path. follow=False lstats the final
    component (O_NOFOLLOW-equivalent) via an open dirfd — no symlink is
    ever traversed (#91). Returns os.stat_result or raises FilesError."""
    if not parts:
        fd = open_root_fd(root_path)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)
    dirfd = open_dir_fd(root_path, parts[:-1])
    try:
        st = os.stat(parts[-1], dir_fd=dirfd, follow_symlinks=follow)
        return st
    except FileNotFoundError:
        raise FilesError("not found", 404) from None
    except OSError as e:
        raise FilesError("cannot stat: " + _oserr(e)) from None
    finally:
        os.close(dirfd)


def _delete_tree_fd(dirfd):
    """rm -rf on an already-open, already-verified directory fd.

    Works purely through dirfds (scandir + openat + unlinkat), so a symlink
    swapped in anywhere below cannot redirect the traversal (#91)."""
    for entry in os.scandir(dirfd):
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                os.remove(entry.name, dir_fd=dirfd)  # removes links, never their targets
            else:
                sub = os.open(entry.name, _O_DIR | _O_NOFOLLOW, dir_fd=dirfd)
                try:
                    _delete_tree_fd(sub)
                finally:
                    os.close(sub)
                os.rmdir(entry.name, dir_fd=dirfd)
        except FileNotFoundError:
            continue  # concurrent removal; nothing left to do
    return


def delete_contained(root_path, parts):
    """Delete a file, symlink or directory tree inside a root (#79/#91)."""
    if not parts:
        raise FilesError("cannot delete the root itself")
    dirfd = open_dir_fd(root_path, parts[:-1])
    try:
        name = parts[-1]
        try:
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            raise FilesError("not found", 404) from None
        if stat.S_ISDIR(st.st_mode):
            sub = _openat_dir(dirfd, name)
            try:
                _delete_tree_fd(sub)
            finally:
                os.close(sub)
            os.rmdir(name, dir_fd=dirfd)
        else:
            os.remove(name, dir_fd=dirfd)  # unlink: safe for symlinks too
    finally:
        os.close(dirfd)


# ---------------------------------------------------------------- names (#86, #91)


def valid_name(name, allow_hidden=False):
    """Validate one path component for creation (#86/#91).

    Rejects: empty/oversized names, separators, null/control characters,
    hidden dotfiles (unless allowed), and names that only differ from
    themselves after stripping. The name is returned unchanged (the stored
    form is preserved; NFC is used only for *comparison* — see name_key)."""
    if name is None:
        raise FilesError("a name is required")
    name = str(name)
    if not name or name in (".", ".."):
        raise FilesError("invalid name")
    if "\x00" in name:
        raise FilesError("invalid name: null byte")
    if "/" in name or "\\" in name or os.sep in name or (os.altsep and os.altsep in name):
        raise FilesError("invalid name: path separator")
    for ch in name:
        if unicodedata.category(ch) in ("Cc", "Cf") or ord(ch) == 127:
            raise FilesError("invalid name: control character")
    if len(name.encode("utf-8")) > NAME_MAX:
        raise FilesError("name too long")
    if not allow_hidden and name.startswith("."):
        raise FilesError("hidden names are not allowed")
    if name.strip() != name:
        raise FilesError("invalid name: leading/trailing space")
    return name


def name_key(name):
    """Comparison form (#86): NFC + casefold. Two names with the same key
    are visually identical, so only one may exist in a directory."""
    return unicodedata.normalize("NFC", str(name)).casefold()


def check_collision(dirfd, name):
    """Refuse writes that would collide with an existing entry whose NFC,
    casefolded name matches but whose stored name differs (#86) — a
    'café.txt' (NFD) cannot silently overwrite 'café.txt' (NFC) and a
    case-variant cannot shadow the original."""
    try:
        existing = os.listdir(dirfd)
    except OSError:
        return
    key = name_key(name)
    for other in existing:
        if other == name:
            continue
        if name_key(other) == key:
            raise FilesError(f"name collides with existing '{other}' (case/Unicode)", 409)


def safe_display_name(name, fallback="download"):
    """ASCII-sanitised filename for a Content-Disposition header (#86)."""
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFC", str(name or "")) if not unicodedata.category(ch) == "Cc"
    )
    cleaned = cleaned.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "").strip()
    ascii_part = cleaned.encode("ascii", "ignore").decode().strip() or fallback
    return ascii_part, cleaned


def content_disposition(name, inline=False):
    """RFC 6266/5987 header value safe for any UTF-8 filename (#86)."""
    ascii_part, full = safe_display_name(name)
    from urllib.parse import quote

    kind = "inline" if inline else "attachment"
    if full == ascii_part:
        return f'{kind}; filename="{ascii_part}"'
    return f"{kind}; filename=\"{ascii_part}\"; filename*=UTF-8''{quote(full)}"


# ---------------------------------------------------------------- legacy v2.0.1 surface


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
    """Legacy basename-only lookup inside FILES_DIR (kept for the shim's
    tests): no traversal, no hidden files, realpath contained, and the
    final component must not be a symlink (#91)."""
    name = os.path.basename(name or "")
    if not name or name.startswith(".") or "\x00" in name or len(name) > NAME_MAX:
        return None
    p = os.path.join(FILES_DIR, name)
    try:
        st = os.lstat(p)  # refuse symlinks outright (#91)
        if stat.S_ISLNK(st.st_mode):
            return None
    except FileNotFoundError:
        pass  # not there yet: uploads create it (containment checked below)
    except OSError:
        return None
    p = os.path.realpath(p)
    if os.path.dirname(p) != os.path.realpath(FILES_DIR):
        return None
    return p


# ---------------------------------------------------------------- shared roots (#84)


class Root:
    """One shared root: an absolute path, a display label and a read-only
    flag (#84). Optional per-root byte quota (#89)."""

    __slots__ = ("rid", "path", "label", "readonly", "quota")

    def __init__(self, rid, path, label, readonly=False, quota=0):
        self.rid, self.path, self.label = rid, path, label
        self.readonly, self.quota = readonly, quota

    def public(self):
        return {"id": self.rid, "path": self.path, "label": self.label, "readonly": self.readonly, "quota": self.quota}


ROOTS = []
ROOT_ERRORS = []


def parse_roots(spec, files_dir):
    """Parse an ``RD_FILES_ROOTS`` value into Root objects.

    Format: comma-separated ``PATH[:LABEL[:FLAGS]]``; FLAGS may be ``ro``
    and/or ``quota=BYTES``. Returns (roots, errors); entries with bad paths
    are skipped and reported so a bad root cannot take the server down."""
    roots, errors = [], []
    entries = [e.strip() for e in (spec or "").split(",") if e.strip()]
    if not entries:
        return [Root("r0", os.path.realpath(files_dir), "Downloads")], errors
    n = 0
    for raw in entries:
        bits = raw.split(":")
        path = os.path.realpath(os.path.expanduser(bits[0]))
        label = bits[1].strip() if len(bits) > 1 and bits[1].strip() else os.path.basename(path) or path
        readonly, quota = False, 0
        for flag in bits[2:]:
            flag = flag.strip().lower()
            if flag in ("ro", "readonly"):
                readonly = True
            elif flag.startswith("quota="):
                try:
                    quota = int(flag.split("=", 1)[1])
                    if quota <= 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"RD_FILES_ROOTS: bad quota for {label!r} (want quota=BYTES)")
                    quota = 0
            elif flag:
                errors.append(f"RD_FILES_ROOTS: unknown flag {flag!r} for {label!r}")
        if not os.path.isabs(bits[0].strip()) and bits[0].strip() != "~":
            errors.append(f"RD_FILES_ROOTS: {label!r} path must be absolute")
            continue
        roots.append(Root(f"r{n}", path, label[:60], readonly, quota))
        n += 1
    if not roots:
        errors.append("RD_FILES_ROOTS: no usable roots; falling back to RD_FILES_DIR")
        roots = [Root("r0", os.path.realpath(files_dir), "Downloads")]
    return roots, errors


def setup_roots():
    """Parse config, create missing root directories, report problems.
    Runs at startup (before the socket binds) and on demand in tests."""
    global ROOTS, ROOT_ERRORS
    ROOTS, ROOT_ERRORS = parse_roots(config.VALUES.get("files_roots", ""), config.FILES_DIR)
    for r in ROOTS:
        try:
            os.makedirs(r.path, exist_ok=True)
        except OSError as e:
            ROOT_ERRORS.append(f"RD_FILES_ROOTS: cannot create {r.path!r}: {e}")
    return ROOT_ERRORS


def resolve_root(ident):
    """Root by id ('r0') or label (case-insensitive); None when unknown."""
    if not ident:
        return ROOTS[0] if ROOTS else None
    for r in ROOTS:
        if r.rid == ident or r.label.lower() == str(ident).lower():
            return r
    return None


def require_writable(root):
    if root is None:
        raise FilesError("no such root", 404)
    if root.readonly:
        raise FilesError("root is read-only", 403)


def default_root():
    return ROOTS[0] if ROOTS else None


def roots_public():
    return [r.public() for r in ROOTS]


# ---------------------------------------------------------------- listing & operations (#79, #81)

SORT_KEYS = ("name", "size", "mtime", "type")


def _entry_json(dirfd, entry):
    """One listing row: name, type, size, times, permissions (#79/#90)."""
    st = entry.stat(follow_symlinks=False)
    is_link = entry.is_symlink()
    is_dir = entry.is_dir(follow_symlinks=False)
    return {
        "name": entry.name,
        "type": "dir" if is_dir else ("link" if is_link else ("file" if stat.S_ISREG(st.st_mode) else "other")),
        "size": 0 if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
        "mode": stat.filemode(st.st_mode),
        "perm": stat.S_IMODE(st.st_mode),
        "owner": _owner_name(st.st_uid),
        "group": _group_name(st.st_gid),
    }


_OWNER_CACHE = {}


def _owner_name(uid):
    """Best-effort user name; falls back to the numeric id (#90)."""
    if uid not in _OWNER_CACHE:
        try:
            import pwd

            _OWNER_CACHE[uid] = pwd.getpwuid(uid).pw_name
        except Exception:
            _OWNER_CACHE[uid] = str(uid)
    return _OWNER_CACHE[uid]


def _group_name(gid):
    """Best-effort group name; falls back to the numeric id (#90)."""
    if gid not in _OWNER_CACHE:
        try:
            import grp

            _GROUP = grp.getgrgid(gid).gr_name
        except Exception:
            _GROUP = str(gid)
        _OWNER_CACHE[gid] = _GROUP
    return _OWNER_CACHE[gid]


def list_dir(root, relpath, sort="name", desc=False, dirs_first=True):
    """Directory listing (#79). Sort keys: name/size/mtime/type (stable;
    directories group first unless dirs_first=False). Only the entries the
    caller may act on are listed — dotfiles and symlinks-in-name-only are
    shown as their own type so they can be identified, never followed."""
    parts = split_parts(relpath)
    dirfd = open_dir_fd(root.path, parts)
    try:
        entries = []
        for entry in os.scandir(dirfd):
            if entry.name.startswith("."):  # partial uploads / dotfiles stay invisible
                continue
            try:
                entries.append(_entry_json(dirfd, entry))
            except OSError:
                continue  # raced away mid-listing
    finally:
        os.close(dirfd)
    key = sort if sort in SORT_KEYS else "name"
    if key == "type":  # group by kind: dirs, links, files, other
        entries.sort(key=lambda e: e["type"])
    elif key == "name":
        entries.sort(key=lambda e: name_key(e["name"]))
    else:
        entries.sort(key=lambda e: e[key])
    if desc:
        entries.reverse()
    if dirs_first:
        entries.sort(key=lambda e: e["type"] != "dir")  # stable: keeps sort order within groups
    return {
        "path": "/".join(parts),
        "entries": entries,
        "dirs": [e["name"] for e in entries if e["type"] == "dir"],
        "files": [e for e in entries if e["type"] != "dir"],
    }


def search(root, relpath, q, limit=SEARCH_RESULTS_MAX):
    """Bounded recursive filename search (#81). Stops at the scan cap, the
    result cap or the time cap — whichever hits first. Cancellation is the
    client aborting the request; the walk itself is always bounded."""
    parts = split_parts(relpath)
    qn = name_key(q or "")
    if not qn:
        return {"matches": [], "scanned": 0, "truncated": False}
    start = time.time()
    matches, scanned = [], 0
    truncated = False
    stack = [parts]
    base = open_dir_fd(root.path, parts)
    os.close(base)  # existence + containment check only; walk re-opens
    while stack:
        if len(matches) >= limit or scanned >= SEARCH_SCAN_MAX or time.time() - start > SEARCH_TIME_MAX:
            truncated = bool(stack) or len(matches) >= limit or scanned >= SEARCH_SCAN_MAX
            break
        cur = stack.pop()
        try:
            dirfd = open_dir_fd(root.path, cur)
        except FilesError:
            continue  # disappeared or refused (symlink) mid-walk
        try:
            for entry in os.scandir(dirfd):
                scanned += 1
                if scanned > SEARCH_SCAN_MAX or time.time() - start > SEARCH_TIME_MAX:
                    truncated = True
                    break
                if entry.name.startswith("."):
                    continue
                if qn in name_key(entry.name):
                    try:
                        matches.append({**_entry_json(dirfd, entry), "dir": "/".join(cur)})
                    except OSError:
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                if entry.is_dir(follow_symlinks=False):
                    stack.append(cur + [entry.name])
        except OSError:
            continue
        finally:
            os.close(dirfd)
    return {"matches": matches[:limit], "scanned": scanned, "truncated": truncated}


def mkdir(root, relpath):
    """Create a directory (and parents) inside a root (#79)."""
    require_writable(root)
    parts = split_parts(relpath)
    if not parts:
        raise FilesError("a directory name is required")
    # walk/create level by level through verified dirfds
    fd = open_root_fd(root.path)
    try:
        for comp in parts:
            try:
                nxt = os.mkdir(comp, 0o755, dir_fd=fd)
            except FileExistsError:
                # must be an existing real directory, not a file/symlink
                st = os.lstat(comp, dir_fd=fd)
                if not stat.S_ISDIR(st.st_mode):
                    raise FilesError(f"'{comp}' exists and is not a directory") from None
                nxt = None
            except FileNotFoundError:
                raise FilesError("parent not found") from None
            except OSError as e:
                if e.errno == errno.EEXIST:
                    raise FilesError(f"'{comp}' exists and is not a directory") from None
                raise FilesError("cannot create: " + _oserr(e)) from None
            if nxt is None:
                nxt = _openat_dir(fd, comp)
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)
    return {"path": "/".join(parts)}


def rename(root, relpath, new_name):
    """Rename one entry within its directory (#79). Collision-checked (#86)."""
    require_writable(root)
    parts = split_parts(relpath)
    if len(parts) < 1:
        raise FilesError("nothing to rename")
    valid_name(new_name)
    dirfd = open_dir_fd(root.path, parts[:-1])
    try:
        check_collision(dirfd, new_name)
        try:
            os.rename(parts[-1], new_name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except FileNotFoundError:
            raise FilesError("not found", 404) from None
        except OSError as e:
            raise FilesError("cannot rename: " + _oserr(e)) from None
    finally:
        os.close(dirfd)
    return {"path": "/".join(parts[:-1] + [new_name])}


def _same_root(root_a, root_b):
    return root_a is not None and root_b is not None and os.path.realpath(root_a.path) == os.path.realpath(root_b.path)


def _dest_name(dest_dirfd, src_name, overwrite):
    """Destination name for move/copy: the same name when free, the same
    name to replace when overwriting, else 'name (2).ext' (#80/#86)."""
    try:
        os.lstat(src_name, dir_fd=dest_dirfd)
    except FileNotFoundError:
        return src_name, False
    except OSError as e:
        raise FilesError("destination unreadable: " + _oserr(e)) from None
    if overwrite:
        return src_name, True
    # auto-rename; dotfiles keep their leading dot
    stem, dot, ext = src_name.rpartition(".")
    base, ext = (src_name, "") if (not dot or src_name.startswith(".")) else (stem, "." + ext)
    for i in range(2, 1000):
        cand = f"{base} ({i}){ext}"
        try:
            os.lstat(cand, dir_fd=dest_dirfd)
        except FileNotFoundError:
            return cand, False
        except OSError as e:
            raise FilesError("destination unreadable: " + _oserr(e)) from None
    raise FilesError("destination exists", 409)


def _copy_file(src_fd, dst_fd):
    shutil.copyfileobj(open(src_fd, "rb", closefd=False), open(dst_fd, "wb", closefd=False), length=1 << 20)


def _copy_tree(src_dirfd, dst_dirfd, budget):
    """Recursive copy between verified dirfds; budget tracks remaining
    bytes so a huge tree cannot run away unbounded."""
    for entry in os.scandir(src_dirfd):
        if budget[0] <= 0:
            raise FilesError("copy too large (quota/cap exceeded)")
        if entry.is_dir(follow_symlinks=False):
            os.mkdir(entry.name, 0o755, dir_fd=dst_dirfd)
            sub_src = _openat_dir(src_dirfd, entry.name)
            try:
                sub_dst = _openat_dir(dst_dirfd, entry.name)
            except FilesError:
                os.close(sub_src)
                raise
            try:
                _copy_tree(sub_src, sub_dst, budget)
            finally:
                os.close(sub_src)
                os.close(sub_dst)
        else:
            if entry.is_symlink():
                continue  # never replicate links (containment model, #91)
            st = entry.stat(follow_symlinks=False)
            budget[0] -= st.st_size
            src_fd = _openat_file(src_dirfd, entry.name, os.O_RDONLY)
            try:
                dst_fd = _openat_file(dst_dirfd, entry.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    _copy_file(src_fd, dst_fd)
                finally:
                    os.close(dst_fd)
            finally:
                os.close(src_fd)


def transfer(root_src, src, root_dst, dest_dir, overwrite=False, copy=False):
    """Move (rename or copy+delete) entries between directories/roots (#79).

    Move within the same root is a rename (fast, atomic); everything else
    copies bytes through verified fds and then unlinks the source for
    moves. Returns a list of per-item results (bulk, #79)."""
    require_writable(root_src)
    if not copy:
        require_writable(root_dst)
    src_parts = split_parts(src)
    dest_parts = split_parts(dest_dir)
    if not src_parts:
        raise FilesError("nothing selected")
    same = _same_root(root_src, root_dst)
    if same and not copy:
        srcfd = open_dir_fd(root_src.path, src_parts[:-1])
        dstfd = open_dir_fd(root_dst.path, dest_parts)
        try:
            name, do_replace = _dest_name(dstfd, src_parts[-1], overwrite)
            if not do_replace:
                check_collision(dstfd, name)
            try:
                os.rename(src_parts[-1], name, src_dir_fd=srcfd, dst_dir_fd=dstfd)
                return {"name": src_parts[-1], "dest": "/".join(dest_parts + [name])}
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise FilesError("cannot move: " + _oserr(e)) from None
                # different filesystem inside one root: fall through to copy
        finally:
            os.close(srcfd)
            os.close(dstfd)
    # copy (or cross-root move): stream through fds
    results = []
    budget = [config.VALUES.get("archive_cap", 2 * 1024**3)]
    src_dirfd = open_dir_fd(root_src.path, src_parts[:-1]) if len(src_parts) > 1 else open_root_fd(root_src.path)
    try:
        src_name = src_parts[-1]
        st = os.lstat(src_name, dir_fd=src_dirfd)
        try:
            dst_dirfd = open_dir_fd(root_dst.path, dest_parts)
        except FilesError as e:
            # copying a directory into a not-yet-existing folder names the
            # copy: transfer(sub -> "subcopy") creates subcopy as the tree
            rename_style = stat.S_ISDIR(st.st_mode) and e.status == 404 and bool(dest_parts)
            if rename_style:
                parent = open_dir_fd(root_dst.path, dest_parts[:-1])
                try:
                    try:
                        os.mkdir(dest_parts[-1], 0o755, dir_fd=parent)
                    except FileExistsError:
                        pass
                    dst_dirfd = _openat_dir(parent, dest_parts[-1])
                finally:
                    os.close(parent)
            else:
                raise
        else:
            rename_style = False
        try:
            name, do_replace = _dest_name(dst_dirfd, src_name, overwrite)
            if not do_replace:
                check_collision(dst_dirfd, name)
            if stat.S_ISDIR(st.st_mode):
                sub_src = _openat_dir(src_dirfd, src_name)
                try:
                    if rename_style or do_replace:
                        # dest dir was just created (rename-style) or is
                        # being replaced: entries stream directly into it
                        sub_dst = dst_dirfd
                    else:
                        try:
                            os.mkdir(name, 0o755, dir_fd=dst_dirfd)
                        except FileExistsError:
                            raise FilesError("already exists", 409) from None
                        sub_dst = _openat_dir(dst_dirfd, name)
                    try:
                        _copy_tree(sub_src, sub_dst, budget)
                    finally:
                        if sub_dst is not dst_dirfd:
                            os.close(sub_dst)
                finally:
                    os.close(sub_src)
            else:
                src_fd = _openat_file(src_dirfd, src_name, os.O_RDONLY)
                try:
                    if do_replace:
                        # open existing destination with O_NOFOLLOW and truncate
                        dst_fd = _openat_file(dst_dirfd, name, os.O_WRONLY | os.O_TRUNC)
                    else:
                        dst_fd = _openat_file(dst_dirfd, name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        _copy_file(src_fd, dst_fd)
                    finally:
                        os.close(dst_fd)
                finally:
                    os.close(src_fd)
        finally:
            os.close(dst_dirfd)
        results.append(
            {
                "name": src_name,
                "dest": "/".join(dest_parts if rename_style else dest_parts + [name]),
            }
        )
    finally:
        os.close(src_dirfd)
    if not copy:
        delete_contained(root_src.path, src_parts)
    return results[0] if len(results) == 1 else results


def delete_many(root, paths):
    """Bulk delete (#79): every path independently; partial failures are
    reported per item, never abort the batch."""
    if isinstance(paths, str):  # legacy single-name body
        paths = [paths]
    results = []
    for p in paths:
        try:
            parts = split_parts(p)
            delete_contained(root.path, parts)
            results.append({"path": p, "ok": True})
        except FilesError as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    failed = [r for r in results if not r["ok"]]
    return {"deleted": [r["path"] for r in results if r["ok"]], "failed": failed, "ok": not failed}


# ---------------------------------------------------------------- HTTP download ranges (#83)


def parse_range(hdr, size):
    """Parse a single HTTP Range header (#83).

    Returns (start, end) inclusive, None when absent (serve the whole
    file), or "invalid" when malformed, multi-range or unsatisfiable —
    the caller answers 416 with Content-Range: bytes */size."""
    if not hdr:
        return None
    if not hdr.startswith("bytes="):
        return "invalid"
    spec = hdr[6:].strip()
    if "," in spec:  # multi-range: refused (documented), not partially served
        return "invalid"
    m = re.match(r"^(\d*)-(\d*)$", spec)
    if not m or (not m.group(1) and not m.group(2)):
        return "invalid"
    if m.group(1) == "":  # suffix form: last N bytes
        n = int(m.group(2))
        if n <= 0 or size == 0:
            return "invalid"
        return (max(0, size - n), size - 1)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if end >= size:  # over-long end is tolerated and clamped (RFC 7233)
        end = size - 1
    if start > end or start >= size:
        return "invalid"
    return (start, end)


# ---------------------------------------------------------------- PWA share target (#87)


class _BoundedReader:
    """Content-Length-bounded view of the request stream: read() must never
    block past the body on a keep-alive connection (#87)."""

    def __init__(self, f, n):
        self.f, self.left = f, n

    def read(self, k=-1):
        if self.left <= 0:
            return b""
        k = 1 << 16 if k is None or k < 0 else min(k, 1 << 16)
        b = self.f.read(min(k, self.left))
        self.left -= len(b)
        return b

    def readline(self, lim=1 << 16):
        if self.left <= 0:
            return b""
        b = self.f.readline(min(lim, self.left))
        self.left -= len(b)
        return b


def share_target_save(root, rfile, content_length, boundary):
    """Stream multipart/form-data file parts into a root (#87).

    Bounded per part (UPLOAD_LIMIT) and in total (content_length); parts
    land through the same contained temp+rename path as uploads, so the
    OS share sheet can never write outside the roots."""
    require_writable(root)
    quota_check(root, content_length or 0)
    boundary = boundary.encode() if isinstance(boundary, str) else boundary
    marker = b"\r\n--" + boundary
    rfile = _BoundedReader(rfile, content_length or 0)
    line = rfile.readline(1 << 16)
    if not line.startswith(b"--" + boundary):
        raise FilesError("malformed multipart body")
    saved = []
    destfd = open_root_fd(root.path)
    try:
        part_no = 0
        while True:
            heads = b""
            while True:  # part headers, blank line ends them
                line = rfile.readline(1 << 16)
                if line in (b"\r\n", b"\n", b""):
                    break
                heads += line
                if len(heads) > 16384:
                    raise FilesError("malformed multipart headers")
            if not heads:
                break  # EOF before the final boundary: keep what we have
            part_no += 1
            m = re.search(rb'filename="([^"]*)"', heads)
            fname = m.group(1).decode("utf-8", "replace") if m else None
            written, final = _consume_share_part(rfile, marker, destfd, part_no)
            if fname is not None:
                name = valid_name(fname)
                check_collision(destfd, name)
                os.rename(f".share-{os.getpid()}-{part_no}.part", name, src_dir_fd=destfd, dst_dir_fd=destfd)
                saved.append({"name": name, "size": written})
            else:  # a non-file field: drop its temp body
                try:
                    os.remove(f".share-{os.getpid()}-{part_no}.part", dir_fd=destfd)
                except OSError:
                    pass
            if final:
                break
        return saved
    finally:
        os.close(destfd)


def _consume_share_part(rfile, marker, destfd, part_no):
    """Stream one part body into a temp file inside the root until the
    next boundary. Returns (bytes_written, is_final_boundary)."""
    tmpname = f".share-{os.getpid()}-{part_no}.part"
    fd = _openat_file(destfd, tmpname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    written = 0
    overlap = b""
    try:
        while True:
            chunk = rfile.read(1 << 16)
            if not chunk:
                raise FilesError("malformed multipart body (truncated)")
            buf = overlap + chunk
            idx = buf.find(marker)
            if idx < 0:
                keep = len(marker) - 1
                emit, overlap = (buf[:-keep], buf[-keep:]) if len(buf) > keep else (b"", buf)
                written += len(emit)
                if written > UPLOAD_LIMIT:
                    raise FilesError("share part too large (2GB limit)", 413)
                if emit:
                    os.write(fd, emit)
                continue
            emit = buf[:idx]
            written += len(emit)
            if written > UPLOAD_LIMIT:
                raise FilesError("share part too large (2GB limit)", 413)
            if emit:
                os.write(fd, emit)
            os.fsync(fd)
            end = buf[idx + len(marker) :]
            return written, end.startswith(b"--")
    except Exception:
        try:
            os.remove(tmpname, dir_fd=destfd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


# ---------------------------------------------------------------- free space & quotas (#89)


def disk_info(root):
    """Free/total bytes for the filesystem holding the root, plus quota
    usage when the root has one (#89). Values are advisory (cached walk)."""
    du = shutil.disk_usage(root.path)
    out = {
        "free": du.free,
        "total": du.total,
        "used": du.used,
        "reserve": config.VALUES.get("files_reserve", 0),
        "quota": root.quota,
        "warn_pct": 90,  # UI warns at 90% usage (#89)
    }
    if root.quota:
        used = _root_used(root)
        out["quota_used"] = used
        out["quota_free"] = max(0, root.quota - used)
    return out


_disk_cache = {}


def _tree_bytes(dirfd, budget):
    """Sum of file sizes under a verified dirfd; bounded walk."""
    total = 0
    for entry in os.scandir(dirfd):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                sub = os.open(entry.name, _O_DIR | _O_NOFOLLOW, dir_fd=dirfd)
                try:
                    total += _tree_bytes(sub, budget)
                finally:
                    os.close(sub)
            else:
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _root_used(root, max_age=60):
    """Best-effort used-bytes under a root, cached briefly (#89)."""
    now = time.time()
    c = _disk_cache.get(root.rid)
    if c and now - c[0] < max_age:
        return c[1]
    try:
        fd = open_root_fd(root.path)
        try:
            used = _tree_bytes(fd, [100_000])
        finally:
            os.close(fd)
    except (OSError, FilesError):
        used = 0
    _disk_cache[root.rid] = (now, used)
    return used


def quota_check(root, incoming):
    """Refuse writes that would cross the free-space reserve or the root's
    quota, BEFORE any bytes are written (#89). Explains the limit."""
    if root is None:
        raise FilesError("no such root", 404)
    incoming = incoming or 0
    reserve = config.VALUES.get("files_reserve", 0)
    du = shutil.disk_usage(root.path)
    if reserve and du.free - incoming < reserve:
        raise FilesError(
            f"refused: this upload would leave less than the configured free-space reserve "
            f"({reserve // (1 << 20)} MiB); only {du.free // (1 << 20)} MiB free on {root.label}",
            507,
        )
    if root.quota:
        used = _root_used(root)
        if used + incoming > root.quota:
            raise FilesError(
                f"refused: per-root quota of {root.quota // (1 << 20)} MiB on {root.label} would be exceeded "
                f"({used // (1 << 20)} MiB already used)",
                507,
            )


# ---------------------------------------------------------------- permissions (#90)

_CHMOD_FORBIDDEN = stat.S_ISUID | stat.S_ISGID  # never from the web UI (#90)


def chmod(root, relpath, mode):
    """Change permission bits on one contained entry (#90).

    Refuses setuid/setgid (dangerous modes are blocked and explained);
    sticky bits and owner/group changes are not offered. The change is
    applied to an already-open O_NOFOLLOW fd, so a symlink swap cannot
    redirect it."""
    require_writable(root)
    if mode & _CHMOD_FORBIDDEN:
        raise FilesError("refused: setuid/setgid bits are not allowed from the web UI", 403)
    mode &= 0o777
    parts = split_parts(relpath)
    if not parts:
        raise FilesError("nothing selected")
    # O_NONBLOCK so a FIFO cannot hang the open; fchmod works on either kind
    fd = open_file_fd(root.path, parts, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):  # cannot happen with O_NOFOLLOW; belt and braces
            raise FilesError("refused: symlink", 403)
        os.fchmod(fd, mode)
        return {"path": "/".join(parts), "mode": oct(mode), "was_dir": stat.S_ISDIR(st.st_mode)}
    finally:
        os.close(fd)


# ---------------------------------------------------------------- audit log (#92)

AUDIT_FILE = None  # test override; default: STATE_BASE/files-audit.jsonl


def _audit_path():
    return AUDIT_FILE or os.path.join(_state_base(), "files-audit.jsonl")


def _audit_clean(value, cap=400):
    s = str(value)[:cap]
    return "".join(ch if not unicodedata.category(ch) == "Cc" else "?" for ch in s)


def audit(action, handler=None, root=None, path=None, dest=None, size=None, detail=None, result="ok"):
    """Record ONE audit record per mutating file operation (#92).

    Never contains file contents — only paths, sizes, identity and result.
    Rotates at AUDIT_MAX keeping AUDIT_KEEP generations; failures to write
    never break the operation that is being audited."""
    from . import auth

    try:
        rec = {
            "ts": int(time.time()),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "action": _audit_clean(action, 40),
            "user": _audit_clean(auth.AUTH_USER, 80),
            "ip": _audit_clean(auth._client_ip(handler) if handler else "-", 64),
            "rid": _audit_clean(getattr(handler, "rid", "-") if handler else "-", 16),
            "root": _audit_clean(root.label, 60) if root else None,
            "path": _audit_clean(path) if path else None,
            "dest": _audit_clean(dest) if dest else None,
            "size": int(size) if size is not None else None,
            "result": _audit_clean(result, 20),
        }
        if detail:
            rec["detail"] = {
                _audit_clean(str(k), 40): _audit_clean(v, 200)
                for k, v in list(detail.items())[:8]
                if isinstance(v, (str, int, bool))
            }
        _audit_append(rec)
    except Exception:
        pass


def _audit_append(rec):
    path = _audit_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size + 512 > AUDIT_MAX:  # rotate: .N-1 -> .N ... live -> .1
            for i in range(AUDIT_KEEP, 0, -1):
                src = path if i == 1 else f"{path}.{i - 1}"
                dst = f"{path}.{i}"
                if os.path.exists(src):
                    if i == AUDIT_KEEP:
                        os.remove(src)
                    else:
                        os.replace(src, dst)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def audit_list(limit=200):
    """Newest-last audit records, bounded read (#92)."""
    try:
        with open(_audit_path(), "rb") as f:
            data = f.read(2 * AUDIT_MAX)
        lines = data.decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- chunked uploads (#78) + queue (#88)

STATE_BASE = None  # test override; default: the configured state dir


def _state_base():
    return STATE_BASE or config.VALUES.get("state_dir", config.STATE_DIR)


def sessions_dir():
    return os.path.join(_state_base(), "files-sessions")


def _session_path(sid):
    if not sid or not all(c in "0123456789abcdef" for c in sid) or len(sid) > 32:
        raise FilesError("bad session id")
    return os.path.join(sessions_dir(), sid)


def _session_write_meta(sdir, meta):
    tmp = os.path.join(sdir, "meta.json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(sdir, "meta.json"))


def _session_load(sid):
    sdir = _session_path(sid)
    try:
        with open(os.path.join(sdir, "meta.json")) as f:
            meta = json.load(f)
    except FileNotFoundError:
        raise FilesError("no such upload session", 404) from None
    except (OSError, ValueError):
        raise FilesError("upload session unreadable") from None
    return sdir, meta


def upload_init(root, dirpath, name, size=0, sha256="", chunk_size=0, resume=""):
    """Start (or resume) a chunked upload session (#78).

    Resume returns the last ACKNOWLEDGED offset — everything past it was
    durably fsynced, so a reconnect continues near where it stopped."""
    if resume:
        sdir, meta = _session_load(resume)
        if meta.get("state") == "done":
            return {"session": resume, "done": True, "offset": meta["offset"], "size": meta["size"]}
        return {"session": resume, "offset": meta["offset"], "size": meta["size"], "chunk_size": meta["chunk"]}
    require_writable(root)
    name = valid_name(name)
    parts = split_parts(dirpath)
    size = int(size or 0)
    if size < 0 or size > UPLOAD_LIMIT:
        raise FilesError(f"size out of range (limit {UPLOAD_LIMIT} bytes)", 413)
    quota_check(root, size)
    chunk_size = int(chunk_size or (4 * 1024**2))
    chunk_size = max(64 * 1024, min(chunk_size, CHUNK_MAX))
    if len(active_sessions()) >= SESSIONS_MAX:
        raise FilesError("too many active upload sessions; retry shortly", 503)
    sid = secrets.token_hex(12)
    sdir = os.path.join(sessions_dir(), sid)
    os.makedirs(sdir, exist_ok=True)
    open(os.path.join(sdir, "part.bin"), "wb").close()
    meta = {
        "id": sid,
        "root": root.rid,
        "dir": "/".join(parts),
        "name": name,
        "size": size,
        "chunk": chunk_size,
        "offset": 0,
        "sha256": (sha256 or "").strip().lower()[:64],
        "state": "running",
        "retries": 0,
        "created": int(time.time()),
        "updated": int(time.time()),
    }
    _session_write_meta(sdir, meta)
    exists = False
    dfd = open_dir_fd(root.path, parts)
    try:
        os.lstat(name, dir_fd=dfd)
        exists = True
    except OSError:
        pass
    finally:
        os.close(dfd)
    return {"session": sid, "offset": 0, "size": size, "chunk_size": chunk_size, "name": name, "path": meta["dir"], "exists": exists}


def upload_chunk(sid, offset, body, chunk_sha=""):
    """Append one chunk at the acknowledged offset (#78). Checksum
    mismatches raise 400 with the offset UNCHANGED so the client retries
    the same chunk; paused sessions refuse with 409 (#88)."""
    sdir, meta = _session_load(sid)
    if meta["state"] == "paused":
        raise FilesError("transfer is paused", 409)
    if meta["state"] in ("done", "cancelled"):
        raise FilesError(f"transfer already {meta['state']}", 409)
    offset = int(offset)
    if offset != meta["offset"]:
        raise FilesError(f"offset mismatch: acknowledged offset is {meta['offset']}", 409)
    if not body:
        raise FilesError("empty chunk")
    if len(body) > meta["chunk"]:
        raise FilesError(f"chunk too large (limit {meta['chunk']} bytes)", 413)
    if meta["offset"] + len(body) > UPLOAD_LIMIT:
        raise FilesError("upload exceeds the 2 GB limit", 413)
    if meta["size"] and meta["offset"] + len(body) > meta["size"]:
        raise FilesError("chunk would exceed the declared size", 400)
    if chunk_sha:
        got = hashlib.sha256(body).hexdigest()
        if got != chunk_sha.strip().lower()[:64]:
            meta["retries"] = meta.get("retries", 0) + 1
            _session_write_meta(sdir, meta)
            raise FilesError("chunk checksum mismatch; resend this chunk", 400)
    part = os.path.join(sdir, "part.bin")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.lseek(fd, offset, 0)
        os.write(fd, body)
        os.fsync(fd)  # acknowledged bytes survive a crash (#78)
    finally:
        os.close(fd)
    meta["offset"] += len(body)
    meta["updated"] = int(time.time())
    _session_write_meta(sdir, meta)
    return {"offset": meta["offset"], "acked": len(body), "state": meta["state"]}


def upload_complete(sid):
    """Verify and move a finished session into the destination (#78)."""
    sdir, meta = _session_load(sid)
    if meta["state"] == "done":
        return {
            "name": meta["name"],
            "path": meta["dir"] + "/" + meta["name"],
            "size": meta["offset"],
            "root": meta["root"],
        }
    if meta["size"] and meta["offset"] != meta["size"]:
        raise FilesError(f"incomplete: {meta['offset']}/{meta['size']} bytes acknowledged", 409)
    if meta["sha256"]:
        h = hashlib.sha256()
        with open(os.path.join(sdir, "part.bin"), "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
        if h.hexdigest() != meta["sha256"]:
            meta["state"] = "failed"
            _session_write_meta(sdir, meta)
            raise FilesError("whole-file checksum mismatch", 400)
    root = resolve_root(meta["root"])
    if root is None:
        raise FilesError("upload root no longer exists")
    require_writable(root)
    dest_parts = split_parts(meta["dir"])
    name = meta["name"]
    destfd = open_dir_fd(root.path, dest_parts)
    tmpname = f".upload-{os.getpid()}-{sid[:8]}.part"
    src = open(os.path.join(sdir, "part.bin"), "rb")
    try:
        # stream the part file to a temp file INSIDE the destination
        # directory (same fs), fsync, then rename — atomic like #15
        dstfd = _openat_file(destfd, tmpname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(os.dup(dstfd), "wb", closefd=True) as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            os.fsync(dstfd)
        finally:
            os.close(dstfd)
        if meta.get("overwrite"):
            try:
                os.remove(name, dir_fd=destfd)  # the client chose replace
            except FileNotFoundError:
                pass
        else:
            check_collision(destfd, name)
        os.rename(tmpname, name, src_dir_fd=destfd, dst_dir_fd=destfd)
    except Exception:
        try:
            os.remove(tmpname, dir_fd=destfd)
        except OSError:
            pass
        raise
    finally:
        src.close()
        os.close(destfd)
    meta["state"] = "done"
    meta["updated"] = int(time.time())
    _session_write_meta(sdir, meta)
    return {
        "name": name,
        "path": (meta["dir"] + "/" if meta["dir"] else "") + name,
        "size": meta["offset"],
        "root": root.rid,
    }


def upload_cancel(sid):
    sdir, meta = _session_load(sid)
    shutil.rmtree(sdir, ignore_errors=True)
    return {"cancelled": sid}


def upload_status(sid):
    sdir, meta = _session_load(sid)
    return session_public(meta)


def session_public(meta):
    return {
        "id": meta["id"],
        "kind": "upload",
        "name": meta["name"],
        "dir": meta["dir"],
        "path": (meta["dir"] + "/" if meta["dir"] else "") + meta["name"],
        "root": meta["root"],
        "size": meta["size"],
        "offset": meta["offset"],
        "state": meta["state"],
        "retries": meta.get("retries", 0),
        "created": meta["created"],
        "updated": meta["updated"],
        "pct": (100 * meta["offset"] // meta["size"]) if meta["size"] else 0,
    }


def active_sessions():
    """Session ids currently on disk (bounded read)."""
    try:
        return os.listdir(sessions_dir())
    except OSError:
        return []


def transfers_snapshot():
    """The persistent transfer queue (#88): every session with state and
    aggregate progress; completed jobs older than the retention window are
    dropped on sight."""
    retention = config.VALUES.get("transfer_retention", TRANSFER_RETENTION)
    gc_transfers(retention)
    jobs = []
    try:
        ids = os.listdir(sessions_dir())
    except OSError:
        ids = []
    for sid in sorted(ids)[:200]:
        try:
            _, meta = _session_load(sid)
            jobs.append(session_public(meta))
        except (FilesError, OSError, KeyError):
            continue
    jobs.sort(key=lambda j: j["updated"], reverse=True)
    running = [j for j in jobs if j["state"] == "running"]
    total_size = sum(j["size"] for j in running)
    total_offset = sum(j["offset"] for j in running)
    return {
        "ok": True,
        "transfers": jobs,
        "aggregate": {
            "running": len(running),
            "offset": total_offset,
            "size": total_size,
            "pct": (100 * total_offset // total_size) if total_size else 0,
        },
    }


def _transfer_set_state(sid, state):
    sdir, meta = _session_load(sid)
    if meta["state"] in ("done", "cancelled") and state != "running":
        raise FilesError(f"transfer already {meta['state']}", 409)
    meta["state"] = state
    meta["updated"] = int(time.time())
    _session_write_meta(sdir, meta)
    return session_public(meta)


def transfer_pause(sid):
    """Pause: acknowledged bytes are kept; chunks to a paused session get 409 (#88)."""
    return _transfer_set_state(sid, "paused")


def transfer_resume(sid):
    return _transfer_set_state(sid, "running")


def transfer_cancel(sid):
    sdir, meta = _session_load(sid)
    shutil.rmtree(sdir, ignore_errors=True)
    return {"id": sid, "state": "cancelled"}


def transfer_retry(sid):
    """Retry a failed/paused transfer from the acknowledged offset (#88)."""
    sdir, meta = _session_load(sid)
    if meta["state"] == "done":
        raise FilesError("transfer already done", 409)
    meta["retries"] = meta.get("retries", 0) + 1
    meta["state"] = "running"
    meta["updated"] = int(time.time())
    _session_write_meta(sdir, meta)
    return session_public(meta)


def gc_transfers(retention=None):
    """Remove expired sessions (abandoned TTL, finished retention) (#78/#88)."""
    retention = config.VALUES.get("transfer_retention", TRANSFER_RETENTION) if retention is None else retention
    now = time.time()
    removed = 0
    try:
        ids = os.listdir(sessions_dir())
    except OSError:
        return 0
    for sid in ids:
        sdir = os.path.join(sessions_dir(), sid)
        try:
            with open(os.path.join(sdir, "meta.json")) as f:
                meta = json.load(f)
            age = now - meta.get("updated", 0)
            state = meta.get("state")
            if age > SESSION_TTL or (state in ("done", "cancelled") and age > retention):
                shutil.rmtree(sdir, ignore_errors=True)
                removed += 1
        except (OSError, ValueError):
            continue
    return removed


def gc_loop():
    """Background GC: abandoned sessions, retention, partial uploads (#78/#88)."""
    while True:
        time.sleep(TRANSFER_GC_PERIOD)
        cleanup_partial_uploads()
        gc_transfers()


# ---------------------------------------------------------------- archives (#85)

_ARCHIVE_FORMATS = {
    "zip": (".zip", "zip"),
    "tar.gz": (".tar.gz", "w:gz"),
    "tar.xz": (".tar.xz", "w:xz"),
}
_MEMBER_MAX = 10_000  # entries per archive


def _member_parts(name, allow_dot=True):
    """Validate one archive member path; returns components. Refuses
    absolute paths, '..', separators smuggled as backslash, control
    characters — the zip-slip defence (#85/#91)."""
    if not name or "\\" in name or "\x00" in name:
        raise FilesError("refused: unsafe member name in archive")
    parts = []
    for comp in name.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise FilesError("refused: traversal member in archive")
        valid_name(comp, allow_hidden=allow_dot)
        parts.append(comp)
    if not parts:
        raise FilesError("refused: empty member name")
    return parts


def _ensure_dirs(fd, parts):
    """Create the directory chain under an open dirfd (contained). Returns
    an fd for the final directory — the caller's `fd` when parts is empty,
    otherwise a NEW fd the caller must close."""
    cur = fd
    for comp in parts:
        try:
            os.mkdir(comp, 0o755, dir_fd=cur)
        except FileExistsError:
            pass
        nxt = _openat_dir(cur, comp)
        if cur is not fd:
            os.close(cur)
        cur = nxt
    return cur


def archive_create(root, paths, fmt="zip", name=""):
    """Create zip/tar.gz/tar.xz from selected entries inside a root (#85)."""
    require_writable(root)
    if fmt not in _ARCHIVE_FORMATS:
        raise FilesError("unsupported format; available: zip, tar.gz, tar.xz", 400)
    ext, mode = _ARCHIVE_FORMATS[fmt]
    name = valid_name(name or "archive")
    if not name.endswith(ext):
        name += ext
    cap = config.VALUES.get("archive_cap", 2 * 1024**3)
    # plan: resolve every selection to component lists and a total size,
    # bounded — an archive is refused before a single byte is written
    plan = []
    total = 0
    for p in paths[:100]:
        parts = split_parts(p)
        if not parts:
            continue
        st = stat_contained(root.path, parts)
        if stat.S_ISDIR(st.st_mode):
            dirfd = open_dir_fd(root.path, parts)
            try:
                for rel, size in _walk_rel(dirfd, parts):
                    plan.append(rel)
                    total += size
            finally:
                os.close(dirfd)
        else:
            plan.append(parts)
            total += st.st_size
        if total > cap:
            raise FilesError(f"selection too large for one archive (cap {cap} bytes)", 413)
    if not plan:
        raise FilesError("nothing to archive")
    dest_dir = split_parts(paths[0])[:-1] if paths else []
    destfd = open_dir_fd(root.path, dest_dir)
    tmpname = f".archive-{os.getpid()}-{secrets.token_hex(4)}{ext}"
    try:
        out_fd = _openat_file(destfd, tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(out_fd, "wb", closefd=False) as zf:
                if fmt == "zip":
                    with zipfile.ZipFile(zf, "w", zipfile.ZIP_DEFLATED) as z:
                        for rel in plan:
                            z.write(_contained_tmp_path(root, rel), arcname="/".join(rel))
                else:
                    with tarfile.open(fileobj=zf, mode=mode) as t:
                        for rel in plan:
                            t.add(_contained_tmp_path(root, rel), arcname="/".join(rel))
            os.fsync(out_fd)
        finally:
            os.close(out_fd)
        os.rename(tmpname, name, src_dir_fd=destfd, dst_dir_fd=destfd)
        archive_rel = "/".join(dest_dir + [name])
    except Exception as e:
        try:
            os.remove(tmpname, dir_fd=destfd)
        except OSError:
            pass
        if isinstance(e, FilesError):
            raise
        raise FilesError("archive failed: " + str(e)[:120]) from None
    finally:
        os.close(destfd)
    return {"path": archive_rel, "format": fmt, "count": len(plan)}


def _walk_rel(dirfd, base_parts, budget=None):
    """Bounded walk under a verified dirfd yielding (parts, size) for files."""
    budget = [10_000] if budget is None else budget
    out = []
    for entry in os.scandir(dirfd):
        if budget[0] <= 0 or len(out) > _MEMBER_MAX:
            raise FilesError("folder too large to archive", 413)
        budget[0] -= 1
        try:
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                sub = os.open(entry.name, _O_DIR | _O_NOFOLLOW, dir_fd=dirfd)
                try:
                    out.extend(_walk_rel(sub, base_parts + [entry.name], budget))
                finally:
                    os.close(sub)
            elif entry.is_symlink():
                continue  # links are never archived (containment model)
            else:
                out.append((base_parts + [entry.name], entry.stat(follow_symlinks=False).st_size))
        except OSError:
            continue
    return out


def _contained_tmp_path(root, parts):
    """Path string for zipfile/tarfile.add(). The fd-verified entry was
    checked in the plan phase; zipfile/tarfile re-open by path."""
    return os.path.join(root.path, *parts)


def archive_extract(root, arcrel, dest_rel):
    """Safe extraction into a destination directory inside the root (#85).

    Members are validated component-by-component (no absolute paths, no
    '..', no control characters), symlink/hardlink/device members are
    skipped, and the uncompressed total is capped — a malicious archive
    cannot write outside the destination or exhaust the disk."""
    require_writable(root)
    src_parts = split_parts(arcrel)
    if not src_parts:
        raise FilesError("no archive given")
    dest_parts = split_parts(dest_rel)
    cap = config.VALUES.get("archive_cap", 2 * 1024**3)
    src_fd = open_file_fd(root.path, src_parts, os.O_RDONLY)
    destfd = open_dir_fd(root.path, dest_parts)
    written, skipped = 0, 0
    try:
        with os.fdopen(src_fd, "rb", closefd=False) as fobj:
            kind = _archive_kind(src_parts[-1])
            if kind == "zip":
                written, skipped = _extract_zip(fobj, destfd, cap)
            else:
                written, skipped = _extract_tar(
                    fobj, destfd, cap, "r|" + kind.split(":", 1)[1] if ":" in kind else "r|"
                )
    finally:
        os.close(src_fd)
        os.close(destfd)
    return {"dest": "/".join(dest_parts), "files": written, "skipped": skipped}


def _archive_kind(name):
    low = name.lower()
    if low.endswith(".zip"):
        return "zip"
    if low.endswith((".tar.gz", ".tgz")):
        return "r:gz"
    if low.endswith((".tar.xz", ".txz")):
        return "r:xz"
    if low.endswith(".tar"):
        return "r:"
    raise FilesError("unsupported archive format; available: zip, tar, tar.gz, tar.xz", 400)


def _extract_zip(fobj, destfd, cap):
    """zip members: validate each name, skip symlinks, cap uncompressed."""
    remaining = cap
    written = skipped = 0
    with zipfile.ZipFile(fobj) as z:
        infos = z.infolist()
        if len(infos) > _MEMBER_MAX:
            raise FilesError("archive has too many members", 413)
        if sum(i.file_size for i in infos) > cap:
            raise FilesError("archive exceeds the uncompressed size cap", 413)
        for info in infos:
            st_mode = info.external_attr >> 16
            if stat.S_ISLNK(st_mode):
                skipped += 1  # symlink members are never written (#85/#91)
                continue
            parts = _member_parts(info.filename)
            if info.is_dir():
                _ensure_dirs(destfd, parts)
                continue
            dirparts, fname = parts[:-1], parts[-1]
            dest = _ensure_dirs(destfd, dirparts) if dirparts else destfd
            try:
                dst_fd = _openat_file(dest, fname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with z.open(info) as src:
                        _stream_bounded(src, dst_fd, info.file_size)
                finally:
                    os.close(dst_fd)
                written += 1
            finally:
                if dest is not destfd:
                    os.close(dest)
    return written, skipped


def _extract_tar(fobj, destfd, cap, tar_mode="r|"):
    """tar members: links/devices/fifos skipped, names validated, capped."""
    remaining = cap
    written = skipped = 0
    with tarfile.open(fileobj=fobj, mode=tar_mode) as t:
        for m in t:
            if written > _MEMBER_MAX:
                raise FilesError("archive has too many members", 413)
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                skipped += 1  # never recreate links/devices from archives (#85)
                continue
            parts = _member_parts(m.name)
            if m.isdir():
                _ensure_dirs(destfd, parts)
                continue
            if not m.isfile():
                skipped += 1
                continue
            remaining -= m.size
            if remaining < 0:
                raise FilesError("archive exceeds the uncompressed size cap", 413)
            dirparts, fname = parts[:-1], parts[-1]
            dest = _ensure_dirs(destfd, dirparts) if dirparts else destfd
            try:
                src = t.extractfile(m)
                if src is None:
                    skipped += 1
                    continue
                dst_fd = _openat_file(dest, fname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    shutil.copyfileobj(src, os.fdopen(os.dup(dst_fd), "wb", closefd=True), length=1 << 18)
                finally:
                    os.close(dst_fd)
                written += 1
            finally:
                if dest is not destfd:
                    os.close(dest)
    return written, skipped


def _stream_bounded(src, dst_fd, expected_size):
    """Copy a file-like into a raw fd, refusing to exceed the declared
    member size (lying headers must not hide a zip bomb)."""
    total = 0
    with os.fdopen(os.dup(dst_fd), "wb", closefd=True) as dst:
        while True:
            b = src.read(1 << 18)
            if not b:
                break
            total += len(b)
            if total > expected_size + (1 << 20):
                raise FilesError("member larger than declared")
            dst.write(b)


# ---------------------------------------------------------------- thumbnails & previews (#82)

THUMB_SOURCE_CAP = 8 * 1024**2  # inline-original cap for small images


def thumbs_dir():
    d = os.path.join(_state_base(), "files-thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def mime_for(name):
    """MIME by extension (never from contents)."""
    from .http import MIME

    return MIME.guess_type(name)[0] or "application/octet-stream"


UNSAFE_INLINE = ("image/svg+xml", "text/html", "application/xhtml+xml")  # #82: never inline


def inline_ok(mime):
    return mime not in UNSAFE_INLINE and not mime.endswith("xml")


def thumbnail(root, relpath, size=256):
    """Thumbnail plan for a contained file (#82).

    Returns ("source", mime) when the small original can be streamed
    inline, or (cache_path, "image/jpeg") after generating (and caching) a
    bounded thumbnail with the host's image tools. Raises FilesError
    (415 unsafe/unsupported type, 501 no tool) so the client falls back to
    a download action. The cache key embeds mtime+size as the invalidation
    signal; generation runs argv-only on a private temp copy of the source
    (never a shell, never a path inside the root — #82/#91)."""
    parts = split_parts(relpath)
    if not parts:
        raise FilesError("no file given")
    st = stat_contained(root.path, parts)
    name = parts[-1]
    mime = mime_for(name)
    if mime in UNSAFE_INLINE:
        raise FilesError("unsafe inline type; download instead", 415)
    # 1) small raster images: the original IS the thumbnail (bounded cap)
    if mime.startswith("image/") and st.st_size <= THUMB_SOURCE_CAP:
        return ("source", mime)
    # 2) generate: images / PDFs / video first frames
    if mime == "application/pdf":
        kind = "pdf"
    elif mime.startswith("video/"):
        kind = "video"
    elif mime.startswith("image/"):
        kind = "image"
    else:
        raise FilesError("no preview available for this type; download instead", 415)
    tool_args = _thumb_tool(kind)
    if tool_args is None:
        raise FilesError("preview generation unavailable (no convert/pdftoppm/ffmpeg on the host)", 501)
    size = max(16, min(int(size or 256), 1024))
    key = hashlib.sha256(f"{root.rid}|{'/'.join(parts)}|{st.st_mtime_ns}|{st.st_size}|{size}".encode()).hexdigest()[:24]
    cache = os.path.join(thumbs_dir(), key + ".jpg")
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        return (cache, "image/jpeg")
    src_fd = open_file_fd(root.path, parts, os.O_RDONLY)
    tmp_src = os.path.join(thumbs_dir(), f".src-{key}")
    tmp_out = os.path.join(thumbs_dir(), f".out-{key}.jpg")
    try:
        with open(tmp_src, "wb") as t:
            with os.fdopen(src_fd, "rb", closefd=False) as s:
                shutil.copyfileobj(s, t, length=1 << 18)
        cmd = []
        for a in tool_args:
            cmd.append(tmp_src if a is None else a.replace("{size}", str(size)))
        cmd.append(tmp_out)
        try:
            subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        except (subprocess.SubprocessError, OSError):
            raise FilesError("preview generation failed", 501) from None
        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            raise FilesError("preview generation failed", 501)
        os.replace(tmp_out, cache)
        try:
            os.chmod(cache, 0o600)
        except OSError:
            pass
        return (cache, "image/jpeg")
    finally:
        try:
            os.close(src_fd)
        except OSError:
            pass
        for t in (tmp_src, tmp_out):
            try:
                os.remove(t)
            except OSError:
                pass


def _thumb_tool(kind):
    """Probe (once) for an image tool able to handle this kind. argv-only;
    None marks the input-file position (#82: uploaded content is never
    executed — it is only ever passed as a data file)."""
    if kind == "pdf" and shutil.which("pdftoppm"):
        return ["pdftoppm", "-jpeg", "-f", "1", "-singlefile", "-scale-to", "{size}", None]
    if kind == "pdf" and shutil.which("convert"):
        return ["convert", "-thumbnail", "{size}x{size}", None, "[0]"]
    if kind == "image" and shutil.which("convert"):
        return ["convert", "-auto-orient", "-thumbnail", "{size}x{size}", None]
    if kind == "video" and shutil.which("ffmpeg"):
        return ["ffmpeg", "-loglevel", "error", "-y", "-i", None, "-vframes", "1", "-vf", "scale={size}:-1"]
    if shutil.which("convert"):
        return ["convert", "-thumbnail", "{size}x{size}", None]
    if shutil.which("ffmpeg"):
        return ["ffmpeg", "-loglevel", "error", "-y", "-i", None, "-vframes", "1", "-vf", "scale={size}:-1"]
    return None
