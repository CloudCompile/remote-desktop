"""v2.5.0 audio & media tests (#94-#96, #98-#103, #105, #106, #124).

Subprocesses (pactl/ffmpeg/xdotool) are always mocked — CI has no
PulseAudio. The shared-capture registry is exercised with fake ffmpeg
processes whose stdout the test controls.
"""

import os
import threading
import time

import pytest

from remote_desktop import audio, config, state


# ------------------------------------------------------------- fakes


class _Out:
    """File-like stdout the producer reads (read1 semantics)."""

    def __init__(self, p):
        self.p = p

    def read1(self, n):
        return self.p._read1(n)


class FakeProc:
    """A stand-in for the ffmpeg child: controllable stdout/stdin."""

    def __init__(self, chunks=(), hold=None, die_after=None):
        self.stdout = _Out(self)
        self.stdin = open(os.devnull, "wb")  # record_start writes 'q' here
        self.pid = 12345
        self.returncode = None
        self.chunks = list(chunks)
        self.hold = hold
        self.die_after = die_after
        self.killed = False
        self._lock = threading.Lock()

    def _read1(self, n):
        with self._lock:
            if self.chunks:
                return self.chunks.pop(0)
            if self.returncode is not None:
                return b""
        if self.hold:
            self.hold.wait()
            with self._lock:
                if self.chunks:
                    return self.chunks.pop(0)
                return b""
        time.sleep(0.01)
        return b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return self.returncode


@pytest.fixture
def pa_env(monkeypatch):
    """pactl present but talking to nothing: every call succeeds empty."""
    monkeypatch.setattr("shutil.which", lambda n, path=None: n in ("pactl", "ffmpeg", "xdotool", "playerctl"))

    def fake_run(cmd, timeout=6, env=None):
        return 0, "", ""

    monkeypatch.setattr(audio.procs, "_run", fake_run)
    monkeypatch.setattr(audio, "sinks", lambda: [])
    yield


SINKS_OUT = """Sink #0
\tState: RUNNING
\tName: remote_out
\tDescription: RemoteOut
\tDriver: module-null-sink.c
\tSample Specification: s16le 2ch 44100Hz
\tChannel Map: front-left,front-right
\tOwner Module: 12
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB,  front-right: 65536 / 100% / 0.00 dB
        balance 0.00
\tBase Volume: 65536 / 100% / 0.00 dB
\tProperties:
\t\tdevice.description = "RemoteOut"

Sink #1
\tState: IDLE
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo
\tDescription: Built-in Audio Analog Stereo
\tDriver: module-alsa-card.c
\tMute: yes
\tVolume: front-left: 30361 / 46% / -20.00 dB,  front-right: 39321 / 60% / -13.00 dB
        balance 0.00
"""

INPUTS_OUT = """Sink Input #41
\tDriver: protocol-native.c
\tOwner Module: 10
\tSink: 0
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB,  front-right: 65536 / 100% / 0.00 dB
        balance 0.00
\t\tapplication.name = "Firefox"
\t\tapplication.process.binary = "firefox"
\t\tmedia.name = "Playback"

Sink Input #42
\tDriver: protocol-native.c
\tSink: 0
\tMute: yes
\tVolume: front-left: 32768 / 50% / -18.00 dB,  front-right: 32768 / 50% / -18.00 dB
        balance 0.00
\t\tapplication.process.binary = "vlc"
"""


# ------------------------------------------------------------------- #94


def test_parse_sinks_description_state_volume_mute():
    s = audio._parse_sinks(SINKS_OUT)
    assert len(s) == 2
    assert s[0]["name"] == "remote_out"
    assert s[0]["description"] == "RemoteOut"
    assert s[0]["state"] == "running"
    assert s[0]["volume"] == 100
    assert s[0]["mute"] is False
    assert s[1]["volume"] == 60  # max across channels
    assert s[1]["mute"] is True


def test_select_sink_validates_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "sinks", lambda: audio._parse_sinks(SINKS_OUT))
    ok, err = audio.select_sink("alsa_output.pci-0000_00_1f.3.analog-stereo")
    assert (ok, err) == (True, None)
    assert audio._selected_sink_name() == "alsa_output.pci-0000_00_1f.3.analog-stereo"
    # a removed sink falls back to the default with a notice (#94)
    monkeypatch.setattr(audio, "sinks", lambda: [audio._parse_sinks(SINKS_OUT)[0]])
    assert audio._current_sink() == "remote_out"
    assert "vanished" in audio.notice()
    assert state.load_audio_state()["sink"] == ""  # stale selection dropped
    audio.select_sink("nope")  # invalid name is refused
    ok, err = audio.select_sink("; rm -rf")
    assert ok is False


