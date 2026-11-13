"""Audio: PulseAudio helpers and ffmpeg-based audio status/source discovery.

Ownership: pactl wrapper, monitor-source discovery, the null-sink setup,
mic-in virtual source and camera device discovery. Stream endpoints live
in server.py (audio out) and media_in.py (device in); spawned ffmpeg
children are registered via procs.
"""

import glob
import os
import shutil

from . import config, logging as log, procs

AUDIO_LOG = "/tmp/remote_audio.log"

MIC_SINK = "remote_mic_sink"
MIC_SOURCE = "remote_mic"
MIC_RATE = 48000


def _pa(*args):
    rc, out, _ = procs._run(["pactl", *args], env=config._pa_env())
    return out.strip() if rc == 0 else ""


def audio_source():
    """PulseAudio monitor of the default sink = what apps are playing (not the mic)."""
    if not _pa("list", "short", "sinks"):
        _pa("load-module", "module-null-sink", "sink_name=remote_out", "sink_properties=device.description=RemoteOut")
        _pa("set-default-sink", "remote_out")
        for line in _pa("list", "short", "sink-inputs").splitlines():
            _pa("move-sink-input", line.split()[0], "remote_out")
    sink = _pa("get-default-sink") or "remote_out"
    return sink + ".monitor"


def audio_status():
    log_tail = ""
    try:
        with open(AUDIO_LOG) as f:
            log_tail = "".join(f.readlines()[-12:])
    except Exception:
        pass
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "pactl": bool(shutil.which("pactl")),
        "pulse_server": config._pa_env().get("PULSE_SERVER"),
        "info": _pa("info")[:400],
        "sinks": _pa("list", "short", "sinks"),
        "sources": _pa("list", "short", "sources"),
        "chosen_source": audio_source(),
        "ffmpeg_log": log_tail,
    }


def ensure_mic_source():
    """Create the virtual 'remote_mic' PulseAudio source: a null sink whose
    monitor is remapped into a capture source. ffmpeg then plays the client
    mic into the sink, so desktop apps capture from 'remote_mic'."""
    if not _pa("list", "short", "sinks"):
        return False
    if MIC_SINK not in _pa("list", "short", "sinks"):
        _pa(
            "load-module",
            "module-null-sink",
            f"sink_name={MIC_SINK}",
            f"rate={MIC_RATE}",
            "sink_properties=device.description=RemoteMicIn",
        )
    if MIC_SOURCE not in _pa("list", "short", "sources"):
        _pa(
            "load-module",
            "module-remap-source",
            f"source_name={MIC_SOURCE}",
            f"master={MIC_SINK}.monitor",
            "source_properties=device.description=RemoteMicIn",
        )
    return MIC_SOURCE in _pa("list", "short", "sources")


def camera_device():
    """Loopback device to feed; REMOTE_CAM_DEVICE overrides (matters when a
    real webcam also exists and takes /dev/video0)."""
    d = config.CAM_DEVICE
    if d and os.path.exists(d):
        return d
    devs = sorted(glob.glob("/dev/video*"))
    return devs[0] if devs else None
