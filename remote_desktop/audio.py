"""Audio: PulseAudio helpers, the shared capture pipeline and media extras.

Ownership: pactl wrapper, monitor-source discovery, the null-sink setup,
mic-in virtual source and camera device discovery, sink selection with
volume/mute (#94), per-application volume (#95), the shared ffmpeg capture
registry that fans encoded chunks out to every listener (#105), quality
presets / opus tuning (#96), desktop-audio recording (#102), media keys
(#101) and the diagnostics snapshot (#106). Stream endpoints live in
server.py (audio out) and media_in.py (device in); spawned ffmpeg children
are registered via procs so shutdown reaps them (#25).
"""

import collections
import glob
import os
import re
import shutil
import subprocess
import threading
import time

from . import config, files, logging as log, procs, state

AUDIO_LOG = "/tmp/remote_audio.log"

MIC_SINK = "remote_mic_sink"
MIC_SOURCE = "remote_mic"
MIC_RATE = 48000

# ---- quality presets (#96): opus bitrate in bits/s + encoder complexity
QUALITY_PRESETS = {"low": 48_000, "medium": 96_000, "high": 160_000}
OPUS_COMPLEXITY = {"low": 3, "medium": 6, "high": 9}
BITRATE_MIN, BITRATE_MAX, BITRATE_DEFAULT = 16_000, 320_000, QUALITY_PRESETS["medium"]

# ---- shared capture (#105): per-listener queue cap before a listener is
# dropped so one slow client can never stall the producer
SLOW_QUEUE_BYTES = 4 * 1024 * 1024
CAPTURE_GRACE = 15.0  # s a listener-less shared capture lingers before respawn

VOLUME_MIN, VOLUME_MAX = 0, 150  # percent, pactl accepts >100 for boosts


def _pa(*args):
    rc, out, _ = procs._run(["pactl", *args], env=config._pa_env())
    return out.strip() if rc == 0 else ""


# ---------------------------------------------------------------- state

_state_lock = threading.Lock()
_last_error = ""  # human-readable, never contains secrets (#106)
_notice = ""  # e.g. "sink vanished; fell back to default" (#94)
_saved_applied = False
_selected_sink = None  # capture sink chosen from the UI (#94); None = default


def last_error():
    with _state_lock:
        return _last_error


def set_last_error(msg):
    global _last_error
    with _state_lock:
        _last_error = str(msg)[:300]


def set_notice(msg):
    global _notice
    with _state_lock:
        _notice = str(msg)[:300]


def notice():
    with _state_lock:
        return _notice


def _persist(sink=None, volume=None, mute=None, app_prefs=None, drop_sink=False):
    """Merge one field into the persisted audio state (best effort)."""
    cur = state.load_audio_state()
    if drop_sink:
        cur["sink"] = ""
    elif sink is not None:
        cur["sink"] = str(sink)[:120]
    if volume is not None:
        cur["volume"] = max(VOLUME_MIN, min(VOLUME_MAX, int(volume)))
    if mute is not None:
        cur["mute"] = bool(mute)
    if app_prefs is not None:
        cur["app_prefs"] = app_prefs
    state.save_audio_state(cur)


def restore_state():
    """Re-apply the persisted sink selection / volume / mute at startup
    (#94 acceptance: volume and mute survive a reconnect)."""
    global _saved_applied
    with _state_lock:
        if _saved_applied:
            return
        _saved_applied = True
    saved = state.load_audio_state()
    names = [s["name"] for s in sinks()]
    if saved.get("sink") and saved["sink"] in names:
        global _selected_sink
        with _state_lock:
            _selected_sink = saved["sink"]
    sink = _current_sink()
    if sink and saved.get("volume") is not None:
        _pa("set-sink-volume", sink, f"{int(saved['volume'])}%")
    if sink and saved.get("mute") is not None:
        _pa("set-sink-mute", sink, "1" if saved["mute"] else "0")


# ---------------------------------------------------------------- sinks (#94)


