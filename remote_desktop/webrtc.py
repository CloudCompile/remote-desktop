"""WebRTC transport (v3.0.0, #137): the desktop framebuffer + audio as
real media tracks, decoded by the browser's hardware video pipeline.

Design (ADR-0007, superseding the no-go in ADR-0006 for v3.0.0):
- **Hybrid transport.** WebRTC carries the heavy outbound media
  (framebuffer video + desktop audio). The noVNC websocket stays
  connected and keeps carrying INPUT (pointer, keys, clipboard) — it is
  tiny and already battle-tested. The noVNC canvas is hidden while the
  WebRTC <video> is active.
- **Opt-in extra.** aiortc is not a runtime dependency: the module
  imports lazily and `available()` reports what is missing. Enable with
  RD_WEBRTC=1 after `pip install aiortc`.
- **One capture per peer** (MediaPlayer). The shared-capture fan-out
  (#105 pattern) applies here too and is future work; the peer cap
  bounds the cost.
- **Signalling** is one authenticated POST on the existing surface; the
  CSRF gate and audit apply unchanged.

Media paths (identical content to the HTTP pipelines):
- video: libav x11grab of DISPLAY -> H.264/VP8 (aiortc negotiates what
  its bundled libav can encode)
- audio: the PulseAudio monitor of the selected sink -> Opus
"""

import asyncio
import fractions
import os
import subprocess
import threading

from . import audit, config

_log_reason = "feature disabled (RD_WEBRTC=0)"
_mod = None  # the aiortc module bundle, imported lazily


def _import_aiortc():
    """Import aiortc once; remember why it failed if it did."""
    global _mod
    if _mod is not None:
        return _mod
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
        from aiortc import MediaStreamTrack
        from aiortc.contrib.media import MediaPlayer
        import av

        _mod = {
            "RTCPeerConnection": RTCPeerConnection,
            "RTCSessionDescription": RTCSessionDescription,
            "RTCConfiguration": RTCConfiguration,
            "RTCIceServer": RTCIceServer,
            "MediaStreamTrack": MediaStreamTrack,
            "MediaPlayer": MediaPlayer,
            "av": av,
        }
    except Exception as e:
        global _log_reason
        _log_reason = f"aiortc not importable: {e}"
        _mod = False
    return _mod


def available():
    """(ok, reason) — whether the WebRTC transport can start."""
    if not config.WEBRTC:
        return False, _log_reason
    m = _import_aiortc()
    if m is False:
        return False, _log_reason
    return True, "ok"


# ------------------------------------------------------------- peer loop

_loop = None  # the media event loop (aiortc wants one thread)
_loop_started = threading.Event()
_peers = set()  # live RTCPeerConnections
_MAX_PEERS = 4
_last_error = ""


def _media_loop():
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    _loop_started.set()
    loop.run_forever()


def _ensure_loop():
    if _loop_started.is_set():
        return
    threading.Thread(target=_media_loop, daemon=True, name="webrtc-media").start()
    _loop_started.wait(5)


# ------------------------------------------------------------- tracks


def _video_size():
    """Current display geometry, for the capture size (falls back to the
    configured RD_GEOMETRY)."""
    try:
        import subprocess

        out = subprocess.run(
            ["xdotool", "getdisplaygeometry"],
            env=config.ENV,
            capture_output=True,
            text=True,
            timeout=4,
        )
        w, h = out.stdout.strip().split()
        return f"{w}x{h}"
    except Exception:
        return config.GEOMETRY or "1280x800"


def _make_tracks(m):
    """Real capture: libav x11grab + the PulseAudio monitor (one capture
    per peer; fan-out is future work)."""
    from . import audio

    disp = config.ENV.get("DISPLAY", ":1")
    video = m["MediaPlayer"](
        disp,
        format="x11grab",
        options={"video_size": _video_size(), "framerate": "24"},
    ).video
    # PyAV builds on some platforms omit the PulseAudio input device even
    # when system ffmpeg supports it. Use the system binary for PCM capture
    # and wrap fixed 20ms frames as an aiortc AudioStreamTrack.
    audio_tr = _PulsePCMTrack(m, audio.audio_source())
    return video, audio_tr


