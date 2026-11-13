"""Child processes and subprocess helpers.

Ownership: every spawned child the server may need to reap (ffmpeg
streams, detached GUI apps, store installs) is tracked here. Shutdown
(#25) calls reap_children(); nothing else may kill children. Also holds
the never-raise _run() helper and the X11/PulseAudio child environment
composition used by all spawned tools.
"""

import signal
import subprocess
import threading
import time

from . import config, log

# every ffmpeg/stream child we spawn, so shutdown can reap stragglers (#14/#25)
child_procs = set()
_child_procs_lock = threading.Lock()


def _run(cmd, timeout=6, env=None):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, env=env or config._env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def run_detached(cmd):
    """Launch a GUI app detached from the request handler."""
    try:
        subprocess.Popen(
            cmd, env=config._pa_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def die_with_parent():
    """preexec_fn: kill this child if the server process dies (no orphaned ffmpeg)."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGHUP)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def spawn_stream(cmd, **popen_kwargs):
    """Popen a streaming child registered for shutdown reaping."""
    proc = subprocess.Popen(cmd, **popen_kwargs)
    with _child_procs_lock:
        child_procs.add(proc)
    return proc


def reap_children(timeout=3):
    """Kill and wait for every registered streaming child. Returns the
    number reaped (#25)."""
    with _child_procs_lock:
        procs = list(child_procs)
        child_procs.clear()
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:
            pass
    if procs:
        log.log(f"reaped {len(procs)} child process(es)")
    return len(procs)


def active_children():
    with _child_procs_lock:
        return len(child_procs)


def elapsed_since(ts):
    return time.time() - ts
