#!/usr/bin/env python3
"""WebRTC audio prototype server (#104) — dev-box only.

Serves a minimal HTML page and an SDP offer/answer endpoint. The audio
track comes from the local PulseAudio monitor (opened through libav,
like the production ffmpeg pipeline), so what you hear is exactly what
the HTTP path captures. aiortc is an OPTIONAL dev dependency;
control.py never imports this file.

Usage:
    python3 -m pip install aiortc          # prototype only
    python3 server.py --port 8091 --source remote_out.monitor
    open https://<host>:8091/ in Chrome (or Safari 17.1+) and press Listen
"""

import argparse
import http.server
import json

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

PAGE = """<!doctype html><title>WebRTC audio prototype</title>
<button onclick="go()">Listen</button><pre id="log"></pre>
<script>
async function go(){
  const pc = new RTCPeerConnection();          // no ICE servers: host candidates only
  pc.ontrack = e => {
    const a = new Audio();
    a.srcObject = e.streams[0];
    a.autoplay = true;
    document.body.append(a);
  };
  pc.addTransceiver('audio', { direction: 'recvonly' });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const r = await fetch('/offer', { method: 'POST', body: JSON.stringify(pc.localDescription) });
  await pc.setRemoteDescription(await r.json());
  document.getElementById('log').textContent =
    'connected (WebRTC); compare with the HTTP path in the main UI';
}
</script>"""


def make_track(source):
    """MediaPlayer over the PulseAudio monitor: libav handles capture,
    aiortc packetises Opus/RTP — one encoder per connected viewer."""
    return MediaPlayer(f"pulse://{source}", format="pulse").audio


connections = set()


async def handle_offer(body, source):
    pc = RTCPeerConnection()
    connections.add(pc)
    pc.addTrack(make_track(source))

    @pc.on("connectionstatechange")
    def _gone():
        if pc.connectionState in ("failed", "closed"):
            connections.discard(pc)

    await pc.setRemoteDescription(RTCSessionDescription(**body))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


class H(http.server.BaseHTTPRequestHandler):
    source = "remote_out.monitor"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def do_POST(self):
        import asyncio

        n = int(self.headers.get("Content-Length", 0))
        answer = asyncio.run(handle_offer(json.loads(self.rfile.read(n)), self.source))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(answer).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--source", default="remote_out.monitor")
    args = ap.parse_args()
    H.source = args.source
    print(f"webrtc prototype on http://0.0.0.0:{args.port} (source: {args.source})")
    http.server.ThreadingHTTPServer(("0.0.0.0", args.port), H).serve_forever()
