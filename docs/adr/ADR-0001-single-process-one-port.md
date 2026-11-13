# ADR-0001: Single process, one port

- Status: accepted
- Date: 2026-11-01
- Deciders: CloudCompile

## Context

The desktop server must serve the noVNC frontend, a JSON control API, the
VNC websocket stream, live audio, clipboard and file transfer from a
phone over one HTTPS URL. Typical stacks glue nginx + websockify + a
framework + several daemons together — more moving parts than the whole
project, each with its own TLS/auth/upgrade semantics.

## Decision

One Python process (`remote_desktop`, started by `control.py`) serves
everything on one port (default 127.0.0.1:8090): static files, JSON API,
websocket→VNC proxy, audio streams. TLS termination is delegated to a
tunnel/reverse proxy.

## Consequences

- Deployment is two systemd units (session watchdog + server) and one
  port; there is exactly one auth path and one log file.
- A threading HTTP server multiplexes everything; concurrency must be
  bounded in-process (#28) because there is no front server to shed load.
- A crash takes down every function at once — mitigated by the session
  watchdog owning Xvnc/PulseAudio and by bounded, observable streams.

## Revisit triggers

Needing multi-host scaling, HTTP/2/3-specific features at the edge, or
request rates where a threaded stdlib server measurably caps throughput
(see docs/PERFORMANCE.md budgets).
