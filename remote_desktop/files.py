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
import os
import secrets
import shutil
import stat
import time
import unicodedata

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
    """A file operation was refused or failed; the message is safe to show."""


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
                raise FilesError("refused: symlink") from None
        except FileNotFoundError:
            raise FilesError("not found") from None
        except OSError:
            if e.errno == errno.ELOOP:
                raise FilesError("refused: symlink") from None
            raise FilesError("cannot open: " + _oserr(e)) from None
        if e.errno == errno.ENOTDIR:
            raise FilesError("not a directory") from None
        if e.errno == errno.ELOOP:
            raise FilesError("refused: symlink") from None
        raise FilesError("cannot open: " + _oserr(e)) from None


def _openat_file(fd, name, flags, mode=0o600):
    """Open one file component with O_NOFOLLOW; the kernel-atomic antidote
    to the classic verify-then-open symlink race (#91)."""
    try:
        return os.open(name, flags | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=fd)
    except FileExistsError:
        raise FilesError("already exists") from None
    except FileNotFoundError:
        raise FilesError("not found") from None
    except IsADirectoryError:
        raise FilesError("is a directory") from None
    except OSError as e:
        if e.errno == errno.ELOOP:  # final component is a symlink
            raise FilesError("refused: symlink") from None
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
        raise FilesError("not found") from None
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
            raise FilesError("not found") from None
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
            raise FilesError(f"name collides with existing '{other}' (case/Unicode)")


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
        raise FilesError("no such root")
    if root.readonly:
        raise FilesError("root is read-only")


def default_root():
    return ROOTS[0] if ROOTS else None


def roots_public():
    return [r.public() for r in ROOTS]
