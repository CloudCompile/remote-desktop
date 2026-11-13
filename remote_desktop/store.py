"""App store: search and background install via apt (Debian/Ubuntu).

Ownership: dpkg/appstream/apt-cache queries and the install-progress
tracking for /api/store*. Installs run `sudo apt-get` in the background
by design — see docs/DEPLOYMENT.md for the privilege model (#33).
"""

import os
import re
import shutil
import subprocess
import time

from . import config, logging as log, procs

INSTALL_LOG = "/tmp/remote-install.log"
_INSTALL_CACHE = {}
_INSTALL_CACHE_TTL = 30  # seconds; dpkg -s on every request made the store sluggish
_INSTALLING = {}  # pkg -> install start time (for progress reporting)

_PKG_RE = re.compile(r"[a-z0-9][a-z0-9+.\-]*")


def valid_pkg_name(pkg):
    return bool(_PKG_RE.fullmatch(pkg or ""))


def is_installed(pkg):
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


def store_search(q):
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


def install_pkg(pkg):
    if not valid_pkg_name(pkg):
        return {"ok": False, "error": "invalid package name"}
    _INSTALL_CACHE.pop(pkg, None)
    _INSTALLING[pkg] = time.time()
    subprocess.Popen(
        ["sudo", "apt-get", "install", "-y", pkg],
        env=config._env(),
        stdout=subprocess.DEVNULL,
        stderr=open(INSTALL_LOG, "ab"),
        start_new_session=True,
    )
    log.log("store: install requested", pkg=pkg)
    return {"ok": True, "status": "installing"}


def install_status(pkg):
    installed = is_installed(pkg)
    if installed:
        _INSTALLING.pop(pkg, None)
        return {"installed": True, "installing": False}
    installing = pkg in _INSTALLING and time.time() - _INSTALLING[pkg] < 900
    return {"installed": False, "installing": installing}
