# ADR-0003: WebSocket→TCP proxy for VNC

- Status: accepted
- Date: 2026-11-01
- Deciders: CloudCompile

## Context

Browsers cannot open raw TCP; Xvnc speaks RFB over TCP on 127.0.0.1:5901.
The classic answer is a separate websockify service (plus its own port,
config and auth story).

## Decision

The server implements a minimal RFC6455 websocket→TCP proxy at
`/websockify` (remote_desktop/vncproxy.py): parse client frames, forward
payload bytes to the VNC socket, forward upstream bytes as binary frames
— both directions with bounded buffers (#29) and transparent upstream
reconnect (#39). The client stays stock noVNC.

## Consequences

- One port, one auth-adjacent surface; no separate websockify daemon.
- Byte-proxying keeps latency flat; the proxy never interprets RFB.
- Server-side reconnect can bridge short Xvnc restarts, but RFB state
  lives in the client — noVNC still owns higher-level recovery; the
  server preserves byte order and closes cleanly on permanent failure.
- The proxy must be careful with untrusted input (masking, frame caps),
  which the ws.py reader enforces in one place.

## Revisit triggers

A need to shape RFB traffic (e.g. server-side framebuffer caching,
multi-client fan-out) would turn this into a real VNC gateway — a
different project — or a maintained websockify dependency.
