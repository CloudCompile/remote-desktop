# ADR-0002: Stdlib-only server runtime

- Status: accepted
- Date: 2026-11-01
- Deciders: CloudCompile

## Context

The server runs on whatever Linux box owns the desktop — often a
minimal VPS or a long-lived Ubuntu install. Dependency installation
(virtualenvs, wheels, native libs) is the biggest source of "works on my
machine" and of upgrade breakage, and the HTTP/websocket surface is small
enough to implement carefully.

## Decision

The server runtime uses only the Python standard library: http.server for
HTTP, a hand-rolled RFC6455 websocket layer (#10, #22), json/hashlib/hmac
for auth, subprocess for tools (ffmpeg, xdotool, xclip, pactl). Dev-only
tools (pytest, ruff, pytest-cov) are not runtime dependencies.

## Consequences

- `git clone` + `python3 control.py` is the whole install; no pip, no
  virtualenv, no supply-chain surface in the runtime.
- We own (and must test) the websocket implementation; in exchange its
  behaviour — frame caps, backpressure, reconnect — is explicit and
  unit-testable (#29, #39).
- Some performance and convenience features (async I/O, ORM-grade
  routing) are out of reach; the workload (a handful of clients, byte
  proxying) does not need them.

## Revisit triggers

A hard requirement that cannot be met with stdlib (e.g. per-request
compression formats), or a measured need for higher concurrency than
threads provide on the target hardware.