class _PulsePCMTrack:
    """Lazy factory replaced below with a real AudioStreamTrack subclass.

    Kept as a factory so aiortc remains an optional, lazy dependency.
    """

    def __new__(cls, m, source):
        base = m["MediaStreamTrack"]

        class PulsePCMTrack(base):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self._proc = None
                self._pts = 0
                self._lock = threading.Lock()

            def _start(self):
                if self._proc is not None:
                    return
                cmd = [
                    config.FFMPEG, "-hide_banner", "-loglevel", "error",
                    "-f", "pulse", "-i", source,
                    "-ac", "2", "-ar", "48000", "-f", "s16le", "pipe:1",
                ]
                self._proc = subprocess.Popen(
                    cmd, env=config._env(), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0,
                )

            async def recv(self):
                import asyncio as _asyncio

                self._start()
                # 48kHz * 20ms * 2 channels * 2 bytes = 3840 bytes.
                data = await _asyncio.to_thread(self._proc.stdout.read, 3840)
                if len(data) != 3840:
                    raise RuntimeError("PulseAudio PCM capture ended")
                frame = m["av"].AudioFrame(format="s16", layout="stereo", samples=960)
                frame.planes[0].update(data)
                frame.sample_rate = 48000
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, 48000)
                self._pts += 960
                return frame

            def stop(self):
                try:
                    if self._proc and self._proc.poll() is None:
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                super().stop()

        return PulsePCMTrack()


async def _create_peer_answer(m, sdp, type_, user):
    """Create and signal the peer entirely on its owning asyncio loop."""
    global _last_error
    if len(_peers) >= _MAX_PEERS:
        raise RuntimeError(f"too many WebRTC peers (cap {_MAX_PEERS})")
    pc = m["RTCPeerConnection"](
        m["RTCConfiguration"](iceServers=[m["RTCIceServer"](urls=config.WEBRTC_STUN.split(","))])
        if config.WEBRTC_STUN
        else None
    )
    _peers.add(pc)

    try:
        video, audio_tr = _make_tracks(m)
        pc._rd_video, pc._rd_audio = video, audio_tr
        pc.addTrack(video)
        pc.addTrack(audio_tr)
    except Exception as e:
        _peers.discard(pc)
        _last_error = f"capture setup failed: {type(e).__name__}: {e}"
        try:
            from . import logging as log

            log.log(f"webrtc capture setup failed: {_last_error}")
        except Exception:
            pass
        raise RuntimeError(_last_error) from e

    def _cleanup():
        _peers.discard(pc)
        for t in (getattr(pc, "_rd_video", None), getattr(pc, "_rd_audio", None)):
            try:
                t.stop()
            except Exception:
                pass

    @pc.on("connectionstatechange")
    def _state():
        global _last_error
        try:
            from . import logging as log

            detail = f"connection={pc.connectionState} ice={pc.iceConnectionState}"
            log.log(f"webrtc peer {detail}")
            if pc.connectionState == "failed" or pc.iceConnectionState == "failed":
                _last_error = f"ICE failed ({detail}); UDP may be blocked by the VM firewall/NAT"
        except Exception:
            pass
        if pc.connectionState in ("failed", "closed", "disconnected"):
            _cleanup()

    try:
        await pc.setRemoteDescription(m["RTCSessionDescription"](sdp=sdp, type=type_))
        ans = await pc.createAnswer()
        await pc.setLocalDescription(ans)
        result = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        try:
            from . import logging as log

            log.log(
                f"webrtc offer accepted user={user} video_size={_video_size()} "
                f"connection={pc.connectionState} ice={pc.iceConnectionState}"
            )
        except Exception:
            pass
    except Exception as e:
        _cleanup()
        _last_error = f"{type(e).__name__}: {e}"
        raise
    audit.event(audit.SECURITY, "webrtc_offer", user=user, result="ok", detail={"peers": len(_peers)})
    return result


def handle_offer(sdp: str, type_: str, user: str):
    """SDP offer in, answer dict out. aiortc objects are created and used
    only on the dedicated media loop; touching PeerConnection from the
    HTTP worker thread causes ICE/DTLS state corruption."""
    m = _import_aiortc()
    if m is False:
        raise RuntimeError(_log_reason)
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(_create_peer_answer(m, sdp, type_, user), _loop)
    global _last_error
    try:
        answer = fut.result(timeout=30)
        _last_error = ""
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        audit.event(audit.SECURITY, "webrtc_offer", user=user, result="failed", detail={"error": _last_error[:240]})
        raise
    return answer


def stop_all():
    """Close every peer (server shutdown)."""
    for pc in list(_peers):
        try:
            pc.close()
        except Exception:
            pass
    _peers.clear()


def update_ice_state(ice_state, peer_state=None):
    """Apply client-reported ICE state to diagnostics; never grants trust."""
    global _last_error
    if ice_state == "failed":
        _last_error = "browser reports ICE failed; UDP may be blocked by the VM firewall/NAT"
    elif ice_state == "connected" or ice_state == "completed":
        _last_error = ""


def status():
    return {
        "enabled": config.WEBRTC,
        "available": available()[0],
        "reason": available()[1],
        "peers": len(_peers),
        "last_error": _last_error,
        "states": [
            {"connection": getattr(p, "connectionState", "unknown"),
             "ice": getattr(p, "iceConnectionState", "unknown")}
            for p in list(_peers)
        ],
    }