def test_set_sink_volume_clamped_and_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "sinks", lambda: audio._parse_sinks(SINKS_OUT))
    calls = []
    monkeypatch.setattr(audio, "_pa", lambda *a: calls.append(a) or "")
    ok, err = audio.set_sink_volume("remote_out", volume=500, mute=True)
    assert (ok, err) == (True, None)
    assert ("set-sink-volume", "remote_out", "150%") in calls
    assert ("set-sink-mute", "remote_out", "1") in calls
    saved = state.load_audio_state()
    assert saved["volume"] == 150 and saved["mute"] is True


def test_restore_state_reapplies_volume_and_mute(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    state.save_audio_state({"sink": "remote_out", "volume": 42, "mute": True})
    monkeypatch.setattr(audio, "sinks", lambda: audio._parse_sinks(SINKS_OUT))
    calls = []
    monkeypatch.setattr(audio, "_pa", lambda *a: calls.append(a) or "")
    audio._saved_applied = False
    audio._selected_sink = None
    try:
        audio.restore_state()
        assert audio._selected_sink_name() == "remote_out"
        assert ("set-sink-volume", "remote_out", "42%") in calls
        assert ("set-sink-mute", "remote_out", "1") in calls
    finally:
        audio._saved_applied = False
        audio._selected_sink = None


def test_audio_source_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(audio, "_selected_sink", None)
    monkeypatch.setattr(audio, "sinks", lambda: audio._parse_sinks(SINKS_OUT))
    monkeypatch.setattr(audio, "_pa", lambda *a: "remote_out" if a[0] == "get-default-sink" else "")
    assert audio.audio_source() == "remote_out.monitor"


# ------------------------------------------------------------------- #95


def test_sink_inputs_parsed_and_grouped(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, path=None: n == "pactl")

    def fake_pa(*a):
        return INPUTS_OUT if a[0] == "list" else ""

    monkeypatch.setattr(audio, "_pa", fake_pa)
    inputs = audio.sink_inputs()
    assert [i["index"] for i in inputs] == [41, 42]
    assert inputs[0]["app"] == "Firefox"
    assert inputs[1]["mute"] is True
    groups = audio.audio_apps()
    by_app = {g["app"]: g for g in groups}
    assert by_app["Firefox"]["count"] == 1
    assert by_app["vlc"]["volume"] == 50


def test_muting_one_app_does_not_affect_others(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda n, path=None: n == "pactl")
    calls = []
    monkeypatch.setattr(audio, "_pa", lambda *a: calls.append(a) or INPUTS_OUT)
    ok, _ = audio.set_app_volume("Firefox", mute=True, volume=30)
    assert ok is True
    # only stream 41 (Firefox) is touched; vlc's #42 never appears
    assert ("set-sink-input-volume", "41", "30%") in calls
    assert ("set-sink-input-mute", "41", "1") in calls
    assert not any("42" in c for c in calls if c[0].startswith("set-sink-input"))
    # the level is stored as the app default (#95)
    assert state.load_audio_state()["app_prefs"]["Firefox"] == {"volume": 30, "mute": True}


def test_new_streams_inherit_app_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda n, path=None: n == "pactl")
    state.save_audio_state({"app_prefs": {"Firefox": {"volume": 33, "mute": False}}})
    calls = []
    monkeypatch.setattr(audio, "_pa", lambda *a: calls.append(a) or INPUTS_OUT)
    n = audio.apply_app_defaults()
    assert n >= 1
    assert ("set-sink-input-volume", "41", "33%") in calls


def test_app_volume_hidden_when_unsupported(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, path=None: None)
    assert audio.app_supported() is False
    assert audio.sink_inputs() == []


# -------------------------------------------------------- #96 / #105


def test_capture_cmd_quality_presets():
    cmd, mime = audio.capture_cmd("remote_out.monitor", "opus", 48_000)
    assert 'audio/webm; codecs="opus"' == mime
    joined = " ".join(cmd)
    assert "-b:a 48000" in joined and "-compression_level 3" in joined
    cmd, _ = audio.capture_cmd("remote_out.monitor", "opus", 160_000)
    assert "-compression_level 9" in " ".join(cmd)
    cmd, mime = audio.capture_cmd("remote_out.monitor", "pcm", 0)
    assert mime == "application/octet-stream" and "-f s16le" in " ".join(cmd)


