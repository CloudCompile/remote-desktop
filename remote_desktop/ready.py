"""Readiness probes for X, VNC, PulseAudio and ffmpeg (#40).

Ownership: the dependency checks behind /api/ready. Each probe is cached
for RD_READY_CACHE seconds and bounded by its own timeout, so the endpoint
never blocks meaningfully and stays truthful when a dependency dies.
/api/health stays fast and probe-free (liveness vs readiness).

Wiring: point systemd (`ExecStartPost=`), a tunnel health check or a
k8s-style probe at GET /api/ready; it answers 200 only when every
dependency reports ok. UI status display is owned by web/.
"""

import concurrent.futures
import shutil
import socket
import subprocess
import threading
import time

from . import config, logging as log

_PROBE_TIMEOUT = 1.0  # per-probe hard bound (seconds)
_cache_lock = threading.Lock()
_cache = {}  # name -> (expiry_ts, ok, reason)


def probe_x():
    """X display reachable (xdotool can talk to it)."""
    try:
        p = subprocess.run(
            ["xdotool", "getdisplaygeometry"], env=config._env(), capture_output=True, text=True, timeout=_PROBE_TIMEOUT
        )
        if p.returncode == 0 and len(p.stdout.split()) >= 2:
            return True, p.stdout.strip()
        return False, (p.stderr.strip() or f"xdotool rc={p.returncode}")[:120]
    except FileNotFoundError:
        return False, "xdotool not installed"
    except subprocess.TimeoutExpired:
        return False, f"xdotool timed out after {_PROBE_TIMEOUT:.0f}s"
    except Exception as e:
        return False, str(e)[:120]


def probe_vnc():
    """VNC TCP endpoint accepting connections."""
    try:
        s = socket.create_connection((config.VNC_HOST, config.VNC_PORT), timeout=_PROBE_TIMEOUT)
        s.close()
        return True, f"{config.VNC_HOST}:{config.VNC_PORT}"
    except Exception as e:
        return False, f"{config.VNC_HOST}:{config.VNC_PORT}: {e}"[:120]


def probe_pulse():
    """PulseAudio daemon answering pactl."""
    try:
        p = subprocess.run(
            ["pactl", "info"], env=config._pa_env(), capture_output=True, text=True, timeout=_PROBE_TIMEOUT
        )
        if p.returncode == 0:
            return True, "pactl info ok"
        return False, (p.stderr.strip() or f"pactl rc={p.returncode}")[:120]
    except FileNotFoundError:
        return False, "pactl not installed"
    except subprocess.TimeoutExpired:
        return False, f"pactl timed out after {_PROBE_TIMEOUT:.0f}s"
    except Exception as e:
        return False, str(e)[:120]


def probe_ffmpeg():
    """ffmpeg binary present (audio streaming capability)."""
    path = shutil.which("ffmpeg")
    return (True, path) if path else (False, "ffmpeg not installed")


PROBES = {"x": probe_x, "vnc": probe_vnc, "pulse": probe_pulse, "ffmpeg": probe_ffmpeg}


def _one_line(text, limit=120):
    """Probe reasons are logged — squash newlines so records stay single-line."""
    return " ".join(str(text).split())[:limit]


def ready_cache_clear():
    """Forget cached probe results (used by tests and after a repair)."""
    with _cache_lock:
        _cache.clear()


def ready_snapshot(max_age=None):
    """Per-dependency status with reasons (#40).

    Each dependency is probed only when its cached result is older than
    RD_READY_CACHE seconds; stale probes run in parallel, each bounded by
    its own timeout, so the endpoint answers in well under a second even
    when something is down. Returns
    {"ok": bool, "dependencies": {name: {ok, reason, cached_s}}}."""
    del max_age  # cache TTL comes from config; kept for callers' clarity
    now = time.time()
    with _cache_lock:
        stale = {n: fn for n, fn in PROBES.items() if n not in _cache or _cache[n][0] <= now}
    if stale:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(stale)) as ex:
            futures = {ex.submit(fn): n for n, fn in stale.items()}
            for fut, name in futures.items():
                try:
                    ok, reason = fut.result(timeout=_PROBE_TIMEOUT + 1)
                except Exception as e:  # a probe must never hang the endpoint
                    ok, reason = False, str(e)[:120]
                with _cache_lock:
                    _cache[name] = (time.time() + config.READY_CACHE, ok, reason)
    deps = {}
    with _cache_lock:
        for n in PROBES:
            _, ok, reason = _cache.get(n, (0, False, "not probed"))
            deps[n] = {
                "ok": ok,
                "reason": _one_line(reason),
                "cached_s": round(max(0.0, _cache[n][0] - time.time()), 1) if n in _cache else 0,
            }
    ok = all(d["ok"] for d in deps.values())
    return {"ok": ok, "dependencies": deps}


def ready_once():
    """First readiness evaluation at startup — logged once (#40)."""
    snap = ready_snapshot()
    log.log(
        "readiness: " + ("ready" if snap["ok"] else "degraded"),
        **{k: ("ok" if v["ok"] else f"fail: {v['reason']}") for k, v in snap["dependencies"].items()},
    )
    return snap
