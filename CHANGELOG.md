# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/), semver.

## [Unreleased]

### Planned

- v2.1.0 through v3.0.0 — see [ROADMAP.md](ROADMAP.md) for milestones,
  release themes and linked issues.

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