def _parse_sinks(out):
    """Parse `pactl list sinks` into [{name, description, state, volume, mute}]."""
    out_sinks = []
    for block in re.split(r"\n(?=Sink #)", out):
        m = re.search(r"^\s+Name: (.+)$", block, re.M)
        if not m:
            continue
        vol = re.search(r"^\s+Volume: (.+)$", block, re.M)
        pcts = [int(p) for p in re.findall(r"(\d+)%", vol.group(1))] if vol else []
        desc = re.search(r"^\s+Description: (.+)$", block, re.M)
        st = re.search(r"^\s+State: (\S+)", block, re.M)
        mute = re.search(r"^\s+Mute: (yes|no)", block, re.M)
        out_sinks.append(
            {
                "name": m.group(1).strip(),
                "description": desc.group(1).strip() if desc else "",
                "state": st.group(1).lower() if st else "unknown",
                "volume": max(pcts) if pcts else None,
                "mute": bool(mute and mute.group(1) == "yes"),
            }
        )
    return out_sinks


def sinks():
    """Every PulseAudio sink with description, state and volume (#94)."""
    out = _pa("list", "sinks")
    return _parse_sinks(out) if out else []


def _selected_sink_name():
    with _state_lock:
        return _selected_sink


def _current_sink():
    """The sink the capture pipeline uses: the user's selection (#94) or the
    daemon default; falls back safely when the selection vanished."""
    names = {s["name"] for s in sinks()}
    sel = _selected_sink_name()
    if sel and sel not in names:
        set_notice(f"sink '{sel}' vanished; fell back to the default sink")
        log.log(f"audio: selected sink {sel!r} is gone, using default")
        _persist(drop_sink=True)
        with _state_lock:
            global _selected_sink
            _selected_sink = None
        sel = None
    if sel:
        return sel
    return _pa("get-default-sink") or "remote_out"


def select_sink(name):
    """Point the capture pipeline at `name` (#94). Returns (ok, error)."""
    global _selected_sink
    if not name or len(name) > 120 or not re.fullmatch(r"[\w.\-:]+", name):
        return False, "invalid sink name"
    if name not in {s["name"] for s in sinks()}:
        return False, "no such sink"
    with _state_lock:
        _selected_sink = name
    _persist(sink=name)
    set_notice("")
    log.log(f"audio: capture sink -> {name}")
    return True, None


def set_sink_volume(sink, volume=None, mute=None):
    """Volume (percent, clamped) and mute for one sink (#94)."""
    if not sink:
        sink = _current_sink()
    if sink not in {s["name"] for s in sinks()}:
        return False, "no such sink"
    if volume is not None:
        v = max(VOLUME_MIN, min(VOLUME_MAX, int(volume)))
        _pa("set-sink-volume", sink, f"{v}%")
        _persist(volume=v)
    if mute is not None:
        _pa("set-sink-mute", sink, "1" if mute else "0")
        _persist(mute=bool(mute))
    return True, None


def audio_source():
    """PulseAudio monitor of the selected sink = what apps are playing (#94).
    Not the mic. Falls back to the default sink and re-creates the shared
    'remote_out' null sink when PulseAudio has no sinks at all."""
    if not _pa("list", "short", "sinks"):
        _pa("load-module", "module-null-sink", "sink_name=remote_out", "sink_properties=device.description=RemoteOut")
        _pa("set-default-sink", "remote_out")
        for line in _pa("list", "short", "sink-inputs").splitlines():
            _pa("move-sink-input", line.split()[0], "remote_out")
    sink = _current_sink()
    return sink + ".monitor"


# ------------------------------------------------- per-application volume (#95)