def test_capture_cmd_clamps_bitrate():
    cmd, _ = audio.capture_cmd("s", "opus", 999_999_999)
    assert f"-b:a {audio.BITRATE_MAX}" in " ".join(cmd)


def test_shared_capture_fans_out_and_drops_slow_listener():
    chunk = b"x" * 65_536
    fake = FakeProc(chunks=[chunk] * 100)
    cap = audio.Capture("opus:96000:s", ["ffmpeg"], shared=True)
    cap.proc = fake
    a, b = cap.attach(), cap.attach()
    threading.Thread(target=cap._produce, daemon=True).start()
    assert a.next() == chunk
    assert b.next() == chunk  # both listeners see the same stream
    # a listener that stops draining is dropped at the cap, not allowed to
    # stall the producer (#105)
    cap.dead = True  # producer loop ends after the queue drains
    got = 0
    while True:
        c = a.next()
        if c is None:
            break
        got += 1
    assert got > 0


def test_slow_listener_capped_without_blocking_producer():
    big = b"y" * 65_536
    fake = FakeProc(chunks=[big] * 200)
    cap = audio.Capture("opus:96000:s", ["ffmpeg"], shared=True)
    cap.proc = fake
    slow = cap.attach()
    fast = cap.attach()
    threading.Thread(target=cap._produce, daemon=True).start()
    assert fast.next() is not None
    # slow listener accumulates past the cap -> dropped with a sentinel
    deadline = time.time() + 10
    while not slow.dropped and time.time() < deadline:
        time.sleep(0.01)
    assert slow.dropped is True
    # a dropped listener finishes its backlog, then hits the sentinel
    seen = 0
    while True:
        c = slow.next()
        if c is None:
            break
        seen += 1
    assert seen > 0


def test_acquire_capture_registry_shares_and_cleans(monkeypatch):
    started = []

    def fake_spawn(cmd, **kw):
        started.append(cmd)
        return FakeProc(hold=threading.Event())  # producer stays alive

    monkeypatch.setattr(audio.procs, "spawn_stream", fake_spawn)
    monkeypatch.setattr(config, "AUDIO_SHARED", True)
    c1, _ = audio.acquire_capture("s", "opus", 96_000)
    c2, _ = audio.acquire_capture("s", "opus", 96_000)
    assert c1 is c2 and len(started) == 1  # one ffmpeg for N listeners (#105)
    c3, _ = audio.acquire_capture("s", "opus", 48_000)
    assert c3 is not c1  # quality tiers stay separate (#96)
    c1.stop()
    assert c2.dead is True
    c3.stop()


def test_per_client_capture_when_isolation_requested(monkeypatch):
    started = []

    def fake_spawn(cmd, **kw):
        started.append(cmd)
        return FakeProc(hold=threading.Event())

    monkeypatch.setattr(audio.procs, "spawn_stream", fake_spawn)
    c1, _ = audio.acquire_capture("s", "opus", 96_000, shared=False)
    c2, _ = audio.acquire_capture("s", "opus", 96_000, shared=False)
    assert c1 is not c2 and len(started) == 2
    c1.stop()
    c2.stop()


# ------------------------------------------------------------------- #102


def test_record_start_stop_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda n, path=None: n == "ffmpeg")
    monkeypatch.setattr(audio, "recordings_dir", lambda: str(tmp_path / "Recordings"))
    monkeypatch.setattr(audio, "audio_source", lambda: "remote_out.monitor")

    spawned = []

    class RecProc(FakeProc):
        def __init__(self):
            super().__init__()
            self.wrote = b""

        def write(self, b):
            self.wrote += b
            self.returncode = 0  # 'q' finalizes cleanly

    def fake_spawn(cmd, **kw):
        p = RecProc()
        spawned.append(cmd)
        p.part = cmd[-1]
        os.makedirs(os.path.dirname(cmd[-1]), exist_ok=True)
        with open(cmd[-1], "wb") as f:
            f.write(b"\x1a\x45\xdf\xa3" * 10)  # a webm-ish fragment
        return p

    monkeypatch.setattr(audio.procs, "spawn_stream", fake_spawn)
    ok, err = audio.record_start("opus", "meeting")
    assert (ok, err) == (True, None)
    assert "-f webm" in " ".join(spawned[-1]) and "-cluster_time_limit" in " ".join(spawned[-1])
    st = audio.record_status()
    assert st["active"] is True and st["name"].startswith("meeting") and st["name"].endswith(".webm")
    r = audio.record_stop()
    assert r["stopped"] is True
    final = tmp_path / "Recordings" / st["name"]
    assert final.exists()  # .part renamed into place (#102)
    assert audio.record_status()["active"] is False


