# ADR-0007: WebRTC transport via aiortc (opt-in)

Date: 2026-11-14 · Status: accepted (supersedes the no-go in ADR-0006
for v3.0.0) · Builds on #137.

## Context

ADR-0006 deferred WebRTC: the stdlib-only rule made a DTLS/SCTP/RTP
stack impossible, and HTTPS-only tunnels complicate ICE. The user's
actual experience settled it: the funnel path adds ~100–150 ms of RTT,
noVNC decodes in JavaScript on the client CPU, and full-motion video
(YouTube at 4 fps) exposes both. WebRTC fixes the client side
structurally: real media tracks, hardware decode, congestion control,
jitter buffers.

## Decision

- **aiortc as an opt-in extra** (`pip install aiortc`), feature-flagged
  with `RD_WEBRTC=1`. The runtime stays stdlib-only when the flag is
  off; `available()` reports what is missing instead of failing.
- **Hybrid transport**: WebRTC carries the outbound heavy media
  (framebuffer video + desktop audio). The noVNC websocket remains
  connected and keeps carrying INPUT (pointer, keys). Input is a few
  bytes per event; video is megabits. Splitting them keeps the
  battle-tested input path and the touch layer untouched.
- **One capture per peer** (aiortc MediaPlayer over x11grab +
  the PulseAudio monitor), peer cap 4. The #105 shared-capture fan-out
  is the follow-up that makes N viewers cost one capture.
- **Signalling** is a single authenticated POST (`/api/webrtc/offer`)
  behind the same CSRF/origin gates as everything else.
- **ICE**: STUN (default `stun:stun.l.google.com:19302`) so the server
  offers its public reflexive candidate; direct deployments (tailnet,
  LAN) can clear `RD_WEBRTC_STUN`. No TURN in v1 — if UDP is blocked
  end-to-end the feature fails visibly and the noVNC canvas keeps
  working.

## Consequences

- +~40 MB of installed packages when the feature is enabled; none when
  it is not.
- The VM encodes the desktop a second time while the noVNC canvas is
  hidden but still streaming — pause-the-canvas is future work.
- Azure NSG (or any host firewall) must allow inbound UDP for ICE.
- Firefox/Safari/Chrome all support the receive side; the hardware
  decode benefit applies on every GPU-equipped client.

## Verification

- Loopback self-test (`WEBRTC_SELFTEST=1`): an aiortc client peer
  exchanges media with the server peer without a browser.
- End-to-end: enable the toggle in More → the video element replaces
  the canvas; the RTT indicator keeps reporting the websocket path.
