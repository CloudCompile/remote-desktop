# WebRTC vs HTTP audio — evaluation report (#104)

Findings behind ADR-0006. No production numbers were collected for this
report: the evaluation sandbox has no PulseAudio/browser pair, so the
figures below are **estimates built from component measurements and
published data**, each with the method to reproduce it on real
hardware. The prototype in this directory is the harness for that
reproduction.

## Latency (buffer-to-speaker) — estimated

| Transport | Estimate | Basis |
|---|---|---|
| HTTP chunked + MSE (production, jitter target 300 ms, #99) | ~300–500 ms | target latency is a configured constant (300 ms, floor 150 ms); plus one 20 ms opus frame and socket buffering |
| HTTP chunked + MSE (target 150 ms) | ~175–310 ms | observed informally with the v2.4 client's fixed resync thresholds |
| WebRTC (prototype, host candidates) | ~50–150 ms | no client jitter buffer; typical RTC figures |

Reproduce: `python3 server.py`, open the page in Chrome/Safari, tap
Listen, and clap-sync against the production path (both play the same
monitor source).

## CPU on the host — estimated

| Listeners | HTTP shared fan-out (#105) | WebRTC (per-listener RTP) |
|---|---|---|
| 1 | one shared ffmpeg, ~2–5 % of one core for 44.1 kHz opus encode | + aiortc per-listener packetisation/DTLS: ~5–10 % of one core |
| 3 / 5 | +≈0 per listener (fan-out is socket writes only) | scales linearly (~3–5× / 5–8× the single-listener cost) |

Reproduce the production side without PulseAudio:
`ffmpeg -f lavfi -i sine=frequency=440:duration=60 -c:a libopus -b:a 96k -f webm - > /dev/null`
under `time`/`top`; add listeners by `tee`-ing the pipe to N readers.

## Reconnect behaviour — qualitative (from the code paths)

| Scenario | HTTP path | WebRTC |
|---|---|---|
| Tab backgrounded | reader continues/resumes; live-edge resync (#99) | ICE usually stays up |
| Network switch | refetch on reconnect (#124), ~1 s | ICE restart, seconds, may need a gesture |
| PulseAudio restart | next start recreates the pipeline (#124) | track ends; full renegotiation |

## NAT / tunnel implications

Tailscale Funnel and Cloudflare Tunnel terminate TLS and forward
HTTP(S) only — no UDP. WebRTC from remote clients therefore requires a
TURN server (over TCP/443), which turns the media into TCP anyway, adds
a credential surface, and removes the latency advantage that motivated
WebRTC. Only LAN/direct-WireGuard deployments see the native path.

## Conclusion

No-go for v2.x (ADR-0006): the HTTP path with the v2.5 adaptive
buffering meets the latency budget at a fraction of the complexity and
CPU. Revisit for sub-100 ms audio or lip-synced video on UDP-reachable
deployments.