def _parse_sink_inputs(out):
    """Parse `pactl list sink-inputs` into client-safe stream records (#95)."""
    inputs = []
    for block in re.split(r"\n(?=Sink Input #)", out):
        idx = re.search(r"^Sink Input #(\d+)", block)
        if not idx:
            continue
        vol = re.search(r"^\s+Volume: (.+)$", block, re.M)
        pcts = [int(p) for p in re.findall(r"(\d+)%", vol.group(1))] if vol else []
        mute = re.search(r"^\s+Mute: (yes|no)", block, re.M)
        app = re.search(r'application\.name = "(.*)"', block)
        binary = re.search(r'application\.process\.binary = "(.*)"', block)
        media = re.search(r'media\.name = "(.*)"', block)
        inputs.append(
            {
                "index": int(idx.group(1)),
                "app": (app.group(1) if app else (binary.group(1) if binary else ""))[:60],
                "binary": binary.group(1)[:60] if binary else "",
                "media": media.group(1)[:80] if media else "",
                "volume": max(pcts) if pcts else None,
                "mute": bool(mute and mute.group(1) == "yes"),
            }
        )
    return inputs


def sink_inputs():
    """Live playback streams (#95). Empty list also means 'unsupported'
    when pactl itself is unavailable — the UI hides the feature then."""
    if not shutil.which("pactl"):
        return []
    out = _pa("list", "sink-inputs")
    if not out:
        return []
    return _parse_sink_inputs(out)


def app_supported():
    """Per-app volume needs `pactl`; anything else degrades gracefully (#95)."""
    return bool(shutil.which("pactl")) and bool(_pa("info"))


def _group_by_app(inputs):
    """Group streams by application name, duplicates listed per index (#95)."""
    groups = {}
    for it in inputs:
        key = it["app"] or it["binary"] or f"stream-{it['index']}"
        g = groups.setdefault(key, {"app": key, "streams": [], "volume": None, "mute": False})
        g["streams"].append(it["index"])
        if it["volume"] is not None:
            g["volume"] = it["volume"] if g["volume"] is None else max(g["volume"], it["volume"])
        g["mute"] = g["mute"] or it["mute"]
    return list(groups.values())


def audio_apps():
    """Playback applications with their live levels and stored prefs (#95)."""
    inputs = sink_inputs()
    prefs = state.load_audio_state().get("app_prefs", {})
    groups = _group_by_app(inputs)
    for g in groups:
        g["pref_volume"] = (prefs.get(g["app"], {}) or {}).get("volume")
        g["pref_mute"] = (prefs.get(g["app"], {}) or {}).get("mute")
        g["count"] = len(g["streams"])
    return groups


def apply_app_defaults(inputs=None):
    """New streams inherit the stored per-app default (#95). Idempotent:
    only inputs whose live level differs from the pref are touched."""
    prefs = state.load_audio_state().get("app_prefs", {})
    if not prefs:
        return 0
    n = 0
    for it in inputs if inputs is not None else sink_inputs():
        key = it["app"] or it["binary"]
        pref = prefs.get(key)
        if not pref:
            continue
        vol, mute = pref.get("volume"), pref.get("mute")
        if vol is not None and it["volume"] != vol:
            _pa("set-sink-input-volume", str(it["index"]), f"{vol}%")
            n += 1
        if mute is not None and it["mute"] != mute:
            _pa("set-sink-input-mute", str(it["index"]), "1" if mute else "0")
            n += 1
    return n


def set_app_volume(app, volume=None, mute=None):
    """Volume/mute for every live stream of one app; stores the level as
    the app's default so new streams inherit it (#95)."""
    app = str(app)[:120]
    if not app:
        return False, "missing app"
    inputs = sink_inputs()
    targets = [it for it in inputs if (it["app"] or it["binary"]) == app]
    if not targets:
        return False, "no playing streams for this app"
    prefs = state.load_audio_state().get("app_prefs", {})
    pref = dict(prefs.get(app) or {})
    for it in targets:
        if volume is not None:
            v = max(VOLUME_MIN, min(VOLUME_MAX, int(volume)))
            _pa("set-sink-input-volume", str(it["index"]), f"{v}%")
            pref["volume"] = v
        if mute is not None:
            _pa("set-sink-input-mute", str(it["index"]), "1" if mute else "0")
            pref["mute"] = bool(mute)
    if pref:
        prefs[app] = pref
        prefs = {k: prefs[k] for k in list(prefs)[:50]}  # bounded
        _persist(app_prefs=prefs)
    return True, None


