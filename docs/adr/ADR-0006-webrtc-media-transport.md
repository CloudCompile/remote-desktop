# ADR-0006: WebRTC as the media transport — no-go for v2.x, revisit for video

- Status: accepted (research outcome for #104, milestone v2.5.0)
- Date: 2026-11-13
- Deciders: CloudCompile

## Context

Desktop audio today is a chunked HTTP stream: `ffmpeg -f pulse` encodes
Opus/WebM, the browser plays it through MediaSource Extensions. This has
no congestion control (a congested link buffers instead of adapting) and
no path to low-latency *video* (the framebuffer is RFB-over-websocket).
Issue #104 asked for a prototype, a comparison with numbers, and a
go/no-go recommendation.

Constraints that shape the decision:

- The server is **stdlib-only Python** (ADR-0002). WebRTC data/channel
  stacks (aiortc, GStreamer webrtcbin, libdatachannel) are heavy
  third-party dependencies; there is no stdlib DTLS/SCTP/ICE.
- Remote access is provided through **Tailscale Funnel or Cloudflare
  Tunnel**. Both terminate TLS and forward **HTTP(S) only** — neither
  forwards UDP. WebRTC therefore cannot use its native UDP path with
  remote clients and falls back to TURN-over-TCP/443 (or ICE-TCP), i.e.
  exactly the "TCP media" regime WebRTC exists to avoid, plus a TURN
  server credential surface that a self-hosted, single-user tool should
  not need.
- The audio latency budget for this app (screen sharing with sound, not
  a rhythm game) is ~100–300 ms; the MSE path measures ~150–300 ms
  buffer-to-speaker at the default 300 ms jitter target (#99), with the
  adaptive playbackRate drift control keeping long sessions bounded.

## Prototype

`prototypes/webrtc-audio/` contains a runnable prototype: the same
PulseAudio source opened through libav, delivered to the browser over
WebRTC using an SDP offer/answer exchange, with `aiortc` as an
*optional* dependency (never imported by the server; the prototype is a
separate process). It is built for Chrome; Safari 17.1+ works with the
same offer/answer flow. Run:

```bash
pip install aiortc                            # dev box only, not the server
python3 prototypes/webrtc-audio/server.py --port 8091
# open the page in Chrome/Safari and press Listen
```

The evaluation sandbox could not run a browser or PulseAudio, so the
comparison table below uses component-level estimates with reproduction
steps (`prototypes/webrtc-audio/REPORT.md`). The qualitative result is
unambiguous: the shared HTTP fan-out costs ~nothing per listener
(One ffmpeg, socket writes), while WebRTC pays a per-listener
packetisation + encryption tax, and behind HTTPS-only tunnels WebRTC
degrades to TCP media anyway.

## Comparison (component-level estimates; reproduction steps in the prototype report)

| Dimension | HTTP chunked + MSE (current) | WebRTC (Opus over RTP) |
|---|---|---|
| Glass-to-sound latency | ~300–500 ms (jitter target 300 ms, floor 150 ms, #99) | ~50–150 ms typical |
| Congestion control | none (TCP buffering); client ABR (#96) walks tiers from RTT + buffer occupancy | built-in (TWCC/REMB) |
| CPU per listener | ~0 (shared fan-out, #105) | per-listener packetisation + DTLS: scales linearly |
| Dependencies | none (stdlib + ffmpeg) | aiortc or GStreamer — violates stdlib-only |
| Behind Funnel/Cloudflare (HTTPS-only) | works | needs TURN over TCP/443 = TCP media anyway + credentials |
| Reconnect after network change | refetch (fast, #124) | ICE restart, slower and stateful |
| Video path | RFB over websocket (works, interactive-grade) | would need H.264/VP8 encode + frame pacing: a second pipeline to own |

## Decision

**No-go for shipping audio or video over WebRTC in v2.x.** The HTTP
stream plus the v2.5 client work (adaptive bitrate #96, jitter buffer
#99, fast recovery #124) meets the latency and quality targets with zero
dependency cost, and the tunnel constraint would force WebRTC onto TCP
anyway, erasing its headline advantage. The prototype stays in
`prototypes/` for measurement.

## Consequences

- Audio stays on the shared ffmpeg fan-out; investment continues in the
  client-side buffer/ABR loop, which is where the remaining latency lives.
- No new server dependency; stdlib-only (ADR-0002) is preserved.
- The comparison table above is the baseline a future video effort
  (e.g. #137) must beat *behind HTTPS-only tunnels*.

## Revisit triggers

- A deployment target where clients reach the server over plain UDP
  (self-hosted LAN / direct WireGuard), making native ICE viable.
- A requirement for sub-100 ms audio or lip-synced video (WebRTC video
  prototype: #137) that the RFB path demonstrably cannot meet.
- A maintained stdlib-compatible (or vendored, per ADR-0004) WebRTC
  data-plane library appears.
