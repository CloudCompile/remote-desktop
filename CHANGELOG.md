# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/), semver.

## [Unreleased]

### Planned

- v3.0.0 — see [ROADMAP.md](ROADMAP.md) for milestones, release themes
  and linked issues.

## [2.6.1] — 2026-11-14

### Fixed

- PCM audio fallback called `/api/audio/raw`; the route is
  `/api/audio.raw` — Firefox (and every non-MSE browser) had no audio
  at all (#100).
- The #121 confirm dialog could never hide: its inline `display:flex`
  overrode the `hidden` attribute, so an empty dialog covered the app
  from page load and Cancel/Confirm appeared dead. Styling moved to the
  stylesheet with a `#confirmScrim[hidden]` enforcement.
- The origin check rejected every mutation from behind a reverse proxy
  (Funnel/Caddy): the browser's Origin did not match the proxy-target
  Host. X-Forwarded-Host from trusted proxies is now honoured, and
  deployments can allow-list origins with RD_ALLOWED_ORIGINS (#116).
- Duplicate `fmtWhen` declaration broke the JS syntax check under
  Node 20.
- The service-worker cache version now tracks the release via a CI
  check — it had been frozen at v2.2.0, serving stale client code to
  every browser since (#128 follow-up).

## [2.6.0] — 2026-11-14

### Added

- Multi-user accounts with isolated sessions: a stdlib user store
  (crypt SHA-512), automatic migration of single-user deployments, and
  viewer/operator/admin role caps enforced per endpoint (#111, #112).
- TOTP two-factor authentication with recovery codes, implemented in
  pure-stdlib RFC 6238 (#109).
- Optional passkey/WebAuthn login behind an opt-in flag (#110).
- Session and device management: persistent registry, device labels,
  immediate revoke, per-user session caps (#114).
- CSRF double-submit tokens plus Origin/Sec-Fetch-Site validation on
  every state-changing endpoint (#116).
- Strict CSP and security headers on every response (#117).
- Tamper-evident audit log: hash-chained JSONL with rotation that
  preserves recent events and a chain-verification endpoint (#113).
- Per-user isolation for file roots, clipboard histories and pins
  (#120).
- Signing-key rotation with a grace window — no downtime, no restart
  (#118).
- Hardened cookies: Secure flag, optional __Host- prefix, proxy
  awareness (#119).
- Persistent rate limiting with fail2ban-friendly events (#115).
- Login notifications and new-device alerts (#123).
- Confirmation flows for sensitive actions (#121).
- Optional VNC password (pure-stdlib classic VNC auth) with fail-closed
  proxy behaviour and loopback-exposure warnings (#122).
- Security regression suite under tests/security/ (#125).
- docs/SECURITY-POSTURE.md: threat model and STRIDE review with
  accepted risks and review cadence (#126).
- RFC 9116 security.txt and a refreshed disclosure workflow with
  response targets, safe harbour and a compromised-host runbook (#127).
- CodeQL for Python and JavaScript, grouped Dependabot updates, and a
  CycloneDX SBOM attached to every release; GitHub Actions pinned by
  commit SHA (#128).

## [2.5.0] — 2026-11-13

### Added

- Audio device control: sink enumeration with volume/mute, persisted
  and re-applied at startup, with graceful fallback when a sink
  disappears (#94).
- Per-application stream volume grouped by app, with per-app defaults
  inherited by new streams (#95).
- Audio quality tiers: low/medium/high/custom opus bitrate with server
  compression tuning, and an adaptive client mode driven by RTT and
  buffer health (#96).
- Shared capture: one ffmpeg per (source, codec, bitrate) fans out to
  multiple listeners with bounded per-listener queues; slow listeners
  are dropped without stalling the producer (#105).
- Audio-only mode: video disconnected, thumbnails and polling paused,
  persistent badge, instant restore (#98).
- Jitter management: adaptive target, drift-corrected playback rate,
  live-edge resync, underrun/overrun counters on both Opus and PCM
  paths (#99).
- Playback capability probing: MSE/Opus and WebAudio detected before
  any ffmpeg spawn, with actionable feedback and a PCM fallback (#100).
- Stream recovery: visible Retry state, audio restarts on reconnect and
  tab visibility, server-side recovery from PulseAudio restarts (#124).
- Media-keys panel via MPRIS with XF86 fallback and active-player
  display (#101).
- Audio recording to the files root in self-contained clusters with
  time/size caps, clean finalization and crash recovery (#102).
- Level meter (RMS/peak, 5 Hz, sheet-open only) distinguishing silence,
  capture stall and clipping (#103).
- Audio diagnostics view with redacted one-tap copy and a test tone
  (#106).
- docs/TROUBLESHOOTING.md: PulseAudio vs PipeWire, symptom→cause→fix
  matrix (#107).
- ADR-0006 (WebRTC audio deferred past v2.x, with rationale and a
  runnable prototype) (#104).

### Fixed

- Removed a duplicate `/api/pins` route (first match won; harmless).

## [2.4.0] — 2026-11-13

### Added

- Multi-root file manager: `RD_FILES_ROOTS` with labels and read-only /
  quota flags, listings with type/size/mode/owner, breadcrumbs, mkdir,
  rename, move, copy and bulk delete with per-item results (#84, #79).
- Chunked resumable uploads: per-chunk SHA-256, fsync'd acknowledgements,
  retry at the failed offset, atomic landing, TTL garbage collection and
  a bounded session count (#78).
- Web upload queue: drag-drop and multi-pick, concurrency cap,
  server-acked progress, pause/retry/cancel, duplicate handling and
  navigation-surviving queue state (#80, #88).
- Sorting, bounded recursive search and bulk selection (#81).
- Previews for images/PDF/video on a private temp copy via argv-only
  host tools, with download fallback and inline-execution never (#82).
- Range downloads: 206/416 with Content-Range, ETag and bounded
  streaming (#83).
- Archive create (zip/tar.gz/tar.xz) and safe extraction with zip-slip
  and zip-bomb protection (#85).
- Unicode-safe names: NFC/casefold collision detection, RFC 5987
  download filenames (#86).
- Share target + camera capture input from the installed PWA (#87).
- Disk space reporting, `RD_FILES_RESERVE` and per-root quotas (#89).
- chmod/executable toggle via fchmod on O_NOFOLLOW fds; setuid/setgid
  refused; JSONL audit log with rotation and an authenticated read view
  (#90, #92).
- docs/FILES.md (containment model, layout, troubleshooting) and a full
  API reference (#93).

### Security

- **Path containment rewritten (P0, #91)**: every path component is
  opened `O_NOFOLLOW` relative to a per-op directory fd with kernel
  verification — no verify-then-use window; traversal, null-byte and
  control-char payloads rejected before touching the filesystem.

## [2.3.0] — 2026-11-13

### Added

- Window switcher with live thumbnails (single-frame JPEG via ffmpeg
  x11grab, size-capped) and window actions: focus, minimize, maximize,
  restore, center, close (#59, #60).
- Layout slots: apply halves/thirds tiling presets to any window (#61).
- Flatpak and Snap discovery merged into `/api/apps` with a `source`
  field; focus matching handles Flatpak/Snap WM_CLASS quirks (#65, #73).
- App Store: install/uninstall/update with a serial apt queue, queue
  position, cancel, and live per-install log tail; curated collection
  chips; backend capability reporting (#66, #67, #69).
- Clipboard v2: pinning (pinned items never trim), text search, tags,
  inline image previews with save-to-phone, and image/URI paste-back
  (#70, #71).
- Opt-in phone→desktop clipboard sync via a visible-tab poll, default
  off, with a documented consent model (#72).
- Paste-as-keystrokes mode with confirm and rate limiting (#74).
- Launch-failure toasts with hints and request ids (#75).
- Per-app window geometry persistence and restore, toggleable (#76).
- Custom app entries with validated CRUD and a UI editor (#63).
- Frequent/recency ranking and pin folders for the app grid (#62, #64).
- ETag/304 caching on `/api/apps` (#77).

### Fixed

- windows.py WM_CLASS guard for focus matching; display geometry is now
  reported in `/api/windows`.

## [2.2.0] — 2026-11-13

### Added

- Direct-touch mode: absolute taps and immediate drag at the touched
  pixel, selectable against the trackpad layer and persisted (#41).
- Pinch-to-zoom (1–5×, focal anchoring) with pan when zoomed and
  double-tap reset; two-finger scroll is unchanged at 1× (#42).
- Viewport modes: Fit / Fill / 1:1 / Remote-resize (noVNC resizeSession),
  persisted and reapplied after reconnect (#43).
- Three-finger horizontal swipe sends Alt+Tab (#44).
- Optional haptic feedback on taps and long-presses (#45).
- Gesture customization: pointer speed, scroll speed, tap tolerance and
  long-press delay sliders, live-applied and persisted (#46).
- IME composition: composed text is committed via the paste API instead
  of per-key events; sticky modifiers now apply to the next key (#47).
- User-editable extra-keys panel (add/remove/reorder, keysym validation,
  localStorage) (#48).
- Installable PWA: manifest, service worker with offline shell,
  install button and iOS hint (#49).
- Connection quality indicator on the toolbar handle plus a diagnostics
  section (RTT, reconnect count, viewport, server version) (#50).
- Dark / light / high-contrast / auto themes with pre-paint application (#51).
- Orientation lock with graceful degradation and a tablet landscape
  layout for sheets (#52).
- Localization: EN/DE/ES/FR for the static shell with instant switching
  and an English fallback (#54).
- First-run tour (three dismissible cards, replayable from More) (#56).
- Toolbar customization: show/hide and reorder actions, long-press to
  hide with undo (#68).

### Fixed

- iOS Safari pointer/gesture conflicts: page pinch-zoom prevented,
  touch-action on chrome, stuck-button panic paths (#57).
- Safe-area and virtual-keyboard insets via visualViewport with
  debounced keyboard animation handling (#58).

### Accessibility

- Accessible names on icon-only controls, dialog semantics for sheets
  with focus trap and restore, focus-visible rings, reduced-motion
  support (#53).

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