# --------------------------------------------------- shared captures (#105)


class _Listener:
    """One consumer of a capture: a bounded queue the request thread drains."""

    __slots__ = ("cap", "q", "ev", "bytes", "dropped")

    def __init__(self, cap):
        self.cap = cap
        self.q = collections.deque()
        self.ev = threading.Event()
        self.bytes = 0
        self.dropped = False

    def next(self):
        """The next encoded chunk, or None when the capture ended. Blocks
        while the desktop is silent — pulse monitors emit nothing then."""
        while True:
            if self.q:
                item = self.q.popleft()
                if item is None:
                    return None
                return item
            if self.cap.dead:
                return None
            self.ev.clear()
            if self.q or self.cap.dead:
                continue
            self.ev.wait()


class Capture:
    """One ffmpeg encoder fanned out to N listeners (#105)."""

    def __init__(self, key, cmd, shared=True):
        self.key = key
        self.cmd = cmd
        self.shared = shared
        self.proc = None
        self.dead = False
        self.started = time.time()
        self.bytes_out = 0
        self.listeners = []
        self.lock = threading.Lock()
        self._stop_timer = None

    def start(self):
        try:
            self.proc = procs.spawn_stream(
                self.cmd,
                env=config._pa_env(),
                stdout=subprocess.PIPE,
                stderr=open(AUDIO_LOG, "ab"),
                stdin=subprocess.DEVNULL,
                preexec_fn=procs.die_with_parent,
            )
        except Exception as e:
            set_last_error(f"ffmpeg start failed: {e}")
            log.log(f"audio: ffmpeg start failed: {e}")
            return None
        threading.Thread(target=self._produce, daemon=True, name=f"audio-{self.key}").start()
        log.log(f"audio: capture {self.key} started (pid {self.proc.pid})")
        return self

    def attach(self):
        lsn = _Listener(self)
        with self.lock:
            self.listeners.append(lsn)
        return lsn

    def detach(self, lsn):
        with self.lock:
            if lsn in self.listeners:
                self.listeners.remove(lsn)
            left = len(self.listeners)
        if self.shared and left == 0:
            # linger briefly so quality switches (#96) don't respawn ffmpeg
            t = threading.Timer(CAPTURE_GRACE, _maybe_stop_capture, (self.key,))
            t.daemon = True
            t.start()

    def _produce(self):
        """Read ffmpeg stdout once and fan chunks out to every listener.
        Never blocks on a slow listener: its queue is capped and it is
        dropped when it falls too far behind (#105)."""
        assert self.proc is not None
        try:
            while True:
                chunk = self.proc.stdout.read1(65_536)
                if not chunk:
                    break
                with self.lock:
                    self.bytes_out += len(chunk)
                    for lsn in self.listeners:
                        if lsn.dropped:
                            continue
                        lsn.q.append(chunk)
                        lsn.bytes += len(chunk)
                        if lsn.bytes > SLOW_QUEUE_BYTES:
                            lsn.dropped = True
                            lsn.q.append(None)  # sentinel ends the client loop
                            log.log("audio: dropped a slow listener")
                    for lsn in self.listeners:
                        lsn.ev.set()
        except Exception as e:
            set_last_error(f"capture {self.key} died: {e}")
        finally:
            self.dead = True
            with self.lock:
                listeners = list(self.listeners)
            for lsn in listeners:
                lsn.q.append(None)
                lsn.ev.set()
            log.log(f"audio: capture {self.key} ended after {time.time() - self.started:.0f}s")

    def stop(self):
        """Kill the encoder and wake every listener with EOF."""
        self.dead = True
        with _cap_lock:
            if _captures.get(self.key) is self:
                del _captures[self.key]
        if self.proc is not None:
            with procs._child_procs_lock:
                procs.child_procs.discard(self.proc)
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
        with self.lock:
            listeners = list(self.listeners)
        for lsn in listeners:
            lsn.q.append(None)
            lsn.ev.set()
        log.log(f"audio: capture {self.key} stopped")


_captures = {}
_cap_lock = threading.Lock()


