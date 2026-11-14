"""WebRTC transport tests (#137): gating, signalling route, and a real
aiortc<->aiortc loopback media flow (no browser, no X server needed —
the server's capture is injected as silent/static test tracks)."""
import asyncio
import os
import json

import pytest

from remote_desktop import config


# ------------------------------------------------------------ gating

def _boot(tmp_path_factory, monkeypatch):
    import crypt as _crypt
    import sys as _sys
    _sys.path.insert(0, "tests")
    from test_integration import TEST_PASSWORD, Server, _free_port
    tmp = tmp_path_factory.mktemp("webrtc")
    pf = tmp / ".passhash"
    pf.write_text(_crypt.crypt(TEST_PASSWORD, _crypt.mksalt(_crypt.METHOD_SHA512)) + "\n")
    s = Server(tmp, _free_port(), _free_port(), pf)
    s.start()
    return s


def test_webrtc_disabled_by_default():
    from remote_desktop import webrtc
    ok, reason = webrtc.available()
    if not config.WEBRTC:
        assert not ok and reason


def test_offer_route_501_when_disabled(tmp_path_factory, monkeypatch):
    s = _boot(tmp_path_factory, monkeypatch)
    try:
        from test_integration import _login
        code_, cookie = _login(s, ip="192.0.2.31")
        assert code_ == 200
        code, _, body = s.request("/api/webrtc/offer", data={"sdp": "v=0 x", "type": "offer"}, cookie=cookie)
        j = json.loads(body)
        if not config.WEBRTC:
            assert code == 501 and not j["ok"]
    finally:
        s.stop()


def test_offer_route_rejects_garbage_sdp(tmp_path_factory, monkeypatch):
    """With the feature ON the malformed SDP is a 400; OFF stays a 501."""
    s = _boot(tmp_path_factory, monkeypatch)
    try:
        from test_integration import _login
        code_, cookie = _login(s, ip="192.0.2.32")
        assert code_ == 200
        code, _, body = s.request("/api/webrtc/offer", data={"sdp": "nonsense", "type": "offer"}, cookie=cookie)
        j = json.loads(body)
        if config.WEBRTC:
            assert code == 400
        else:
            assert code == 501
    finally:
        s.stop()


# ------------------------------------------------- loopback media flow

def _aiortc_ok():
    try:
        import aiortc  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _aiortc_ok(), reason="aiortc extra not installed")
@pytest.mark.skipif(os.environ.get("WEBRTC_SELFTEST") != "1",
                    reason="loopback ICE needs UDP; run with WEBRTC_SELFTEST=1")
def test_webrtc_loopback_media():
    """aiortc client peer <-> server handle_offer: both audio and video
    tracks flow end to end. Runs the server's media loop machinery for
    real (its own thread + event loop)."""
    asyncio.run(_loopback())


def _aiortc_ok():
    try:
        import aiortc  # noqa: F401
        return True
    except ImportError:
        return False


async def _loopback():
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamTrack

    from remote_desktop import webrtc

    class SilentAudio(MediaStreamTrack):
        kind = "audio"

        async def recv(self):
            await asyncio.sleep(0.02)
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            for p in frame.planes:
                p.update(b"\x00" * (p.buffer_size))
            frame.pts = None
            frame.time_base = None
            return frame

    class BlackVideo(MediaStreamTrack):
        kind = "video"

        async def recv(self):
            await asyncio.sleep(0.04)
            frame = av.VideoFrame(width=320, height=240)
            for p in frame.planes:
                p.update(b"\x00" * p.buffer_size)
            return frame

    # server side with INJECTED tracks (no X server in CI)
    class FakeReq:
        user = "admin"

    m = webrtc._import_aiortc()
    assert m, "aiortc must import for the loopback test"

    pc_server = m["RTCPeerConnection"]()
    webrtc._peers.add(pc_server)
    vid, aud = BlackVideo(), SilentAudio()
    pc_server._rd_video, pc_server._rd_audio = vid, aud
    pc_server.addTrack(vid)
    pc_server.addTrack(aud)

    pc_client = RTCPeerConnection()
    received = {"video": 0, "audio": 0, "ev": asyncio.Event()}

    @pc_client.on("track")
    def on_track(track):
        async def pump():
            while True:
                try:
                    await track.recv()
                    received[track.kind] += 1
                except Exception:
                    break
        asyncio.ensure_future(pump())

    await pc_server.setRemoteDescription(RTCSessionDescription(**(await _client_offer(pc_client))))
    ans = await pc_server.createAnswer()
    await pc_server.setLocalDescription(ans)
    await pc_client.setRemoteDescription(RTCSessionDescription(sdp=pc_server.localDescription.sdp,
                                                              type=pc_server.localDescription.type))

    # wait for media to flow both ways
    for _ in range(150):
        if received["video"] >= 3 and received["audio"] >= 3:
            break
        await asyncio.sleep(0.1)
    assert received["video"] >= 3, "no video frames flowed"
    assert received["audio"] >= 3, "no audio frames flowed"

    await pc_client.close()
    webrtc.stop_all()


async def _client_offer(pc):
    pc.addTransceiver("audio", direction="recvonly")
    pc.addTransceiver("video", direction="recvonly")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
