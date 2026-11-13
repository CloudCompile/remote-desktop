# Roadmap

> **Status:** living document · **Last updated:** 2026-11-12
>
> [Project board](https://github.com/users/CloudCompile/projects/4) · [Milestones](https://github.com/CloudCompile/remote-desktop/milestones) · [Releases](https://github.com/CloudCompile/remote-desktop/releases) · [Open issues](https://github.com/CloudCompile/remote-desktop/issues)

This roadmap turns the v2.0.0 feature set into eight scheduled releases plus a parking lot. Every item below is a GitHub issue with acceptance criteria, labels, a milestone and a card on the project board. The roadmap is a planning contract, not a promise: scope may move between releases as we learn.

## Product direction

Make a real desktop usable from a phone or tablet, on a machine you control, without a build pipeline, a pile of services or a cloud account. The browser is the client; one Python process remains the server; Tailscale or Cloudflare provides the TLS edge.

### Principles

- **One process, one port, one owner.** Keep deployment boring and auditable.
- **Mobile first, not mobile only.** Every feature must work on a 360 px screen with touch, then improve on tablet and desktop.
- **Secure by default, honest about limits.** Localhost binding, explicit auth, no hidden cloud dependency, documented threat model.
- **Fix before you add.** Reliability and data-safety issues outrank new surface.
- **Small, reviewable releases.** Ship a coherent theme, keep `main` releasable.

## Release timeline

| Version | Theme | Target | Milestone | Epic | Issues | Status |
|---|---|---|---|---|---:|---|
| **v2.0.1** | Stabilize & Ship | 2026-11-25 | [v2.0.1](https://github.com/CloudCompile/remote-desktop/milestones/1) | [#1](https://github.com/CloudCompile/remote-desktop/issues/1) | 12 | ✅ Shipped 2026-11-13 |
| **v2.1.0** | Architecture & Reliability | 2026-12-20 | [v2.1.0](https://github.com/CloudCompile/remote-desktop/milestones/2) | [#2](https://github.com/CloudCompile/remote-desktop/issues/2) | 17 | ✅ Shipped 2026-11-13 |
| **v2.2.0** | Touch-First UX 2.0 | 2027-02-07 | [v2.2.0](https://github.com/CloudCompile/remote-desktop/milestones/3) | [#3](https://github.com/CloudCompile/remote-desktop/issues/3) | 18 | ✅ Shipped 2026-11-13 |
| **v2.3.0** | Apps, Windows & Clipboard | 2027-03-21 | [v2.3.0](https://github.com/CloudCompile/remote-desktop/milestones/4) | [#4](https://github.com/CloudCompile/remote-desktop/issues/4) | 18 | ✅ Shipped 2026-11-13 |
| **v2.4.0** | Files & Transfer | 2027-05-02 | [v2.4.0](https://github.com/CloudCompile/remote-desktop/milestones/5) | [#5](https://github.com/CloudCompile/remote-desktop/issues/5) | 16 | ✅ Shipped 2026-11-13 |
| **v2.5.0** | Audio & Media | 2027-06-13 | [v2.5.0](https://github.com/CloudCompile/remote-desktop/milestones/6) | [#6](https://github.com/CloudCompile/remote-desktop/issues/6) | 15 | ✅ Shipped 2026-11-13 |
| **v2.6.0** | Security & Multi-User | 2027-08-01 | [v2.6.0](https://github.com/CloudCompile/remote-desktop/milestones/7) | [#7](https://github.com/CloudCompile/remote-desktop/issues/7) | 19 | Planned |
| **v3.0.0** | Platform Expansion | 2027-10-03 | [v3.0.0](https://github.com/CloudCompile/remote-desktop/milestones/8) | [#8](https://github.com/CloudCompile/remote-desktop/issues/8) | 22 | Planned |

Icebox items are tracked in the [Icebox milestone](https://github.com/CloudCompile/remote-desktop/milestones/9) and have no target date.

## How the roadmap is managed

**Milestones are release scope.** An issue belongs to exactly one release milestone. A release ships when its milestone is empty of open P0/P1 work and the release gates pass.

**The project board is the execution view.** Fields:

| Field | Values | Meaning |
|---|---|---|
| Status | Todo · In Progress · Done | Workflow state; closing the issue moves it to Done. |
| Version | v2.0.1 … v3.0.0 · Icebox | The release milestone, mirrored for filtering. |
| Area | Server · Web · Mobile · Apps · Windows · Clipboard · Files · Audio · Security · Deploy · CI · Docs · Meta | Subsystem that owns the change. |
| Priority | P0 · P1 · P2 · P3 | P0 = data loss/security, P1 = required for the release, P2 = should, P3 = nice to have. |
| Size | XS · S · M · L · XL | XS < ½ day, S ≈ 1 day, M ≈ 2–3 days, L ≈ 1 week, XL = split before starting. |

**Labels** mirror type (`feature`, `bug`, `security`, `performance`, `refactor`, `testing`, `documentation`, `infrastructure`, `research`, `accessibility`), area, priority and size. `epic` marks the tracking issue for a release.

**Dependencies** are listed in each issue body. An issue should not start before its dependencies are merged unless the owner explicitly accepts the risk.

## Releases

### v2.0.1 — Stabilize & Ship

**Target:** 2026-11-25 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/1) · **Epic:** [#1](https://github.com/CloudCompile/remote-desktop/issues/1) · **Release:** [v2.0.1](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.0.1) ✅ shipped

**Theme.** Stop the bleeding: fix data-loss and resource-leak bugs, then put a CI, test and release baseline under the project.

**Goal.** Make the current feature set trustworthy enough to recommend. No new UI surface; every change is a fix, a guardrail or release plumbing.

**Exit criteria**

- [ ] CI runs lint, syntax, unit tests and the no-secrets check on every PR.
- [ ] Uploads are atomic, audio processes are reaped, and clipboard state survives a crash.
- [ ] A tagged release can be produced without hand editing a changelog.
- [ ] Known limitations are documented instead of discovered in production.

**Scope (12 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#10](https://github.com/CloudCompile/remote-desktop/issues/10) | Handle WebSocket fragmentation, continuation frames and size limits | Server | P0 | M |
| [#15](https://github.com/CloudCompile/remote-desktop/issues/15) | Make file uploads atomic and clean up partial files | Files | P0 | M |
| [#14](https://github.com/CloudCompile/remote-desktop/issues/14) | Reap ffmpeg audio processes when clients disconnect | Audio | P0 | S |
| [#11](https://github.com/CloudCompile/remote-desktop/issues/11) | Add a pytest harness and tests for control.py helpers | Server | P1 | M |
| [#12](https://github.com/CloudCompile/remote-desktop/issues/12) | Automate tag-to-release with changelog validation | CI | P1 | M |
| [#18](https://github.com/CloudCompile/remote-desktop/issues/18) | Expand /api/health into a useful liveness and version endpoint | Server | P1 | S |
| [#13](https://github.com/CloudCompile/remote-desktop/issues/13) | Expand CI with shellcheck, ruff, JS syntax and docs link checks | CI | P1 | M |
| [#16](https://github.com/CloudCompile/remote-desktop/issues/16) | Fix rate-limit state growth and trusted-proxy client IPs | Security | P1 | M |
| [#17](https://github.com/CloudCompile/remote-desktop/issues/17) | Make clipboard history persistence crash-safe | Clipboard | P1 | S |
| [#19](https://github.com/CloudCompile/remote-desktop/issues/19) | Document supported versions, upgrade and rollback | Docs | P2 | S |
| [#21](https://github.com/CloudCompile/remote-desktop/issues/21) | Publish known limitations and data-loss warnings | Docs | P2 | XS |
| [#20](https://github.com/CloudCompile/remote-desktop/issues/20) | Record vendored noVNC and pako provenance with checksums | Security | P2 | S |

### v2.1.0 — Architecture & Reliability

**Target:** 2026-12-20 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/2) · **Epic:** [#2](https://github.com/CloudCompile/remote-desktop/issues/2) · **Release:** [v2.1.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.1.0) ✅ shipped

**Theme.** Turn one large file into a maintainable single-process application with observable failure modes.

**Goal.** A contributor can change one subsystem without reading 1,400 lines, and an operator can tell why a session is unhealthy from logs and a readiness probe.

**Exit criteria**

- [ ] control.py is a thin entry point over testable modules.
- [ ] Configuration is validated at startup and documented from one source of truth.
- [ ] VNC restarts and process shutdown no longer strand clients or children.
- [ ] Integration tests exercise HTTP, auth and the websocket proxy end to end.

**Scope (17 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#34](https://github.com/CloudCompile/remote-desktop/issues/34) | Add end-to-end integration tests with fake X and VNC | Server | P1 | L |
| [#40](https://github.com/CloudCompile/remote-desktop/issues/40) | Add readiness checks for X, VNC, PulseAudio and ffmpeg | Server | P1 | S |
| [#24](https://github.com/CloudCompile/remote-desktop/issues/24) | Add structured logging with request IDs and levels | Server | P1 | M |
| [#30](https://github.com/CloudCompile/remote-desktop/issues/30) | Add versioned backup and restore for runtime state | Server | P1 | M |
| [#29](https://github.com/CloudCompile/remote-desktop/issues/29) | Add WebSocket backpressure, frame caps and buffer reuse | Server | P1 | M |
| [#28](https://github.com/CloudCompile/remote-desktop/issues/28) | Bound connection threads and concurrent streams | Server | P1 | M |
| [#23](https://github.com/CloudCompile/remote-desktop/issues/23) | Centralize configuration with validation and generated docs | Server | P1 | M |
| [#32](https://github.com/CloudCompile/remote-desktop/issues/32) | Fix session.sh PID handling and orphaned Xvnc processes | Deploy | P1 | M |
| [#33](https://github.com/CloudCompile/remote-desktop/issues/33) | Harden systemd units with sandboxing and resource limits | Deploy | P1 | M |
| [#25](https://github.com/CloudCompile/remote-desktop/issues/25) | Implement graceful shutdown with child reaping | Server | P1 | M |
| [#39](https://github.com/CloudCompile/remote-desktop/issues/39) | Reconnect the VNC proxy transparently when Xvnc restarts | Server | P1 | L |
| [#22](https://github.com/CloudCompile/remote-desktop/issues/22) | Split control.py into testable modules behind one entry point | Server | P1 | XL |
| [#35](https://github.com/CloudCompile/remote-desktop/issues/35) | Add coverage gate and Python version matrix | CI | P2 | S |
| [#38](https://github.com/CloudCompile/remote-desktop/issues/38) | Adopt ruff format and pre-commit hooks | CI | P2 | S |
| [#37](https://github.com/CloudCompile/remote-desktop/issues/37) | Benchmark HTTP, websocket and audio overhead | Server | P2 | M |
| [#36](https://github.com/CloudCompile/remote-desktop/issues/36) | Create an ADR log with the first architecture decisions | Docs | P2 | S |
| [#31](https://github.com/CloudCompile/remote-desktop/issues/31) | Make display geometry and DPI configurable and persistent | Server | P2 | M |

### v2.2.0 — Touch-First UX 2.0

**Target:** 2027-02-07 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/3) · **Epic:** [#3](https://github.com/CloudCompile/remote-desktop/issues/3) · **Release:** [v2.2.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.2.0) ✅ shipped

**Theme.** From 'usable on a phone' to 'designed for a phone': direct touch, zoom, PWA install, accessibility and a shell that adapts to the device.

**Goal.** A first-time phone or tablet user can connect, navigate, type and recover from a dropped connection without reading documentation.

**Exit criteria**

- [ ] Users choose trackpad or direct-touch mode and both are first-class.
- [ ] Pinch/pan, viewport modes and orientation changes do not fight noVNC.
- [ ] The shell is installable, themeable, localized and screen-reader navigable.
- [ ] iOS Safari and Android Chrome pass the documented gesture matrix.

**Scope (18 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#41](https://github.com/CloudCompile/remote-desktop/issues/41) | Add a direct-touch mode with absolute taps | Mobile | P1 | L |
| [#50](https://github.com/CloudCompile/remote-desktop/issues/50) | Add connection quality indicator and reconnect diagnostics | Mobile | P1 | M |
| [#43](https://github.com/CloudCompile/remote-desktop/issues/43) | Add fit, fill, 1:1 and remote-resize viewport modes | Web | P1 | M |
| [#42](https://github.com/CloudCompile/remote-desktop/issues/42) | Add pinch-to-zoom, pan and zoom reset | Mobile | P1 | L |
| [#53](https://github.com/CloudCompile/remote-desktop/issues/53) | Audit and fix accessibility of the mobile shell | Web | P1 | L |
| [#58](https://github.com/CloudCompile/remote-desktop/issues/58) | Correct safe-area and virtual-keyboard insets | Mobile | P1 | S |
| [#57](https://github.com/CloudCompile/remote-desktop/issues/57) | Fix iOS Safari pointer and gesture conflicts | Mobile | P1 | M |
| [#47](https://github.com/CloudCompile/remote-desktop/issues/47) | Improve IME composition and the mobile keyboard accessory | Mobile | P1 | L |
| [#49](https://github.com/CloudCompile/remote-desktop/issues/49) | Ship an installable PWA with offline shell | Web | P1 | L |
| [#51](https://github.com/CloudCompile/remote-desktop/issues/51) | Add dark, light, auto and high-contrast themes | Web | P2 | M |
| [#46](https://github.com/CloudCompile/remote-desktop/issues/46) | Add gesture customization and sensitivity controls | Mobile | P2 | M |
| [#44](https://github.com/CloudCompile/remote-desktop/issues/44) | Add three-finger swipe actions for app switching | Mobile | P2 | M |
| [#54](https://github.com/CloudCompile/remote-desktop/issues/54) | Localize the custom mobile shell | Web | P2 | L |
| [#48](https://github.com/CloudCompile/remote-desktop/issues/48) | Make the extra-keys panel user-editable | Mobile | P2 | M |
| [#52](https://github.com/CloudCompile/remote-desktop/issues/52) | Support orientation lock and a tablet landscape layout | Mobile | P2 | M |
| [#56](https://github.com/CloudCompile/remote-desktop/issues/56) | Add a first-run tour and contextual hints | Mobile | P3 | S |
| [#45](https://github.com/CloudCompile/remote-desktop/issues/45) | Add optional haptic feedback for gestures and clicks | Mobile | P3 | S |
| [#68](https://github.com/CloudCompile/remote-desktop/issues/68) | Let users reorder and hide toolbar actions | Mobile | P3 | S |

### v2.3.0 — Apps, Windows & Clipboard

**Target:** 2027-03-21 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/4) · **Epic:** [#4](https://github.com/CloudCompile/remote-desktop/issues/4) · **Release:** [v2.3.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.3.0) ✅ shipped

**Theme.** Make the remote desktop a usable workspace: switch windows, manage running apps, install software and move richer clipboard content.

**Goal.** A user can run a full session without dropping to a terminal to find a window, install an app or move an image between devices.

**Exit criteria**

- [ ] Window switcher and window actions cover the common WM workflows.
- [ ] Flatpak and Snap apps appear and focus correctly alongside .desktop entries.
- [ ] The app store supports install, update, remove and progress.
- [ ] Clipboard history handles text, images and opt-in device sync.

**Scope (18 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#59](https://github.com/CloudCompile/remote-desktop/issues/59) | Add a window switcher with live thumbnails | Windows | P1 | L |
| [#66](https://github.com/CloudCompile/remote-desktop/issues/66) | Add app store uninstall, update, queue and live logs | Apps | P1 | L |
| [#70](https://github.com/CloudCompile/remote-desktop/issues/70) | Add clipboard pinning, search and tags | Clipboard | P1 | M |
| [#60](https://github.com/CloudCompile/remote-desktop/issues/60) | Add close, minimize, maximize and move window actions | Windows | P1 | M |
| [#72](https://github.com/CloudCompile/remote-desktop/issues/72) | Add opt-in phone-to-desktop clipboard sync | Clipboard | P1 | L |
| [#65](https://github.com/CloudCompile/remote-desktop/issues/65) | Discover and launch Flatpak and Snap applications | Apps | P1 | L |
| [#73](https://github.com/CloudCompile/remote-desktop/issues/73) | Fix focus matching for Flatpak, Snap and native Wayland apps | Apps | P1 | M |
| [#71](https://github.com/CloudCompile/remote-desktop/issues/71) | Support images and file URIs in clipboard history | Clipboard | P1 | L |
| [#75](https://github.com/CloudCompile/remote-desktop/issues/75) | Surface app launch failures with remediation | Apps | P1 | S |
| [#67](https://github.com/CloudCompile/remote-desktop/issues/67) | Abstract the app store across apt, dnf and pacman | Apps | P2 | L |
| [#74](https://github.com/CloudCompile/remote-desktop/issues/74) | Add paste-as-keystrokes fallback | Clipboard | P2 | S |
| [#64](https://github.com/CloudCompile/remote-desktop/issues/64) | Add pin folders and drag reordering | Apps | P2 | M |
| [#61](https://github.com/CloudCompile/remote-desktop/issues/61) | Add snap and tiling layout presets | Windows | P2 | M |
| [#77](https://github.com/CloudCompile/remote-desktop/issues/77) | Cache app discovery with ETag and pagination | Apps | P2 | S |
| [#62](https://github.com/CloudCompile/remote-desktop/issues/62) | Rank recent and frequent apps in the launcher | Apps | P2 | S |
| [#76](https://github.com/CloudCompile/remote-desktop/issues/76) | Remember per-app window geometry | Windows | P2 | M |
| [#63](https://github.com/CloudCompile/remote-desktop/issues/63) | Support custom app entries and launch arguments | Apps | P2 | M |
| [#69](https://github.com/CloudCompile/remote-desktop/issues/69) | Add curated app collections and screenshots | Apps | P3 | M |

### v2.4.0 — Files & Transfer

**Target:** 2027-05-02 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/5) · **Epic:** [#5](https://github.com/CloudCompile/remote-desktop/issues/5) · **Release:** [v2.4.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.4.0) ✅ shipped

**Theme.** Replace the one-folder upload toy with a safe, resumable, mobile-friendly file workspace.

**Goal.** Moving a large file from a phone to the desktop, or a screenshot back to the phone, is reliable on a flaky connection.

**Exit criteria**

- [ ] Large and interrupted uploads resume instead of restarting.
- [ ] The file manager supports folders, rename, move, copy, search and preview.
- [ ] Path containment is symlink- and TOCTOU-safe and covered by tests.
- [ ] Transfer state survives a reload and reports actionable errors.

**Scope (16 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#91](https://github.com/CloudCompile/remote-desktop/issues/91) | Harden path containment against symlinks and TOCTOU | Files | P0 | M |
| [#80](https://github.com/CloudCompile/remote-desktop/issues/80) | Add drag-and-drop and multi-file upload with progress | Files | P1 | L |
| [#79](https://github.com/CloudCompile/remote-desktop/issues/79) | Build a full file manager with folders and operations | Files | P1 | XL |
| [#86](https://github.com/CloudCompile/remote-desktop/issues/86) | Fix Unicode normalization and filename collisions | Files | P1 | S |
| [#78](https://github.com/CloudCompile/remote-desktop/issues/78) | Implement chunked and resumable uploads | Files | P1 | L |
| [#84](https://github.com/CloudCompile/remote-desktop/issues/84) | Support multiple shared roots with per-root permissions | Files | P1 | M |
| [#83](https://github.com/CloudCompile/remote-desktop/issues/83) | Support range requests, resume and download progress | Files | P1 | M |
| [#88](https://github.com/CloudCompile/remote-desktop/issues/88) | Add a persistent transfer queue with pause and retry | Files | P2 | L |
| [#87](https://github.com/CloudCompile/remote-desktop/issues/87) | Add camera capture and mobile share-target upload | Mobile | P2 | M |
| [#81](https://github.com/CloudCompile/remote-desktop/issues/81) | Add file search, sorting, filtering and bulk selection | Files | P2 | M |
| [#82](https://github.com/CloudCompile/remote-desktop/issues/82) | Add image, PDF and video previews with thumbnails | Files | P2 | L |
| [#93](https://github.com/CloudCompile/remote-desktop/issues/93) | Document files, storage layout and backup | Docs | P2 | S |
| [#89](https://github.com/CloudCompile/remote-desktop/issues/89) | Show free space and enforce upload quotas | Files | P2 | S |
| [#92](https://github.com/CloudCompile/remote-desktop/issues/92) | Add a transfer and file-operation audit log | Files | P3 | S |
| [#85](https://github.com/CloudCompile/remote-desktop/issues/85) | Add archive creation and extraction | Files | P3 | M |
| [#90](https://github.com/CloudCompile/remote-desktop/issues/90) | Add permission and executable-bit controls | Files | P3 | M |

### v2.5.0 — Audio & Media

**Target:** 2027-06-13 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/6) · **Epic:** [#6](https://github.com/CloudCompile/remote-desktop/issues/6) · **Release:** [v2.5.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.5.0) ✅ shipped

**Theme.** Audio becomes controllable, diagnosable and usable in both directions, with a path toward WebRTC media.

**Goal.** Users can pick a sink, set volume, talk into the desktop from the phone, and recover audio after network or PulseAudio changes.

**Exit criteria**

- [ ] Sink selection, volume, mute and per-app volume work from the UI.
- [ ] Microphone streaming is usable with push-to-talk and clear consent.
- [ ] Codec/bitrate selection adapts to network conditions with PCM fallback.
- [ ] Audio recovers from backgrounding, reconnects and PulseAudio restarts.

**Scope (15 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#96](https://github.com/CloudCompile/remote-desktop/issues/96) | Add audio quality selection and adaptive bitrate | Audio | P1 | M |
| [#94](https://github.com/CloudCompile/remote-desktop/issues/94) | Add audio sink selection with volume and mute | Audio | P1 | L |
| [#100](https://github.com/CloudCompile/remote-desktop/issues/100) | Detect codec support and provide a PCM fallback UI | Audio | P1 | M |
| [#99](https://github.com/CloudCompile/remote-desktop/issues/99) | Improve jitter buffering and drift recovery | Audio | P1 | M |
| [#124](https://github.com/CloudCompile/remote-desktop/issues/124) | Recover audio across visibility changes and reconnects | Audio | P1 | M |
| [#97](https://github.com/CloudCompile/remote-desktop/issues/97) | Add desktop-browser microphone and webcam input | Audio | P1 | L |
| [#101](https://github.com/CloudCompile/remote-desktop/issues/101) | Add a media keys panel and shortcuts | Mobile | P2 | S |
| [#98](https://github.com/CloudCompile/remote-desktop/issues/98) | Add an audio-only low-bandwidth mode | Audio | P2 | S |
| [#95](https://github.com/CloudCompile/remote-desktop/issues/95) | Add per-application volume and mute | Audio | P2 | M |
| [#106](https://github.com/CloudCompile/remote-desktop/issues/106) | Build an audio diagnostics page | Audio | P2 | S |
| [#107](https://github.com/CloudCompile/remote-desktop/issues/107) | Publish an audio troubleshooting matrix | Docs | P2 | S |
| [#102](https://github.com/CloudCompile/remote-desktop/issues/102) | Record desktop audio to a downloadable file | Audio | P2 | M |
| [#104](https://github.com/CloudCompile/remote-desktop/issues/104) | Research WebRTC as the media transport | Audio | P2 | L |
| [#105](https://github.com/CloudCompile/remote-desktop/issues/105) | Share one ffmpeg capture across multiple listeners | Audio | P2 | M |
| [#103](https://github.com/CloudCompile/remote-desktop/issues/103) | Add an audio level meter | Audio | P3 | S |

### v2.6.0 — Security & Multi-User

**Target:** 2027-08-01 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/7) · **Epic:** [#7](https://github.com/CloudCompile/remote-desktop/issues/7) · **Draft release:** [v2.6.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v2.6.0)

**Theme.** Move from one trusted user with a password to explicit accounts, roles, second factors and an audit trail.

**Goal.** A small team or household can share a host safely, and an operator can answer who did what, when, and revoke access.

**Exit criteria**

- [ ] TOTP and passkeys are supported alongside passwords.
- [ ] Multiple users have isolated sessions, files and clipboards.
- [ ] State-changing endpoints are CSRF-protected and responses carry a strict CSP.
- [ ] Auth, app installs, file changes and admin actions are audited and revocable.

**Scope (19 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#116](https://github.com/CloudCompile/remote-desktop/issues/116) | Add CSRF protection and strict origin checks | Security | P0 | M |
| [#113](https://github.com/CloudCompile/remote-desktop/issues/113) | Add a tamper-evident audit log | Security | P1 | M |
| [#128](https://github.com/CloudCompile/remote-desktop/issues/128) | Add CodeQL, Dependabot and SBOM generation | CI | P1 | M |
| [#117](https://github.com/CloudCompile/remote-desktop/issues/117) | Add CSP, security headers and vendored-script integrity | Security | P1 | M |
| [#111](https://github.com/CloudCompile/remote-desktop/issues/111) | Add multiple user accounts with isolated sessions | Server | P1 | XL |
| [#114](https://github.com/CloudCompile/remote-desktop/issues/114) | Add session and device management with revoke | Security | P1 | M |
| [#109](https://github.com/CloudCompile/remote-desktop/issues/109) | Add TOTP two-factor authentication with recovery codes | Security | P1 | L |
| [#112](https://github.com/CloudCompile/remote-desktop/issues/112) | Add viewer, operator and admin roles | Security | P1 | L |
| [#125](https://github.com/CloudCompile/remote-desktop/issues/125) | Build a security regression test suite | Security | P1 | M |
| [#126](https://github.com/CloudCompile/remote-desktop/issues/126) | Document the threat model and complete a STRIDE review | Security | P1 | M |
| [#119](https://github.com/CloudCompile/remote-desktop/issues/119) | Harden cookies with Secure, __Host- and proxy awareness | Security | P1 | S |
| [#120](https://github.com/CloudCompile/remote-desktop/issues/120) | Isolate file roots and clipboards per user | Security | P1 | L |
| [#115](https://github.com/CloudCompile/remote-desktop/issues/115) | Persist rate limiting and integrate with fail2ban | Security | P1 | M |
| [#118](https://github.com/CloudCompile/remote-desktop/issues/118) | Rotate the password hash and signing keys without downtime | Security | P1 | M |
| [#123](https://github.com/CloudCompile/remote-desktop/issues/123) | Add login notifications and suspicious-activity alerts | Security | P2 | M |
| [#122](https://github.com/CloudCompile/remote-desktop/issues/122) | Add optional VNC authentication and TLS in depth | Security | P2 | M |
| [#110](https://github.com/CloudCompile/remote-desktop/issues/110) | Add WebAuthn and passkey login | Security | P2 | L |
| [#127](https://github.com/CloudCompile/remote-desktop/issues/127) | Refresh the vulnerability disclosure workflow and security.txt | Security | P2 | S |
| [#121](https://github.com/CloudCompile/remote-desktop/issues/121) | Require confirmation for sensitive actions | Security | P2 | S |

### v3.0.0 — Platform Expansion

**Target:** 2027-10-03 · **Milestone:** [view](https://github.com/CloudCompile/remote-desktop/milestones/8) · **Epic:** [#8](https://github.com/CloudCompile/remote-desktop/issues/8) · **Draft release:** [v3.0.0](https://github.com/CloudCompile/remote-desktop/releases/tag/v3.0.0)

**Theme.** Break the Linux/X11 + bespoke-host assumption: Wayland, other distributions, containers, multiple displays and an extension surface.

**Goal.** The project can be installed repeatably on more hosts and extended without forking control.py, with a stable API contract for the next major cycle.

**Exit criteria**

- [ ] Wayland and X11 sessions both work through explicit backends.
- [ ] An installer and container image cover the supported distributions.
- [ ] Multi-display and per-client DPI are supported.
- [ ] The plugin API and v3 API deprecation policy are published.

**Scope (22 issues)**

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#131](https://github.com/CloudCompile/remote-desktop/issues/131) | Abstract clipboard access across X11 and Wayland | Server | P1 | M |
| [#130](https://github.com/CloudCompile/remote-desktop/issues/130) | Abstract input across xdotool, ydotool and libei | Server | P1 | L |
| [#150](https://github.com/CloudCompile/remote-desktop/issues/150) | Add load and soak tests with a performance budget | CI | P1 | L |
| [#135](https://github.com/CloudCompile/remote-desktop/issues/135) | Add multi-display and virtual monitor management | Server | P1 | L |
| [#134](https://github.com/CloudCompile/remote-desktop/issues/134) | Add per-client resolution and DPI scaling | Server | P1 | L |
| [#145](https://github.com/CloudCompile/remote-desktop/issues/145) | Add versioned state schema and migration tooling | Server | P1 | M |
| [#129](https://github.com/CloudCompile/remote-desktop/issues/129) | Add Wayland session support | Server | P1 | XL |
| [#149](https://github.com/CloudCompile/remote-desktop/issues/149) | Define the v3 API stability and deprecation policy | Docs | P1 | S |
| [#148](https://github.com/CloudCompile/remote-desktop/issues/148) | Ship a one-line installer and container image | Deploy | P1 | L |
| [#132](https://github.com/CloudCompile/remote-desktop/issues/132) | Support Fedora, Arch and openSUSE packages | Deploy | P1 | XL |
| [#144](https://github.com/CloudCompile/remote-desktop/issues/144) | Add Prometheus and OpenTelemetry metrics | Server | P2 | M |
| [#140](https://github.com/CloudCompile/remote-desktop/issues/140) | Add session recording and playback | Server | P2 | L |
| [#139](https://github.com/CloudCompile/remote-desktop/issues/139) | Add VAAPI/NVENC hardware encoding | Server | P2 | L |
| [#136](https://github.com/CloudCompile/remote-desktop/issues/136) | Design and ship a plugin/extension API | Server | P2 | XL |
| [#138](https://github.com/CloudCompile/remote-desktop/issues/138) | Evaluate a native Tauri or Capacitor shell | Mobile | P2 | L |
| [#151](https://github.com/CloudCompile/remote-desktop/issues/151) | Generate the API reference from the implementation | Docs | P2 | M |
| [#143](https://github.com/CloudCompile/remote-desktop/issues/143) | Localize server messages and negotiate locale | Server | P2 | M |
| [#137](https://github.com/CloudCompile/remote-desktop/issues/137) | Prototype WebRTC for framebuffer and audio transport | Server | P2 | XL |
| [#161](https://github.com/CloudCompile/remote-desktop/issues/161) | Publish a documentation site with screenshots and a demo | Docs | P2 | M |
| [#141](https://github.com/CloudCompile/remote-desktop/issues/141) | Support multiple desktops per host with a session picker | Server | P2 | XL |
| [#147](https://github.com/CloudCompile/remote-desktop/issues/147) | Automate roadmap and project synchronization | CI | P3 | M |
| [#142](https://github.com/CloudCompile/remote-desktop/issues/142) | Research a relay mode for NAT traversal | Server | P3 | L |

## Icebox

Unscheduled ideas live in the [Icebox milestone](https://github.com/CloudCompile/remote-desktop/milestones/9) and the project board. They are promoted to a release only with a design note and an explicit trade-off against scheduled work.

| Issue | Title | Area | Priority | Size |
|---|---|---|---|---|
| [#160](https://github.com/CloudCompile/remote-desktop/issues/160) | Add a TV and 10-foot UI | Web | P3 | L |
| [#162](https://github.com/CloudCompile/remote-desktop/issues/162) | Add switch-control accessibility | Mobile | P3 | L |
| [#155](https://github.com/CloudCompile/remote-desktop/issues/155) | Gamepad and controller passthrough | Mobile | P3 | M |
| [#159](https://github.com/CloudCompile/remote-desktop/issues/159) | Home-screen widgets and quick actions | Mobile | P3 | M |
| [#157](https://github.com/CloudCompile/remote-desktop/issues/157) | Integrated SSH terminal | Server | P3 | L |
| [#158](https://github.com/CloudCompile/remote-desktop/issues/158) | Multi-pointer collaboration | Server | P3 | XL |
| [#152](https://github.com/CloudCompile/remote-desktop/issues/152) | Push notifications for long-running tasks | Mobile | P3 | M |
| [#153](https://github.com/CloudCompile/remote-desktop/issues/153) | QR-code pairing for phone setup | Security | P3 | S |
| [#156](https://github.com/CloudCompile/remote-desktop/issues/156) | Remote printing from phone | Files | P3 | L |
| [#154](https://github.com/CloudCompile/remote-desktop/issues/154) | Voice input and dictation | Mobile | P3 | M |

## Cross-release dependencies

These links are the main sequencing constraints. They are also written into the individual issue bodies.

| Issue | Depends on | Dependency ships in |
|---|---|---|
| [#35](https://github.com/CloudCompile/remote-desktop/issues/35) Add coverage gate and Python version matrix | [#11](https://github.com/CloudCompile/remote-desktop/issues/11) Add a pytest harness and tests for control.py helpers | v2.0.1 |
| [#34](https://github.com/CloudCompile/remote-desktop/issues/34) Add end-to-end integration tests with fake X and VNC | [#11](https://github.com/CloudCompile/remote-desktop/issues/11) Add a pytest harness and tests for control.py helpers | v2.0.1 |
| [#38](https://github.com/CloudCompile/remote-desktop/issues/38) Adopt ruff format and pre-commit hooks | [#13](https://github.com/CloudCompile/remote-desktop/issues/13) Expand CI with shellcheck, ruff, JS syntax and docs link checks | v2.0.1 |
| [#40](https://github.com/CloudCompile/remote-desktop/issues/40) Add readiness checks for X, VNC, PulseAudio and ffmpeg | [#18](https://github.com/CloudCompile/remote-desktop/issues/18) Expand /api/health into a useful liveness and version endpoint | v2.0.1 |
| [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | [#11](https://github.com/CloudCompile/remote-desktop/issues/11) Add a pytest harness and tests for control.py helpers | v2.0.1 |
| [#39](https://github.com/CloudCompile/remote-desktop/issues/39) Reconnect the VNC proxy transparently when Xvnc restarts | [#10](https://github.com/CloudCompile/remote-desktop/issues/10) Handle WebSocket fragmentation, continuation frames and size limits | v2.0.1 |
| [#29](https://github.com/CloudCompile/remote-desktop/issues/29) Add WebSocket backpressure, frame caps and buffer reuse | [#10](https://github.com/CloudCompile/remote-desktop/issues/10) Handle WebSocket fragmentation, continuation frames and size limits | v2.0.1 |
| [#50](https://github.com/CloudCompile/remote-desktop/issues/50) Add connection quality indicator and reconnect diagnostics | [#40](https://github.com/CloudCompile/remote-desktop/issues/40) Add readiness checks for X, VNC, PulseAudio and ffmpeg | v2.1.0 |
| [#77](https://github.com/CloudCompile/remote-desktop/issues/77) Cache app discovery with ETag and pagination | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#70](https://github.com/CloudCompile/remote-desktop/issues/70) Add clipboard pinning, search and tags | [#17](https://github.com/CloudCompile/remote-desktop/issues/17) Make clipboard history persistence crash-safe | v2.0.1 |
| [#63](https://github.com/CloudCompile/remote-desktop/issues/63) Support custom app entries and launch arguments | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#65](https://github.com/CloudCompile/remote-desktop/issues/65) Discover and launch Flatpak and Snap applications | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#75](https://github.com/CloudCompile/remote-desktop/issues/75) Surface app launch failures with remediation | [#24](https://github.com/CloudCompile/remote-desktop/issues/24) Add structured logging with request IDs and levels | v2.1.0 |
| [#64](https://github.com/CloudCompile/remote-desktop/issues/64) Add pin folders and drag reordering | [#17](https://github.com/CloudCompile/remote-desktop/issues/17) Make clipboard history persistence crash-safe | v2.0.1 |
| [#67](https://github.com/CloudCompile/remote-desktop/issues/67) Abstract the app store across apt, dnf and pacman | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#87](https://github.com/CloudCompile/remote-desktop/issues/87) Add camera capture and mobile share-target upload | [#49](https://github.com/CloudCompile/remote-desktop/issues/49) Ship an installable PWA with offline shell | v2.2.0 |
| [#84](https://github.com/CloudCompile/remote-desktop/issues/84) Support multiple shared roots with per-root permissions | [#23](https://github.com/CloudCompile/remote-desktop/issues/23) Centralize configuration with validation and generated docs | v2.1.0 |
| [#78](https://github.com/CloudCompile/remote-desktop/issues/78) Implement chunked and resumable uploads | [#15](https://github.com/CloudCompile/remote-desktop/issues/15) Make file uploads atomic and clean up partial files | v2.0.1 |
| [#83](https://github.com/CloudCompile/remote-desktop/issues/83) Support range requests, resume and download progress | [#15](https://github.com/CloudCompile/remote-desktop/issues/15) Make file uploads atomic and clean up partial files | v2.0.1 |
| [#96](https://github.com/CloudCompile/remote-desktop/issues/96) Add audio quality selection and adaptive bitrate | [#14](https://github.com/CloudCompile/remote-desktop/issues/14) Reap ffmpeg audio processes when clients disconnect | v2.0.1 |
| [#105](https://github.com/CloudCompile/remote-desktop/issues/105) Share one ffmpeg capture across multiple listeners | [#14](https://github.com/CloudCompile/remote-desktop/issues/14) Reap ffmpeg audio processes when clients disconnect | v2.0.1 |
| [#124](https://github.com/CloudCompile/remote-desktop/issues/124) Recover audio across visibility changes and reconnects | [#14](https://github.com/CloudCompile/remote-desktop/issues/14) Reap ffmpeg audio processes when clients disconnect | v2.0.1 |
| [#99](https://github.com/CloudCompile/remote-desktop/issues/99) Improve jitter buffering and drift recovery | [#14](https://github.com/CloudCompile/remote-desktop/issues/14) Reap ffmpeg audio processes when clients disconnect | v2.0.1 |
| [#104](https://github.com/CloudCompile/remote-desktop/issues/104) Research WebRTC as the media transport | [#37](https://github.com/CloudCompile/remote-desktop/issues/37) Benchmark HTTP, websocket and audio overhead | v2.1.0 |
| [#94](https://github.com/CloudCompile/remote-desktop/issues/94) Add audio sink selection with volume and mute | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#117](https://github.com/CloudCompile/remote-desktop/issues/117) Add CSP, security headers and vendored-script integrity | [#20](https://github.com/CloudCompile/remote-desktop/issues/20) Record vendored noVNC and pako provenance with checksums | v2.0.1 |
| [#111](https://github.com/CloudCompile/remote-desktop/issues/111) Add multiple user accounts with isolated sessions | [#23](https://github.com/CloudCompile/remote-desktop/issues/23) Centralize configuration with validation and generated docs | v2.1.0 |
| [#111](https://github.com/CloudCompile/remote-desktop/issues/111) Add multiple user accounts with isolated sessions | [#30](https://github.com/CloudCompile/remote-desktop/issues/30) Add versioned backup and restore for runtime state | v2.1.0 |
| [#115](https://github.com/CloudCompile/remote-desktop/issues/115) Persist rate limiting and integrate with fail2ban | [#16](https://github.com/CloudCompile/remote-desktop/issues/16) Fix rate-limit state growth and trusted-proxy client IPs | v2.0.1 |
| [#115](https://github.com/CloudCompile/remote-desktop/issues/115) Persist rate limiting and integrate with fail2ban | [#24](https://github.com/CloudCompile/remote-desktop/issues/24) Add structured logging with request IDs and levels | v2.1.0 |
| [#119](https://github.com/CloudCompile/remote-desktop/issues/119) Harden cookies with Secure, __Host- and proxy awareness | [#23](https://github.com/CloudCompile/remote-desktop/issues/23) Centralize configuration with validation and generated docs | v2.1.0 |
| [#125](https://github.com/CloudCompile/remote-desktop/issues/125) Build a security regression test suite | [#34](https://github.com/CloudCompile/remote-desktop/issues/34) Add end-to-end integration tests with fake X and VNC | v2.1.0 |
| [#128](https://github.com/CloudCompile/remote-desktop/issues/128) Add CodeQL, Dependabot and SBOM generation | [#12](https://github.com/CloudCompile/remote-desktop/issues/12) Automate tag-to-release with changelog validation | v2.0.1 |
| [#128](https://github.com/CloudCompile/remote-desktop/issues/128) Add CodeQL, Dependabot and SBOM generation | [#20](https://github.com/CloudCompile/remote-desktop/issues/20) Record vendored noVNC and pako provenance with checksums | v2.0.1 |
| [#109](https://github.com/CloudCompile/remote-desktop/issues/109) Add TOTP two-factor authentication with recovery codes | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#120](https://github.com/CloudCompile/remote-desktop/issues/120) Isolate file roots and clipboards per user | [#84](https://github.com/CloudCompile/remote-desktop/issues/84) Support multiple shared roots with per-root permissions | v2.4.0 |
| [#120](https://github.com/CloudCompile/remote-desktop/issues/120) Isolate file roots and clipboards per user | [#70](https://github.com/CloudCompile/remote-desktop/issues/70) Add clipboard pinning, search and tags | v2.3.0 |
| [#122](https://github.com/CloudCompile/remote-desktop/issues/122) Add optional VNC authentication and TLS in depth | [#23](https://github.com/CloudCompile/remote-desktop/issues/23) Centralize configuration with validation and generated docs | v2.1.0 |
| [#151](https://github.com/CloudCompile/remote-desktop/issues/151) Generate the API reference from the implementation | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#131](https://github.com/CloudCompile/remote-desktop/issues/131) Abstract clipboard access across X11 and Wayland | [#71](https://github.com/CloudCompile/remote-desktop/issues/71) Support images and file URIs in clipboard history | v2.3.0 |
| [#131](https://github.com/CloudCompile/remote-desktop/issues/131) Abstract clipboard access across X11 and Wayland | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#132](https://github.com/CloudCompile/remote-desktop/issues/132) Support Fedora, Arch and openSUSE packages | [#67](https://github.com/CloudCompile/remote-desktop/issues/67) Abstract the app store across apt, dnf and pacman | v2.3.0 |
| [#161](https://github.com/CloudCompile/remote-desktop/issues/161) Publish a documentation site with screenshots and a demo | [#54](https://github.com/CloudCompile/remote-desktop/issues/54) Localize the custom mobile shell | v2.2.0 |
| [#161](https://github.com/CloudCompile/remote-desktop/issues/161) Publish a documentation site with screenshots and a demo | [#93](https://github.com/CloudCompile/remote-desktop/issues/93) Document files, storage layout and backup | v2.4.0 |
| [#134](https://github.com/CloudCompile/remote-desktop/issues/134) Add per-client resolution and DPI scaling | [#43](https://github.com/CloudCompile/remote-desktop/issues/43) Add fit, fill, 1:1 and remote-resize viewport modes | v2.2.0 |
| [#130](https://github.com/CloudCompile/remote-desktop/issues/130) Abstract input across xdotool, ydotool and libei | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#148](https://github.com/CloudCompile/remote-desktop/issues/148) Ship a one-line installer and container image | [#23](https://github.com/CloudCompile/remote-desktop/issues/23) Centralize configuration with validation and generated docs | v2.1.0 |
| [#148](https://github.com/CloudCompile/remote-desktop/issues/148) Ship a one-line installer and container image | [#33](https://github.com/CloudCompile/remote-desktop/issues/33) Harden systemd units with sandboxing and resource limits | v2.1.0 |
| [#150](https://github.com/CloudCompile/remote-desktop/issues/150) Add load and soak tests with a performance budget | [#37](https://github.com/CloudCompile/remote-desktop/issues/37) Benchmark HTTP, websocket and audio overhead | v2.1.0 |
| [#150](https://github.com/CloudCompile/remote-desktop/issues/150) Add load and soak tests with a performance budget | [#28](https://github.com/CloudCompile/remote-desktop/issues/28) Bound connection threads and concurrent streams | v2.1.0 |
| [#144](https://github.com/CloudCompile/remote-desktop/issues/144) Add Prometheus and OpenTelemetry metrics | [#24](https://github.com/CloudCompile/remote-desktop/issues/24) Add structured logging with request IDs and levels | v2.1.0 |
| [#144](https://github.com/CloudCompile/remote-desktop/issues/144) Add Prometheus and OpenTelemetry metrics | [#28](https://github.com/CloudCompile/remote-desktop/issues/28) Bound connection threads and concurrent streams | v2.1.0 |
| [#141](https://github.com/CloudCompile/remote-desktop/issues/141) Support multiple desktops per host with a session picker | [#111](https://github.com/CloudCompile/remote-desktop/issues/111) Add multiple user accounts with isolated sessions | v2.6.0 |
| [#135](https://github.com/CloudCompile/remote-desktop/issues/135) Add multi-display and virtual monitor management | [#31](https://github.com/CloudCompile/remote-desktop/issues/31) Make display geometry and DPI configurable and persistent | v2.1.0 |
| [#138](https://github.com/CloudCompile/remote-desktop/issues/138) Evaluate a native Tauri or Capacitor shell | [#49](https://github.com/CloudCompile/remote-desktop/issues/49) Ship an installable PWA with offline shell | v2.2.0 |
| [#138](https://github.com/CloudCompile/remote-desktop/issues/138) Evaluate a native Tauri or Capacitor shell | [#104](https://github.com/CloudCompile/remote-desktop/issues/104) Research WebRTC as the media transport | v2.5.0 |
| [#136](https://github.com/CloudCompile/remote-desktop/issues/136) Design and ship a plugin/extension API | [#22](https://github.com/CloudCompile/remote-desktop/issues/22) Split control.py into testable modules behind one entry point | v2.1.0 |
| [#136](https://github.com/CloudCompile/remote-desktop/issues/136) Design and ship a plugin/extension API | [#112](https://github.com/CloudCompile/remote-desktop/issues/112) Add viewer, operator and admin roles | v2.6.0 |
| [#147](https://github.com/CloudCompile/remote-desktop/issues/147) Automate roadmap and project synchronization | [#12](https://github.com/CloudCompile/remote-desktop/issues/12) Automate tag-to-release with changelog validation | v2.0.1 |
| [#143](https://github.com/CloudCompile/remote-desktop/issues/143) Localize server messages and negotiate locale | [#54](https://github.com/CloudCompile/remote-desktop/issues/54) Localize the custom mobile shell | v2.2.0 |
| [#140](https://github.com/CloudCompile/remote-desktop/issues/140) Add session recording and playback | [#79](https://github.com/CloudCompile/remote-desktop/issues/79) Build a full file manager with folders and operations | v2.4.0 |
| [#145](https://github.com/CloudCompile/remote-desktop/issues/145) Add versioned state schema and migration tooling | [#30](https://github.com/CloudCompile/remote-desktop/issues/30) Add versioned backup and restore for runtime state | v2.1.0 |
| [#145](https://github.com/CloudCompile/remote-desktop/issues/145) Add versioned state schema and migration tooling | [#111](https://github.com/CloudCompile/remote-desktop/issues/111) Add multiple user accounts with isolated sessions | v2.6.0 |
| [#137](https://github.com/CloudCompile/remote-desktop/issues/137) Prototype WebRTC for framebuffer and audio transport | [#104](https://github.com/CloudCompile/remote-desktop/issues/104) Research WebRTC as the media transport | v2.5.0 |

## Release gates

A version may be tagged only when all of the following are true:

- CI is green on `main` (Python syntax, lint, shellcheck, unit and integration tests, no secrets).
- The milestone has no open P0 or P1 issues; any remaining P2/P3 work has been moved out explicitly.
- `CHANGELOG.md` has a dated section for the version, following Keep a Changelog.
- The release workflow has created or updated the draft release with notes.
- Upgrade from the previous release and rollback have been exercised on a clean host.
- `docs/API.md`, `README.md` and the deployment docs match the release.
- Known limitations and security notes are updated.

The mechanics are documented in [docs/RELEASING.md](docs/RELEASING.md).

## What is explicitly not planned

- A hosted multi-tenant service or shared relay operated by the project.
- Windows or macOS host support in the 2.x/3.0 horizon.
- Direct-touch-first input replacing the trackpad model. Direct touch is an additional mode, not a replacement.
- A native mobile app before the PWA and WebRTC experiments have produced a decision.

## Changing the roadmap

1. Open an issue using the feature request or bug report template.
2. Pick the milestone whose theme fits; if none fit, propose an Icebox item.
3. Add it to the project board and set Version, Area, Priority and Size.
4. Update this file if the issue changes a release goal or exit criteria.
5. Link dependencies in the issue body rather than relying on prose.