def _maybe_stop_capture(key):
    """Grace-period callback: stop a shared capture with no listeners."""
    with _cap_lock:
        cap = _captures.get(key)
    if cap is not None and not cap.dead:
        with cap.lock:
            if not cap.listeners:
                cap.stop()


def capture_cmd(source, codec, bitrate):
    """The ffmpeg command for one capture tier (#96/#105)."""
    if codec == "opus":
        b = max(BITRATE_MIN, min(BITRATE_MAX, int(bitrate or BITRATE_DEFAULT)))
        cx = (
            OPUS_COMPLEXITY["low"]
            if b <= QUALITY_PRESETS["low"]
            else (OPUS_COMPLEXITY["medium"] if b <= QUALITY_PRESETS["medium"] else OPUS_COMPLEXITY["high"])
        )
        args = [
            "-c:a",
            "libopus",
            "-b:a",
            f"{b}",
            "-vbr",
            "on",
            "-frame_duration",
            "20",
            "-compression_level",
            str(cx),  # server-side tuning (#96)
            "-f",
            "webm",
        ]
        mime = 'audio/webm; codecs="opus"'
    else:
        args = ["-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-f", "s16le"]
        mime = "application/octet-stream"
    return [
        "ffmpeg",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "pulse",
        "-i",
        source,
        *args,
        "-",
    ], mime


def acquire_capture(source, codec, bitrate, shared=None):
    """A capture for (source, codec, bitrate); one process is shared by
    every listener with the same tier unless isolation is requested (#105).
    Returns (capture, mime) or (None, None) when ffmpeg cannot start."""
    if shared is None:
        shared = config.AUDIO_SHARED
    cmd, mime = capture_cmd(source, codec, bitrate)
    key = f"{codec}:{bitrate if codec == 'opus' else 0}:{source}"
    if shared:
        with _cap_lock:
            cap = _captures.get(key)
            if cap is not None and not cap.dead:
                return cap, mime
            cap = Capture(key, cmd, shared=True)
            _captures[key] = cap
    else:
        cap = Capture(key, cmd, shared=False)
    if cap.start() is None:
        if cap.shared:
            with _cap_lock:
                if _captures.get(key) is cap:
                    del _captures[key]
        return None, None
    return cap, mime


def captures_snapshot():
    """Registry health for the diagnostics view (#106)."""
    with _cap_lock:
        caps = list(_captures.values())
    return [
        {
            "key": c.key,
            "listeners": len(c.listeners),
            "alive": not c.dead,
            "seconds": round(time.time() - c.started),
            "mib": round(c.bytes_out / 1024 / 1024, 1),
        }
        for c in caps
    ]


# ------------------------------------------------------ recording (#102)


def recordings_dir():
    """Recordings live inside the first shared files root (#102) so the
    existing download path serves them; falls back to RD_FILES_DIR."""
    root = files.default_root()
    base = root.path if root else config.FILES_DIR
    return os.path.join(base, "Recordings")


_rec_lock = threading.Lock()
_rec = {"proc": None, "part": None, "path": None, "name": None, "started": 0.0, "fmt": None}


def record_status():
    with _rec_lock:
        active = _rec["proc"] is not None and _rec["proc"].poll() is None
        size = 0
        if _rec["part"]:
            try:
                size = os.path.getsize(_rec["part"])
            except OSError:
                pass
        return {
            "active": active,
            "name": _rec["name"] if active else None,
            "format": _rec["fmt"] if active else None,
            "seconds": round(time.time() - _rec["started"]) if active else 0,
            "bytes": size,
            "max_minutes": config.REC_MAX_MINUTES,
            "max_mb": config.REC_MAX_MB,
        }


def _record_finalize(clean):
    """Rename the fragment file into place; a crash loses at most the last
    WebM cluster, never the whole file (#102)."""
    part, final = _rec["part"], _rec["path"]
    _rec.update({"proc": None, "part": None, "path": None, "name": None, "started": 0.0, "fmt": None})
    if not part or not final:
        return
    try:
        if os.path.exists(part):
            os.replace(part, final)
            log.log(f"audio: recording {'saved' if clean else 'recovered'} -> {final}")
    except OSError as e:
        set_last_error(f"recording finalize failed: {e}")


