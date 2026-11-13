"""Stats: system metrics and the public health snapshot.

Ownership: /api/stats payloads (CPU/mem/disk/uptime, live connection
counts, geometry) and /api/health (deliberately minimal liveness — no
host details, no user data; kept independent of dependency probes, see
ready.py for /api/ready).
"""

import os
import shutil
import time

from . import config, limits, procs, vncproxy, windows

_START_TIME = time.time()


def system_stats():
    """CPU / memory / disk / uptime snapshot for the More sheet."""

    def cpu_sample():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        return sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)  # total, idle

    t0, i0 = cpu_sample()
    time.sleep(0.25)
    t1, i1 = cpu_sample()
    dt = t1 - t0
    cpu = max(0.0, min(100.0, 100 * (1 - (i1 - i0) / dt))) if dt else 0.0
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.strip().split()[0]) * 1024
    du = shutil.disk_usage(os.path.expanduser("~"))
    try:
        with open("/proc/uptime") as f:
            uptime = int(float(f.read().split()[0]))
    except Exception:
        uptime = 0
    stats = {
        "cpu_pct": round(cpu, 1),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cores": os.cpu_count() or 1,
        "mem_used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
        "mem_total": mem.get("MemTotal", 0),
        "disk_used": du.used,
        "disk_total": du.total,
        "uptime_s": uptime,
        # 28: live connection/stream counters
        "conns": limits.snapshot(),
        # 29: websocket proxy counters
        "ws": vncproxy.stats_snapshot(),
    }
    geo = windows.display_geometry()  # 31: current geometry
    if geo:
        stats["geometry"] = geo
    return stats


def health():
    """Public liveness/version snapshot (#18). Deliberately minimal: no host
    details, no user data."""
    return {
        "ok": True,
        "version": config.VERSION,
        "time": int(time.time()),
        "uptime_s": int(time.time() - _START_TIME),
        "active_streams": procs.active_children(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "pactl": bool(shutil.which("pactl")),
    }
