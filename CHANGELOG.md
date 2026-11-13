# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/), semver.

## [Unreleased]

### Planned

- v2.2.0 through v3.0.0 — see [ROADMAP.md](ROADMAP.md) for milestones,
  release themes and linked issues.

## [2.1.0] — 2026-11-13

### Added

- `control.py` is now a thin entry point over the `remote_desktop/`
  package — config, logging, auth, HTTP, websockets, VNC proxy, audio,
  media-in, clipboard, files, apps, store, windows and stats are separate
  testable modules with an ownership map in docs/ARCHITECTURE.md (#22).
- Centralized `RD_*` configuration schema with startup validation,
  `--check-config`, optional `RD_CONFIG` file and a generated
  docs/CONFIGURATION.md (#23).
- Structured logging with levels (`RD_LOG_LEVEL`), optional JSON output
  (`RD_LOG_FORMAT`), rotation, and an `X-Request-Id` header per request (#24).
- Readiness checks for X, VNC, PulseAudio and ffmpeg via `/api/ready` (#40).
- Versioned state backup/restore: automatic snapshots on start,
  `POST /api/backup`, `GET /api/backup`, `--export-state`/`--import-state`
  with manifest checksums (#30).
- Configurable display geometry and DPI (`RD_GEOMETRY`, `RD_DPI`) with
  last-client-size persistence (#31).
- Transparent VNC proxy reconnect: Xvnc restarts no longer drop the
  websocket; client frames are buffered and replayed in order (#39).
- Connection caps (connections, websockets, audio streams, device-in
  streams) with fast 503 + Retry-After overloads, idle timeouts and live
  counts in `/api/stats` (#28).
- WebSocket backpressure: bounded buffers, slow-client write deadlines,
  frame caps and drop counters (#29).
- End-to-end integration tests (HTTP, auth, websocket byte fidelity,
  shutdown reaping) and a 40% coverage gate with a Python 3.10/3.12 CI
  matrix (#34, #35).
- Architecture decision records (docs/adr/), a benchmark script with a
  performance budget doc, and pre-commit hooks in check-only mode (#36,
  #37, #38).
- webmanifest/json MIME mappings for the new PWA shell assets.

### Fixed

- session.sh PID handling: watchdog no longer double-starts, stale locks
  are cleaned only when unowned, TERM/INT stops Xvnc and PulseAudio
  cleanly (#32).
- Graceful shutdown: SIGTERM stops the listener, closes clients, reaps
  ffmpeg/VNC children with a bounded wait; second signal forces exit (#25).

### Security

- systemd units hardened: NoNewPrivileges, PrivateTmp, ProtectSystem=full,
  ProtectHome=read-only with explicit ReadWritePaths, address-family
  restrictions and NOFILE limits (#33).

## [2.0.1] — 2026-11-13

### Added

- pytest harness with 30 unit tests covering token auth, websocket
  framing, clipboard persistence, rate limiting and upload safety (#11).
- CI now runs ruff, pytest, `node --check` on all custom JS, a docs-link
  checker and vendored-code integrity verification (#13, #20).
- `/api/health` reports version, uptime, active stream count and
  ffmpeg/pactl availability (#18).
- `docs/KNOWN-LIMITATIONS.md` and documented upgrade/rollback steps in
  docs/DEPLOYMENT.md (#19, #21).
- Vendored noVNC and pako provenance with SHA-256 checksums
  (docs/VENDORED.md + docs/VENDORED.sums) (#20).
- Release workflow validates the tagged version is the newest, dated
  changelog section before publishing (#12).

### Fixed

- WebSocket reader reassembles fragmented messages, rejects unmasked
  client frames, stray continuations and oversized payloads (#10).
- File uploads are atomic: streamed to a hidden temp file, fsynced, then
  renamed into place; stale partials are cleaned up on start (#15).
- ffmpeg/audio children are registered and reaped on shutdown; stream
  open/close is logged with duration (#14).
- Clipboard history persistence is crash-safe: atomic writes, corrupt
  files quarantined instead of crashing, entries validated on load (#17).
- Login rate limiting no longer grows without bound and honours
  X-Forwarded-For only from explicitly trusted proxies
  (`RD_TRUSTED_PROXIES`) (#16).

### Security

- Hidden dotfiles are rejected as upload/download names.
- `crypt` import is guarded for Python 3.13 compatibility.

## [2.0.0] — 2026-11-11

### Added

- Single-process server (`control.py`): static frontend, JSON API,
  websocket → VNC proxy, audio streaming, clipboard and file transfer on
  one port.
- Mobile-first UI with a thumb-reachable bottom toolbar, top sheets,
  auto-reconnect with backoff, and wake lock.
- Trackpad touch layer with acceleration, tap/double-tap-drag, long-press
  right-click, two-finger scroll, three-finger middle-click.
- Dynamic app menu from `.desktop` files with icons, categories, pinning,
  and focus-existing-window launching.
- App store search/install via AppStream/apt.
- Multi-item clipboard history (CLIPBOARD + PRIMARY, persisted).
- File upload/download/delete into `~/Downloads`.
- Opus/WebM and PCM audio streams with drift recovery.
- Live system stats.
- Password auth on all endpoints, signed 7-day cookie, login rate limit.
- `systemd/` units, `session.sh` Xvnc + PulseAudio watchdog.
- Tailscale Funnel / Cloudflare tunnel support and `tunnel-url.sh`.

### Fixed

- Chrome launches now focus the browser window instead of the first Chrome
  `--app` window (WM_CLASS instance matching).
- Bottom toolbar no longer blocks the remote desktop: it is click-through,
  hides on touch, and its reserved nav-bar inset is only applied when the
  OS actually overlays the page.
- Toolbar discoverability: labeled **▲ Controls** handle instead of a faint
  sliver.