def _record_over_limit(seconds, size):
    """Time/size caps for one recording (#102)."""
    return seconds >= config.REC_MAX_MINUTES * 60 or size >= config.REC_MAX_MB * 1024 * 1024


def _record_watchdog():
    """Enforce the time and size caps (#102)."""
    while True:
        time.sleep(5)
        st = record_status()
        if not st["active"]:
            return
        if _record_over_limit(st["seconds"], st["bytes"]):
            log.log(f"audio: recording cap reached ({st['seconds']}s, {st['bytes']} B)")
            record_stop()
            return


def record_start(fmt="opus", name=None):
    """Start capturing the desktop mix into a downloadable file (#102)."""
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not installed"
    with _rec_lock:
        if _rec["proc"] is not None and _rec["proc"].poll() is None:
            return False, "already recording"
        fmt = "flac" if fmt == "flac" else "opus"
        d = recordings_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return False, f"cannot create {d!r}: {e}"
        base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(name) or ""))[:80]
        final = os.path.join(d, f"{base or time.strftime('audio-%Y%m%d-%H%M%S')}.{'flac' if fmt == 'flac' else 'webm'}")
        i = 1
        while os.path.exists(final):
            stem, ext = os.path.splitext(final)
            final = f"{stem}-{i}{ext}"
            i += 1
        part = os.path.join(d, "." + os.path.basename(final) + ".part")
        src = audio_source()
        if fmt == "flac":
            codec_args = ["-c:a", "flac", "-f", "flac"]
        else:
            codec_args = [
                "-c:a",
                "libopus",
                "-b:a",
                f"{BITRATE_DEFAULT}",
                "-vbr",
                "on",
                # small self-contained clusters: a crash loses seconds, not
                # the whole recording (#102 acceptance)
                "-cluster_time_limit",
                "5000",
                "-cluster_size_limit",
                "1048576",
                "-f",
                "webm",
            ]
        cmd = [
            "ffmpeg",
            "-loglevel",
            "warning",
            "-f",
            "pulse",
            "-i",
            src,
            *codec_args,
            part,
        ]
        try:
            proc = procs.spawn_stream(
                cmd, env=config._pa_env(), stdin=subprocess.PIPE, preexec_fn=procs.die_with_parent
            )
        except Exception as e:
            set_last_error(f"recording start failed: {e}")
            return False, f"ffmpeg start failed: {e}"
        _rec.update(
            {
                "proc": proc,
                "part": part,
                "path": final,
                "name": os.path.basename(final),
                "started": time.time(),
                "fmt": fmt,
            }
        )
    threading.Thread(target=_record_watchdog, daemon=True, name="audio-rec").start()
    log.log(f"audio: recording -> {final}")
    return True, None


def record_stop():
    """Finalize the recording cleanly with 'q' so the file plays (#102)."""
    with _rec_lock:
        proc = _rec["proc"]
        if proc is None or proc.poll() is not None:
            _record_finalize(False)
            return {"ok": True, "stopped": False}
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
            clean = proc.returncode == 0
        except Exception:
            clean = False
            try:
                proc.kill()
            except Exception:
                pass
        with procs._child_procs_lock:
            procs.child_procs.discard(proc)
        _record_finalize(clean)
        return {"ok": True, "stopped": True}


def record_recover():
    """Surface recordings left as fragments by a crash (#102): the .part
    file holds every completed WebM cluster, so renaming makes it playable."""
    d = recordings_dir()
    try:
        parts = [f for f in os.listdir(d) if f.endswith(".part")]
    except OSError:
        return
    for f in parts:
        stem = f[: -len(".part")]
        if stem.startswith("."):
            stem = stem[1:]
        final = os.path.join(d, stem)
        if os.path.exists(final):
            final = os.path.join(d, "recovered-" + stem)
        try:
            os.replace(os.path.join(d, f), final)
            log.log(f"audio: recovered recording fragment -> {final}")
        except OSError:
            pass


