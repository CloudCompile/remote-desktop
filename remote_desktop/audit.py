"""Tamper-evident audit log (#113): hash-chained JSONL events.

Ownership: the security audit trail — one structured event per
security-relevant action (auth, session, files, apps, store, admin),
hash chaining so editing or deleting an old record is detectable, size-
capped rotation, and a bounded read/filter API for the admin UI.

Chain design: each record carries ``prev`` (the hex digest of the
previous record) and ``h = sha256(prev + canonical_json(record))``.
Truncation or edit anywhere breaks every hash after it, so tampering is
detectable without any key material ([ADR-0002](adr/ADR-0002-stdlib-only.md)
— no signatures infrastructure needed for a host-local log; the chain
detects in-place edits, the rotation generations make deletion visible).

Privacy (#24/#113): passwords, cookies, tokens and clipboard CONTENTS
are never recorded — only actor, IP, request id, target and result.
Writes never break the operation being audited.
"""

import hashlib
import json
import os
import threading
import time
import unicodedata

from . import config, logging as log

# event kinds (the "domain" of the action)
AUTH, SESSION, FILES, APP, STORE, ADMIN, SECURITY, NOTIFY = (
    "auth",
    "session",
    "files",
    "app",
    "store",
    "admin",
    "security",
    "notify",
)
KINDS = (AUTH, SESSION, FILES, APP, STORE, ADMIN, SECURITY, NOTIFY)

AUDIT_MAX = 512 * 1024  # rotate threshold (byte size of the live file)
AUDIT_KEEP = 3  # rotated generations kept (plus the live file)

AUDIT_FILE = None  # test override; default: STATE_DIR/audit.jsonl

_lock = threading.Lock()
_chains = {}  # path -> last hash (recovered from the tail on first use)


def _path():
    return AUDIT_FILE or os.path.join(config.STATE_DIR, "audit.jsonl")


def _clean(value, cap=400):
    s = str(value)[:cap] if value is not None else ""
    return "".join(ch if unicodedata.category(ch) != "Cc" else "?" for ch in s)


def _canonical(rec):
    return json.dumps(rec, sort_keys=True, separators=(",", ":"))


def _digest(prev, rec_no_chain):
    body = dict(rec_no_chain)
    body["prev"] = prev
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _recover_tail(path):
    """Last record's hash for the chain (bounded tail read)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        for ln in reversed(tail):
            try:
                rec = json.loads(ln)
                if isinstance(rec.get("h"), str):
                    return rec["h"]
            except ValueError:
                continue
    except OSError:
        pass
    return "0" * 64  # genesis


def append_record(path, rec, max_bytes=None, keep=None):
    """Append ONE record to the chain at `path` (rotation when the live
    file crosses max_bytes, keeping `keep` generations). Never raises.
    Returns the record with prev/h filled in, or None."""
    max_bytes = max_bytes or AUDIT_MAX
    keep = keep or AUDIT_KEEP
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
    try:
        with _lock:
            last = _chains.get(path)
            if last is None:
                last = _chains[path] = _recover_tail(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
                _chains[path] = last = _recover_tail(path)
            if size + 512 > max_bytes:  # rotate: .N-1 -> .N ... live -> .1
                for i in range(keep, 0, -1):
                    src = path if i == 1 else f"{path}.{i - 1}"
                    dst = f"{path}.{i}"
                    if os.path.exists(src):
                        # shifting into the last slot prunes it (replace)
                        os.replace(src, dst)
            rec = dict(rec)
            rec["prev"] = last
            rec["h"] = _digest(last, rec)
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            _chains[path] = rec["h"]
            return rec
    except OSError as e:
        log.log(f"audit append failed: {e}")
        return None


def event(
    kind,
    action,
    user=None,
    handler=None,
    target=None,
    result="ok",
    detail=None,
    path=None,
    size=None,
):
    """Record one security-relevant event (#113). `handler` supplies the
    client ip + request id when available. Never contains secrets."""
    try:
        from . import auth

        rec = {
            "ts": int(time.time()),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "kind": _clean(kind, 20),
            "action": _clean(action, 40),
            "user": _clean(user if user is not None else (getattr(handler, "user", None) or auth.AUTH_USER), 80),
            "ip": _clean(auth._client_ip(handler) if handler else "-", 64),
            "rid": _clean(getattr(handler, "rid", "-") if handler else "-", 16),
            "target": _clean(target, 300) if target else None,
            "result": _clean(result, 20),
        }
        if size is not None:  # #92: transfer rows carry the byte count
            rec["size"] = int(size)
        if detail:
            rec["detail"] = {
                _clean(str(k), 40): _clean(v, 200)
                for k, v in list(detail.items())[:8]
                if isinstance(v, (str, int, bool))
            }
        return append_record(path or _path(), rec)
    except Exception:
        return None


def read(path=None, kind=None, q=None, limit=200):
    """Newest-last records from the live file (+.1 generation so a recent
    rotation is not lost), optionally filtered by kind and a substring of
    action/user/target. Bounded read."""
    path = path or _path()
    out = []
    # all kept generations (oldest first) + live, across the rotation seam;
    # AUDIT_KEEP may be overridden by tests, so enumerate what exists
    gens = [f"{path}.{i}" for i in range(min(max(AUDIT_KEEP, 1), 16), 0, -1)] + [path]
    for p in gens:
        try:
            with open(p, "rb") as f:
                data = f.read(2 * AUDIT_MAX)
            lines = data.decode("utf-8", "replace").strip().splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if kind and rec.get("kind") != kind:
                continue
            if q:
                hay = " ".join(str(rec.get(k) or "") for k in ("action", "user", "target", "ip", "rid")).lower()
                if q.lower() not in hay:
                    continue
            out.append(rec)
    # the .1 generation and the live file can overlap at the seam boundary;
    # de-duplicate by chain hash keeping order
    seen, dedup = set(), []
    for rec in out:
        h = rec.get("h")
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        dedup.append(rec)
    return dedup[-max(1, min(int(limit or 200), 1000)) :]


def verify(path=None):
    """Re-walk the chain across every generation (.N ... .1, live):
    (ok, first_bad_index, checked). Detects edited records and removed
    generations (#113 acceptance).

    The OLDEST kept generation starts a new chain root: older generations
    are pruned by rotation, so their hashes are legitimately absent. The
    root entry is still tamper-checked (its own `h` must match its body),
    and every following record must link to it."""
    path = path or _path()
    gens = []
    for i in range(AUDIT_KEEP, 0, -1):
        p = f"{path}.{i}"  # .N = rotated generations; the live file has no suffix
        if os.path.exists(p):
            gens.append(p)
    if os.path.exists(path):  # live file, always checked last
        gens.append(path)
    prev = None  # seeded by the oldest kept generation's first record
    i = 0
    for gen in gens:
        try:
            with open(gen, "rb") as f:
                for raw in f:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        return (False, i, i)
                    if prev is not None and rec.get("prev") != prev:
                        return (False, i, i)
                    body = {k: v for k, v in rec.items() if k != "h"}
                    expect = _digest(rec.get("prev") or "0" * 64, body)
                    if expect != rec.get("h"):
                        return (False, i, i)
                    prev = rec["h"]
                    i += 1
        except OSError:
            continue
    return (True, -1, i)
