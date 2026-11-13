"""Device-in streams: desktop browser mic and webcam into the desktop (#22).

Ownership: the /api/mic and /api/camera websocket endpoints, their busy
locks and status. Binary frames are piped into ffmpeg children (managed
by procs); concurrency is capped by limits ("media_in") on top of the
per-device busy lock (409).
"""

import glob
import os
import shutil
import subprocess
import threading
import time

from . import audio, config, limits, log, procs, ws

_mic_busy = threading.Lock()
_cam_busy = threading.Lock()


def _ws_to_stdin(handler, cmd, opened_log, closed_log):
    """Shared device-in loop: authenticated websocket whose binary frames
    are piped into a subprocess (ffmpeg). Blocks until the client leaves."""
    try:
        proc = procs.spawn_stream(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=open(audio.AUDIO_LOG, "ab"),
            preexec_fn=procs.die_with_parent,
            env=config._pa_env(),
        )
    except Exception as e:
        log.log(f"device-in: ffmpeg start failed: {e}")
        return
    started = time.time()
    log.log(opened_log)
    reader = ws._WsReader(handler.rfile)
    try:
        while True:
            fr = reader.read()
            if fr is None:
                break
            op, payload = fr
            if op == 0x8:
                break
            if op == 0x9:
                ws._ws_pong(handler, payload)
                continue
            if payload and op in (0x1, 0x2):
                proc.stdin.write(payload)  # blocks if ffmpeg falls behind = backpressure
    except Exception:
        pass
    finally:
        with procs._child_procs_lock:
            procs.child_procs.discard(proc)
        try:
            proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        log.log(f"{closed_log} after {time.time() - started:.0f}s")


def websocket_mic(handler):
    """Mic in: binary frames = raw s16le / 48 kHz / mono PCM from the
    client, played by ffmpeg into the null sink behind 'remote_mic'."""
    if not shutil.which("ffmpeg"):
        return handler._json(503, {"error": "ffmpeg not installed"})
    if not limits.gate(handler, "media_in", "device-in streams"):
        return
    if not _mic_busy.acquire(blocking=False):
        limits.release("media_in")
        return handler._json(409, {"error": "mic-in already in use"})
    try:
        if not audio.ensure_mic_source():
            return handler._json(503, {"error": "PulseAudio unavailable"})
        if not ws.ws_upgrade(handler):
            return
        cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "s16le",
            "-ar",
            str(audio.MIC_RATE),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "pulse",
            "-device",
            audio.MIC_SINK,
            "remote-mic-in",
        ]
        _ws_to_stdin(
            handler,
            cmd,
            f"mic-in open  {handler.client_address[0]} -> {audio.MIC_SINK}",
            f"mic-in close {handler.client_address[0]}",
        )
    finally:
        _mic_busy.release()
        limits.release("media_in")


def mic_status():
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "source": audio.MIC_SOURCE in audio._pa("list", "short", "sources"),
        "sample_rate": audio.MIC_RATE,
        "active": _mic_busy.locked(),
    }


def websocket_camera(handler):
    """Cam in: binary frames = WebM (MediaRecorder) video from the client,
    decoded by ffmpeg onto a v4l2loopback device so desktop apps see it as
    a webcam. Needs the v4l2loopback kernel module on the desktop."""
    if not shutil.which("ffmpeg"):
        return handler._json(503, {"error": "ffmpeg not installed"})
    dev = audio.camera_device()
    if not dev:
        return handler._json(503, {"error": "no /dev/video device (install v4l2loopback)"})
    if not limits.gate(handler, "media_in", "device-in streams"):
        return
    if not _cam_busy.acquire(blocking=False):
        limits.release("media_in")
        return handler._json(409, {"error": "cam-in already in use"})
    try:
        if not ws.ws_upgrade(handler):
            return
        cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "matroska",
            "-i",
            "pipe:0",
            "-f",
            "v4l2",
            "-pix_fmt",
            "yuv420p",
            dev,
        ]
        _ws_to_stdin(
            handler,
            cmd,
            f"cam-in open  {handler.client_address[0]} -> {dev}",
            f"cam-in close {handler.client_address[0]}",
        )
    finally:
        _cam_busy.release()
        limits.release("media_in")


def camera_status():
    _, lsmod, _ = procs._run(["lsmod"])
    devs = []
    for dev in sorted(glob.glob("/dev/video*")):
        try:
            with open(f"/sys/class/video4linux/{os.path.basename(dev)}/name") as f:
                name = f.read().strip()
        except Exception:
            name = ""
        devs.append({"dev": dev, "name": name})
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "loopback": "v4l2loopback" in lsmod,
        "devices": devs,
        "device": audio.camera_device(),
        "active": _cam_busy.locked(),
    }