def recordings_list():
    """Stored recordings with a ready download path (#102)."""
    d = recordings_dir()
    rid = ""
    try:
        root = files.default_root()
        if root:
            rid = root.rid
    except Exception:
        pass
    out = []
    try:
        for name in sorted(os.listdir(d), reverse=True):
            if name.startswith("."):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p) or not name.endswith((".webm", ".flac")):
                continue
            out.append(
                {
                    "name": name,
                    "size": os.path.getsize(p),
                    "mtime": int(os.path.getmtime(p)),
                    "download": f"/api/files/content?root={rid}&path=Recordings/{name}",
                }
            )
    except OSError:
        pass
    return out[:100]


# --------------------------------------------------------- media keys (#101)

MEDIA_KEYSYMS = {
    "play": "XF86AudioPlay",
    "pause": "XF86AudioPause",
    "playpause": "XF86AudioPlay",
    "next": "XF86AudioNext",
    "prev": "XF86AudioPrev",
    "stop": "XF86AudioStop",
    "mute": "XF86AudioMute",
    "volup": "XF86AudioRaiseVolume",
    "voldown": "XF86AudioLowerVolume",
}
_PLAYERCTL_CMD = {
    "play": "play",
    "pause": "pause",
    "playpause": "play-pause",
    "next": "next",
    "prev": "previous",
    "stop": "stop",
}


def media_players():
    """MPRIS players, when playerctl is available (#101)."""
    if not shutil.which("playerctl"):
        return []
    rc, out, _ = procs._run(["playerctl", "--list-all"], env=config._pa_env())
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:10]


def media_key(key):
    """One media key: MPRIS via playerctl when it works, XF86 keysyms via
    xdotool otherwise (#101 acceptance: the panel works without MPRIS)."""
    keysym = MEDIA_KEYSYMS.get(str(key))
    if not keysym:
        return {"ok": False, "error": "unknown key"}
    via = "keysym"
    players = media_players()
    cmd = _PLAYERCTL_CMD.get(str(key))
    if cmd and players:
        rc, _, _ = procs._run(["playerctl", cmd], env=config._pa_env())
        if rc == 0:
            via = "mpris"
    if via != "mpris":
        rc, _, err = procs._run(["xdotool", "key", "--clearmodifiers", keysym], timeout=10)
        if rc != 0:
            set_last_error(f"media key failed: {err[:150]}")
            return {"ok": False, "error": "xdotool failed (no X display?)"}
    return {"ok": True, "via": via, "player": players[0] if players else ""}


# ------------------------------------------------------ diagnostics (#106)


def diagnostics():
    """Everything the audio diagnostics view needs, client-safe (#106)."""
    st = audio_status()
    rec = record_status()
    sel = _selected_sink_name()
    return {
        "ok": True,
        "ffmpeg": st["ffmpeg"],
        "pactl": st["pactl"],
        "pulse_server": st["pulse_server"],
        "pulse_ok": bool(_pa("info")),
        "selected_sink": sel,
        "effective_sink": _current_sink() if st["pactl"] else "",
        "fallback": bool(sel and st["pactl"] and sel not in {s["name"] for s in sinks()}),
        "sinks": st["sinks"].splitlines()[:20],
        "sources": st["sources"].splitlines()[:20],
        "chosen_source": st["chosen_source"],
        "volume": next((s["volume"] for s in sinks() if s["name"] == (sel or "")), None),
        "mute": next((s["mute"] for s in sinks() if s["name"] == (sel or "")), None),
        "app_supported": app_supported(),
        "presets": QUALITY_PRESETS,
        "bitrate_default": BITRATE_DEFAULT,
        "captures": captures_snapshot(),
        "listeners": sum(c["listeners"] for c in captures_snapshot()),
        "record": rec,
        "last_error": last_error(),
        "notice": _notice,
        "ffmpeg_log": st["ffmpeg_log"],
    }


def audio_status():
    """Status snapshot for /api/audio/status (extended in v2.5)."""
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
