# WebRTC audio prototype (#104)

Measures the WebRTC transport against the production HTTP/MSE path.
The server process is **separate** from control.py and needs the
optional `aiortc` dependency on a dev box — the shipped server stays
stdlib-only (ADR-0002).

```
PulseAudio monitor ──ffmpeg──► Opus/RTP ──aiortc──► browser (WebRTC PeerConnection)
                                     ▲
   offer/answer: GET /offer (SDP in, SDP out, no ICE servers = host candidates only)
```

Run it against a desktop that has PulseAudio and ffmpeg:

```bash
python3 -m pip install aiortc          # prototype only
python3 server.py --port 8091          # serves page.py.html content at /
```

Then open `https://<host>:8091/` (Chrome, Safari 17.1+), press
**Listen**, and compare glass-to-sound latency with the production path
(Audio sheet → Start). `REPORT.md` records the numbers behind
ADR-0006's comparison table (latency, CPU per listener, reconnect
behaviour, complexity) and the NAT/tunnel implications.
