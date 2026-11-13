"""App store: a backend interface across apt/dnf/pacman (#67) plus the
serial job queue with uninstall, update and live logs (#66) and the
curated collections (#69).

Ownership: package-manager detection and operations, install-progress
tracking for /api/store*, and the per-job log tails. Real behaviour is
implemented for apt only — dnf/pacman are detected and reported as
unsupported so the UI can hide the buttons honestly (#67).

Privilege model: operations run `sudo apt-get` in the background by
design — see docs/DEPLOYMENT.md (#33). Exactly ONE package-manager
operation runs at a time (#66): everything else waits in a FIFO queue
and reports its position through install_status.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import config, logging as log, procs

LEGACY_INSTALL_LOG = "/tmp/remote-install.log"
JOB_DIR = os.path.join(tempfile.gettempdir(), "remote-install-jobs")
JOBS_FILE = os.path.join(JOB_DIR, "jobs.json")
LOG_TAIL_LINES = 20  # surfaced per job in install_status (#66)
JOB_TTL = 900  # a job older than this no longer counts as "installing"

_INSTALL_CACHE = {}
_INSTALL_CACHE_TTL = 30  # seconds; dpkg -s on every request made the store sluggish

_PKG_RE = re.compile(r"[a-z0-9][a-z0-9+.\-]*")


def valid_pkg_name(pkg):
    return bool(_PKG_RE.fullmatch(pkg or ""))


# ------------------------------------------------------------- backends
# #67: one interface, apt implemented; dnf/pacman stubs report themselves
# unsupported so /api/store/backend can drive the UI honestly.


class StoreBackend:
    name = "generic"
    env_name = ""

    @property
    def supported(self):
        return False

    def available(self):
        return bool(shutil.which(self.env_name, path=config._env().get("PATH")))

    def capabilities(self):
        return {"search": False, "install": False, "remove": False, "update": False, "status": False}

    def search(self, q):  # pragma: no cover - interface
        return None

    def is_installed(self, pkg):
        return False

    def install_cmd(self, pkg):
        return None

    def remove_cmd(self, pkg):
        return None

    def update_cmd(self, pkg):
        return None


class AptBackend(StoreBackend):
    name = "apt"
    env_name = "apt-get"

    @property
    def supported(self):
        return True

    def capabilities(self):
        return {"search": True, "install": True, "remove": True, "update": True, "status": True}

    def search(self, q):
        return apt_search(q)

    def is_installed(self, pkg):
        now = time.time()
        hit = _INSTALL_CACHE.get(pkg)
        if hit and now - hit[0] < _INSTALL_CACHE_TTL:
            return hit[1]
        rc, _, _ = procs._run(["dpkg", "-s", pkg], timeout=4)
        val = rc == 0
        if not val:
            val = shutil.which(pkg, path=config._env().get("PATH")) is not None
        _INSTALL_CACHE[pkg] = (now, val)
        return val

    def install_cmd(self, pkg):
        return ["sudo", "apt-get", "install", "-y", pkg]

    def remove_cmd(self, pkg):
        return ["sudo", "apt-get", "remove", "-y", pkg]

    def update_cmd(self, pkg):
        return ["sudo", "apt-get", "install", "--only-upgrade", "-y", pkg]


class DnfBackend(StoreBackend):
    """Stub (#67): detected and reported, operations refuse honestly."""

    name = "dnf"
    env_name = "dnf"

    def capabilities(self):
        return {"search": False, "install": False, "remove": False, "update": False, "status": False}

    def is_installed(self, pkg):
        return shutil.which(pkg, path=config._env().get("PATH")) is not None


class PacmanBackend(DnfBackend):
    name = "pacman"
    env_name = "pacman"


_BACKENDS = (AptBackend(), DnfBackend(), PacmanBackend())
_backend_lock = threading.Lock()
_backend = None


def detect_backend():
    """Pick the first available backend, apt preferred (#67)."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = next((b for b in _BACKENDS if b.available()), None)
        return _backend


def reset_backend_cache():
    global _backend
    with _backend_lock:
        _backend = None


def backend_info():
    be = detect_backend()
    if not be:
        return {
            "backend": "none",
            "supported": False,
            "capabilities": {c: False for c in AptBackend().capabilities()},
            "error": "no supported package manager found (apt, dnf or pacman)",
        }
    return {
        "backend": be.name,
        "supported": be.supported,
        "capabilities": be.capabilities(),
        "error": None if be.supported else f"the {be.name} backend is not implemented yet; apt is fully supported",
    }


def _unsupported():
    info = backend_info()
    return {"ok": False, "error": info.get("error") or "package operations are not supported on this system"}


def apt_search(q):
    """AppStream search with an apt-cache fallback (apt only)."""
    rows = []
    rc, out, _ = procs._run(["appstreamcli", "search", q], timeout=30)
    if rc == 0:
        cur = {}
        blocks = []
        for line in out.splitlines():
            if line.strip() == "---":
                blocks.append(cur)
                cur = {}
            elif ":" in line:
                k, v = line.split(":", 1)
                cur[k.strip()] = v.strip()
        blocks.append(cur)
        for cur in blocks:
            if not cur.get("Package"):
                continue
            label = cur.get("Name", cur["Package"])
            desc = cur.get("Summary", "")
            if q.lower() in (label + " " + desc + " " + cur["Package"]).lower():
                rows.append(
                    {"name": cur["Package"], "label": label, "desc": desc, "installed": is_installed(cur["Package"])}
                )
    if not rows:  # fall back to apt-cache
        rc, out, _ = procs._run(["apt-cache", "search", q], timeout=15)
        if rc == 0:
            for line in out.splitlines():
                if " - " in line:
                    name, desc = line.split(" - ", 1)
                    name = name.strip()
                    rows.append({"name": name, "label": name, "desc": desc.strip(), "installed": is_installed(name)})
    return rows[:60]


# ----------------------------------------------------------- public API


def is_installed(pkg):
    be = detect_backend()
    if be:
        return be.is_installed(pkg)
    return shutil.which(pkg, path=config._env().get("PATH")) is not None


def store_search(q):
    be = detect_backend()
    if be and be.supported:
        return be.search(q)
    return []  # unsupported backends search nothing rather than failing


def _persist_jobs():
    try:
        os.makedirs(JOB_DIR, exist_ok=True)
        with open(JOBS_FILE + ".tmp", "w") as f:
            json.dump(
                [
                    {k: v for k, v in j.items() if k not in ("proc",)}
                    for j in list(_JOBS.values())[-20:]
                ],
                f,
            )
        os.replace(JOBS_FILE + ".tmp", JOBS_FILE)
    except Exception:
        pass


def _load_persisted_jobs():
    """Job status survives a server restart (#66): running jobs become
    'interrupted' (their child died with us), queued jobs stay queued."""
    try:
        with open(JOBS_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            for j in raw:
                if isinstance(j, dict) and valid_pkg_name(j.get("pkg", "")):
                    j["state"] = "interrupted" if j.get("state") in ("running", "queued") else j.get("state")
                    _JOBS[j["pkg"]] = j
    except Exception:
        pass


restore_jobs = _load_persisted_jobs  # called from server.main() (no IO at import)


# serial queue (#66): one package-manager operation at a time
_queue_lock = threading.Lock()
_queue = []  # FIFO of pending job dicts
_JOBS = {}  # pkg -> latest job dict (for status lookups)
_worker_running = False


def _enqueue(op, pkg):
    job = {
        "op": op,
        "pkg": pkg,
        "state": "queued",
        "ts": time.time(),
        "rc": None,
        "log": os.path.join(JOB_DIR, f"{op}-{pkg}-{int(time.time())}.log"),
    }
    with _queue_lock:
        existing = _JOBS.get(pkg)
        if existing and existing.get("state") in ("queued", "running"):
            return {"ok": False, "error": f"{pkg} already has a {existing['state']} job"}
        _INSTALL_CACHE.pop(pkg, None)
        _JOBS[pkg] = job
        _queue.append(job)
        _persist_jobs()
        _ensure_worker()
    log.log(f"store: {op} queued", pkg=pkg)
    return {"ok": True, "status": "queued"}


def _ensure_worker():
    global _worker_running
    if _worker_running:
        return
    _worker_running = True
    threading.Thread(target=_worker, daemon=True).start()


def _worker():
    global _worker_running
    while True:
        with _queue_lock:
            job = _queue[0] if _queue else None
            if job is None:
                _worker_running = False
                return
        _run_job(job)
        with _queue_lock:
            if job in _queue:
                _queue.remove(job)


def _run_job(job):
    """Run one queued package operation to completion; apt output is
    appended to the job's own log file (#66)."""
    be = detect_backend()
    pkg = job["pkg"]
    cmd = {
        "install": be.install_cmd if be else None,
        "remove": be.remove_cmd if be else None,
        "update": be.update_cmd if be else None,
    }.get(job["op"])
    cmd = cmd(pkg) if cmd else None
    if not cmd:
        job["state"] = "failed"
        job["rc"] = -1
        _persist_jobs()
        return
    job["state"] = "running"
    job["started"] = time.time()
    _persist_jobs()
    try:
        os.makedirs(JOB_DIR, exist_ok=True)
        with open(job["log"], "ab") as logf, open(LEGACY_INSTALL_LOG, "ab") as legacy:
            p = subprocess.Popen(cmd, env=config._env(), stdout=logf, stderr=legacy, start_new_session=True)
            job["proc"] = p
            rc = p.wait()
    except Exception as e:
        rc = -1
        try:
            with open(job["log"], "ab") as logf:
                logf.write(f"could not start {cmd[0]}: {e}\n".encode())
        except Exception:
            pass
    job["rc"] = rc
    job["state"] = "done" if rc == 0 else "failed"
    job["finished"] = time.time()
    job.pop("proc", None)
    _INSTALL_CACHE.pop(pkg, None)
    _persist_jobs()
    log.log(f"store: {job['op']} {pkg} -> {job['state']} (rc={rc})")


def _log_tail(path, lines=LOG_TAIL_LINES):
    """Last N lines of a job log, secrets-free by construction (apt
    output never contains the password; the hash lives in .passhash)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))  # bounded read: the tail is enough
            data = f.read().decode(errors="replace")
        return data.splitlines()[-lines:]
    except OSError:
        return []


def install_pkg(pkg):
    if not valid_pkg_name(pkg):
        return {"ok": False, "error": "invalid package name"}
    if not _supported_or_none():
        return _unsupported()
    r = _enqueue("install", pkg)
    return r


def remove_pkg(pkg):
    """Uninstall (#66): same privilege path, same queue."""
    if not valid_pkg_name(pkg):
        return {"ok": False, "error": "invalid package name"}
    if not _supported_or_none():
        return _unsupported()
    return _enqueue("remove", pkg)


def update_pkg(pkg):
    """Upgrade one package (#66): apt-get install --only-upgrade."""
    if not valid_pkg_name(pkg):
        return {"ok": False, "error": "invalid package name"}
    if not _supported_or_none():
        return _unsupported()
    return _enqueue("update", pkg)


def cancel_pkg(pkg):
    """Cancel a queued job or terminate the running child (#66). SIGTERM
    lets apt release its locks cleanly, leaving the system recoverable."""
    if not valid_pkg_name(pkg):
        return {"ok": False, "error": "invalid package name"}
    with _queue_lock:
        for j in list(_queue):
            if j["pkg"] == pkg:
                _queue.remove(j)
                j["state"] = "cancelled"
                j["finished"] = time.time()
                _persist_jobs()
                return {"ok": True, "cancelled": "queued"}
        job = _JOBS.get(pkg)
        p = (job or {}).get("proc")
        if job and job.get("state") == "running" and p:
            try:
                p.terminate()
            except Exception:
                pass
            return {"ok": True, "cancelled": "running"}
    return {"ok": False, "error": "no active job for this package"}


def _supported_or_none():
    be = detect_backend()
    return be if (be and be.supported) else None


def install_status(pkg):
    """Status for one package (#66): installed / installing / queued
    position / last log lines. Legacy keys kept."""
    installed = is_installed(pkg)
    with _queue_lock:
        job = _JOBS.get(pkg)
        state_now = job.get("state") if job else None
        pending = [j for j in _queue if j.get("state") == "queued" and j["pkg"] != pkg]
        queue_pos = (next((i for i, j in enumerate(_queue) if j["pkg"] == pkg), None))
        queue_pos = queue_pos + 1 if queue_pos is not None else None
    installing = bool(job) and state_now == "running" and time.time() - job.get("ts", 0) < JOB_TTL
    if installed and state_now in (None, "running", "queued"):
        state_now = "done" if state_now == "running" else state_now
    out = {
        "installed": installed,
        "installing": installing or (state_now == "queued"),
        "state": state_now or "",
        "queue_pos": queue_pos,
        "queued_ahead": len(pending),
        "log": _log_tail(job["log"]) if job else [],
    }
    return out


# ------------------------------------------------------------ collections
# #69: curated collections — static, offline, never auto-install. A
# STATE_DIR/collections.json override replaces the shipped catalog.


def collections_file():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "data", "collections.json")


def collections():
    """The curated catalog: shipped JSON, overridden by
    ~/.local/share/remote-desktop/collections.json when present."""
    for path in (os.path.join(config.STATE_DIR, "collections.json"), collections_file()):
        try:
            with open(path) as f:
                raw = json.load(f)
            cols = raw.get("collections") if isinstance(raw, dict) else raw
            if isinstance(cols, list) and cols:
                out = []
                for c in cols[:12]:
                    if not isinstance(c, dict) or not isinstance(c.get("apps"), list):
                        continue
                    out.append(
                        {
                            "id": str(c.get("id") or c.get("label") or "")[:40],
                            "label": str(c.get("label") or "Collection")[:60],
                            "apps": [
                                {
                                    "pkg": str(a.get("pkg") or "")[:80],
                                    "label": str(a.get("label") or a.get("pkg") or "")[:60],
                                    "desc": str(a.get("desc") or "")[:120],
                                }
                                for a in c["apps"]
                                if isinstance(a, dict) and valid_pkg_name(a.get("pkg") or "")
                            ][:8],
                        }
                    )
                out = [c for c in out if c["apps"]]
                if out:
                    return out
        except Exception:
            continue
    return []


def collections_with_status():
    cols = collections()
    for c in cols:
        for a in c["apps"]:
            a["installed"] = is_installed(a["pkg"])
    return cols