def test_record_requires_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda n, path=None: None)
    ok, err = audio.record_start("opus")
    assert ok is False and "ffmpeg" in err


def test_record_cap_boundaries(monkeypatch):
    monkeypatch.setattr(config, "REC_MAX_MINUTES", 1)
    monkeypatch.setattr(config, "REC_MAX_MB", 1)
    assert audio._record_over_limit(0, 0) is False
    assert audio._record_over_limit(59, 0) is False
    assert audio._record_over_limit(60, 0) is True
    assert audio._record_over_limit(0, 1 << 20) is True


def test_record_watchdog_stops_at_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REC_MAX_MINUTES", 120)
    monkeypatch.setattr(config, "REC_MAX_MB", 1)
    part = tmp_path / ".x.webm.part"
    part.write_bytes(b"z" * (1024 * 1024 + 10))

    class P(FakeProc):
        def poll(self):
            return None

    audio._rec.update(
        {
            "proc": P(),
            "part": str(part),
            "path": str(tmp_path / "x.webm"),
            "name": "x",
            "started": time.time(),
            "fmt": "opus",
        }
    )
    stops = []
    monkeypatch.setattr(audio, "record_stop", lambda: stops.append(1) or {"ok": True})
    monkeypatch.setattr(audio.time, "sleep", lambda s: None)
    try:
        audio._record_watchdog()
    finally:
        audio._rec.update({"proc": None, "part": None, "path": None, "name": None, "started": 0.0, "fmt": None})
    assert stops == [1]


def test_record_recover_renames_fragments(monkeypatch, tmp_path):
    d = tmp_path / "Recordings"
    d.mkdir()
    part = d / ".audio-1.webm.part"
    part.write_bytes(b"frag")
    monkeypatch.setattr(audio, "recordings_dir", lambda: str(d))
    audio.record_recover()
    assert (d / "audio-1.webm").exists() and not part.exists()


def test_recordings_list_safe_without_files_root(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "recordings_dir", lambda: str(tmp_path / "gone"))
    assert audio.recordings_list() == []


# ------------------------------------------------------------------- #101


def test_media_keys_fallback_to_keysyms(monkeypatch):
    # playerctl present but no players -> keysym path
    monkeypatch.setattr("shutil.which", lambda n, path=None: n in ("xdotool", "playerctl"))
    calls = []
    monkeypatch.setattr(audio.procs, "_run", lambda cmd, timeout=6, env=None: calls.append(cmd) or (0, "", ""))
    r = audio.media_key("playpause")
    assert r["ok"] is True and r["via"] == "keysym"
    assert any("XF86AudioPlay" in c for c in calls)
    assert audio.media_key("bogus")["ok"] is False


def test_media_keys_use_mpris_when_players_exist(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, path=None: n in ("xdotool", "playerctl"))
    monkeypatch.setattr(audio, "media_players", lambda: ["spotify"])
    calls = []
    monkeypatch.setattr(audio.procs, "_run", lambda cmd, timeout=6, env=None: calls.append(cmd) or (0, "", ""))
    r = audio.media_key("next")
    assert r["ok"] is True and r["via"] == "mpris" and r["player"] == "spotify"
    assert any("next" in c for c in calls)
    # volume keys never go through MPRIS
    audio.media_key("volup")
    assert any("XF86AudioRaiseVolume" in c for c in calls)


# ------------------------------------------------------------------- #106


def test_diagnostics_shape(pa_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(audio, "recordings_dir", lambda: str(tmp_path))
    monkeypatch.setattr(audio, "_pa", lambda *a: "")
    monkeypatch.setattr(audio, "sinks", lambda: [])
    monkeypatch.setattr(audio, "audio_source", lambda: "x.monitor")
    d = audio.diagnostics()
    for key in (
        "ffmpeg",
        "pactl",
        "pulse_ok",
        "selected_sink",
        "effective_sink",
        "sinks",
        "chosen_source",
        "app_supported",
        "presets",
        "captures",
        "record",
        "last_error",
        "notice",
        "ffmpeg_log",
    ):
        assert key in d, key
    assert d["presets"] == audio.QUALITY_PRESETS
