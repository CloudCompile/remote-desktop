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
        from aiortc.contrib.media import MediaPlayer

        _mod = {
            "RTCPeerConnection": RTCPeerConnection,
            "RTCSessionDescription": RTCSessionDescription,
            "RTCConfiguration": RTCConfiguration,
            "RTCIceServer": RTCIceServer,
            "MediaPlayer": MediaPlayer,
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


def _media_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
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
    audio_tr = m["MediaPlayer"](f"pulse://{audio.audio_source()}", format="pulse").audio
    return video, audio_tr


def handle_offer(sdp: str, type_: str, user: str):
    """SDP offer in, answer dict out (runs on the media loop)."""
    m = _import_aiortc()
    if m is False:
        raise RuntimeError(_log_reason)
    if len(_peers) >= _MAX_PEERS:
        raise RuntimeError(f"too many WebRTC peers (cap {_MAX_PEERS})")
    _ensure_loop()

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
        raise RuntimeError(f"cannot open desktop capture: {e}")

    def _cleanup():
        _peers.discard(pc)
        for t in (getattr(pc, "_rd_video", None), getattr(pc, "_rd_audio", None)):
            try:
                t.stop()
            except Exception:
                pass

    @pc.on("connectionstatechange")
    def _state():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            _cleanup()

    async def _answer():
        await pc.setRemoteDescription(m["RTCSessionDescription"](sdp=sdp, type=type_))
        ans = await pc.createAnswer()
        await pc.setLocalDescription(ans)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    fut = asyncio.run_coroutine_threadsafe(_answer(), _loop)
    try:
        answer = fut.result(timeout=20)
    except Exception:
        _cleanup()
        raise
    audit.event(audit.SECURITY, "webrtc_offer", user=user, result="ok", detail={"peers": len(_peers)})
    return answer


def stop_all():
    """Close every peer (server shutdown)."""
    for pc in list(_peers):
        try:
            pc.close()
        except Exception:
            pass
    _peers.clear()


def status():
    return {"enabled": config.WEBRTC, "available": available()[0], "reason": available()[1], "peers": len(_peers)}
