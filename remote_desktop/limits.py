"""Concurrency limits: bounded connections and streams (#28).

Ownership: every capability cap and its live counter. The server's socket
layer (server.py) enforces the connection cap; request handlers acquire a
kind-specific slot before starting long-lived streams and answer 503 with
`Retry-After` when a cap is full, instead of growing without bound.

Caps (env-configurable, see docs/CONFIGURATION.md):
- connections  RD_MAX_CONNECTIONS  64   TCP connections (≈ threads)
- ws           RD_MAX_WS           32   /websockify VNC proxies
- audio        RD_MAX_AUDIO        8    ffmpeg audio-out streams
- media_in     RD_MAX_MEDIA_IN     2    mic/cam device-in streams
"""

import threading

from . import config, logging as log

_lock = threading.Lock()
_counts = {"connections": 0, "ws": 0, "audio": 0, "media_in": 0}

_CAPS = {
    "connections": config.MAX_CONNECTIONS,
    "ws": config.MAX_WS,
    "audio": config.MAX_AUDIO,
    "media_in": config.MAX_MEDIA_IN,
}

RETRY_AFTER = 2  # seconds advertised to 503 clients


def caps():
    return dict(_CAPS)


def counts():
    with _lock:
        return dict(_counts)


def acquire(kind):
    """Take one slot of `kind`; False (and no change) when at capacity."""
    with _lock:
        if _counts[kind] >= _CAPS[kind]:
            return False
        _counts[kind] += 1
        return True


def release(kind):
    with _lock:
        _counts[kind] = max(0, _counts[kind] - 1)


def full(kind):
    return counts()[kind] >= _CAPS[kind]


def gate(handler, kind, what):
    """503 + Retry-After when `kind` is at capacity (caller must not have
    taken a slot yet). Returns True when the caller may proceed."""
    if acquire(kind):
        return True
    log.log(f"503: {kind} at capacity ({_CAPS[kind]})", what=what)
    handler._json(
        503,
        {"ok": False, "error": f"server busy: {what} at capacity ({_CAPS[kind]}); retry shortly"},
        extra={"Retry-After": str(RETRY_AFTER)},
    )
    return False


def snapshot():
    """Metrics for /api/stats: live counts per capability."""
    return {"caps": caps(), "active": counts()}
