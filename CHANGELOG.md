# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/), semver.

## [Unreleased]

### Planned

- v2.0.1 through v3.0.0 — see [ROADMAP.md](ROADMAP.md) for milestones,
  release themes and linked issues.

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
