# feat(web): v2.2.0 — Touch-First UX 2.0

Implements the full v2.2.0 mobile-shell scope (#3): all 18 milestone issues
that live in `web/index.html`, `web/touch.js` and new static assets. No
Python, systemd or vendored code is touched; no new server endpoints were
added (only existing `/api/health`, `/api/paste`, `/api/clipboard*` calls).

## Issues implemented

| Issue | What shipped |
|---|---|
| #41 | **Direct-touch mode.** Segmented Trackpad/Direct control in the More sheet plus a `Touch` toolbar toggle; mode persists per device (localStorage, default trackpad). Direct mode sends taps, long-press right-click and immediate press-and-drag at the touched pixel through the existing `rfb._sendMouse` path, with 1:1 mapping (no acceleration), a touch ripple at the finger, and a movement threshold before a drag starts (release after a drag never clicks). |
| #42 | **Pinch-to-zoom, pan, reset.** CSS transform zoom (1–5×) on noVNC's screen element with focal-point anchoring; two-finger spread zooms, fingers moving together scroll at zoom == 1 or pan when zoomed; one-finger pan while zoomed in direct mode; double-tap resets; pan is clamped so the framebuffer can never leave the screen; zoom resets on reconnect, orientation change and viewport-mode change; live percentage indicator + reset button in the Display section. |
| #43 | **Viewport modes.** Fit (previous contain behaviour) / Fill (cover, crops) / 1:1 (native pixels, unscaled, pan to reach off-screen areas) / Remote (sets `rfb.resizeSession = true` and requests a desktop matching the viewport proportions). Only Remote mode may resize the desktop; `matchDesktop` and noVNC's automatic resize are gated on it. Mode persists per device, is reapplied after reconnect, survives rotation without a reload, and the sheet shows fb size, effective on-screen scale and whether the image is stretched. |
| #44 | **Three-finger swipe.** Horizontal three-finger swipe sends Alt+Tab (keysym sequence), shows a transient "Alt+Tab" overlay, vibrates when haptics are on; vertical or cancelled gestures do nothing; the gesture cannot fire accidental clicks (tap detection requires no movement). Disable via the More-sheet toggle. |
| #45 | **Haptics.** `navigator.vibrate` ticks (8 ms) on tap-clicks and long-press, silent when the toggle is off (default off), unsupported (iOS), or `prefers-reduced-motion` is set. Persists per device. |
| #46 | **Gesture customization.** Sliders for pointer speed, scroll speed (px per wheel notch), tap tolerance and long-press delay; toggles for acceleration, scroll inversion, tap-to-click, double-tap-drag and three-finger swipe. All apply live without reconnecting, persist per device, and a reset restores documented defaults. |
| #47 | **IME composition.** The hidden keyboard textarea now suppresses key/input events while composing (`compositionstart/update`, `isComposing`) and types the committed text in one shot through `POST /api/paste` on `compositionend` (VNC-clipboard fallback when the API is unreachable). Enter joined the accessory row; sticky Ctrl/Alt/Shift/Win toggles now actually modify the next key. |
| #48 | **Editable extra keys.** The shortcut row is a user-editable list: add/remove, label + keysym sequence (e.g. `Ctrl+Alt+T`, `Escape`, `f`), reorder, restore defaults; validated against noVNC's keysym table with a readable error for invalid sequences; stored in localStorage merged over the defaults; survives reloads and reconnects. |
| #49 | **PWA.** `web/manifest.webmanifest` (standalone, `#161616`, `start_url /`, vendored noVNC icons for any + maskable) and `web/sw.js`: precached shell, cache-first for vendored assets (`/app/*`, `/core/*`, `/vendor/*`, `/include/*`), network-first for the shell docs (`/`, `/touch.js`, `/locales/*`, manifest) so deploys activate on the next load, offline navigation falls back to the cached `/`, and `/api/*` + `/websockify` are never intercepted. Registration guarded to secure origins; `beforeinstallprompt` powers an Install button with an iOS Add-to-Home-Screen hint. |
| #50 | **Connection quality.** `/api/health` pinged every 5 s while connected; a dot on the toolbar handle shows green <300 ms, amber <1 s, red worse or disconnected. A Connection section in the More sheet shows RTT, reconnect count (auto-reconnects since load), viewport, server version, last disconnect reason and the user agent — no cookie or credential material. |
| #51 | **Themes.** All shell colours moved to CSS custom properties; dark (default), light and high-contrast themes, plus Auto resolving `prefers-color-scheme`. A pre-paint head script applies the stored theme (no flash of the wrong theme), the `theme-color` meta follows, and the picker lives in the More sheet. The remote framebuffer is never tinted. |
| #52 | **Orientation.** Lock/unlock landscape via `screen.orientation.lock` with the rejection caught and reported inline (most browsers require fullscreen/install mode); landscape tablets get centered 560 px side sheets; viewport scale and touch mapping recompute on rotation without resizing the remote desktop (unless Remote mode is selected). |
| #53 | **Accessibility.** Accessible names on every toolbar button, icon-only control and dynamically created row button; stateful toggles expose `aria-pressed`; sheets are `role="dialog"` + `aria-modal` with titled headings; focus moves into an opened sheet, Tab/Shift+Tab are trapped inside, focus is restored on close; Escape closes; visible `:focus-visible` outlines; `prefers-reduced-motion` disables sheet/toolbar/ripple animations. |
| #54 | **Localization.** `web/locales/{en,de,es,fr}.json` (~85 strings: toolbar labels, sheet titles, common buttons, touch settings, tour) with a small i18n helper: `data-i18n` / `data-i18n-ph` / `data-i18n-aria` attributes on static strings, English fallback everywhere, picker in the More sheet (persisted), switching applies instantly without reload (touch settings rebuild), and `web/locales/README.md` documents how to contribute a language. |
| #56 | **First-run tour.** Three dismissible cards (trackpad model → gestures → controls/Apps sheet) with Skip on every step, shown once per device shortly after first load; "Replay intro tour" in the More sheet; no animations (reduced-motion friendly), localized, and it never blocks an existing session. |
| #57 | **iOS Safari fixes.** `gesturestart/gesturechange/gestureend` are rejected at the document level so two-finger pinch is handled by the app, never as page zoom; `touch-action: manipulation` on toolbar buttons and the handle; the canvas keeps `touch-action: none`; stuck buttons after OS gestures/app switches are cleared by the touch layer's `touchcancel`/blur/visibility panic (button-up is always sent). |
| #58 | **Insets.** New `--ikb` keyboard inset derived from `visualViewport.height` (ignoring URL-bar collapse), updated on visualViewport resize/scroll, focusin/focusout (after the keyboard animation) and orientation change; toolbar, handle, status pill, toast and the extra-keys bar lift above the keyboard; the canvas keeps the layout viewport so toggling the keyboard never offsets the pointer mapping; `env(safe-area-inset-*)` handling unchanged. |
| #68 | **Toolbar customization.** "Customize toolbar" in the More sheet: per-action show/hide checkbox plus reordering, reset action, persisted per device; long-pressing a toolbar button hides it with an undo toast; the More button can never be hidden so settings stay reachable; every configuration keeps ≥44 px targets (toolbar buttons tightened to 44 px min-width to fit eight actions on a 360 px screen). |

## Notes / limitations

- Python-side code, `systemd/`, `session.sh`, `tests/` and `CHANGELOG.md`
  were intentionally not touched.
- The service worker only registers on secure origins (HTTPS or localhost);
  plain-HTTP LAN deployments behave exactly as before.
- Python's `mimetypes` may serve the manifest as `application/octet-stream`;
  Chrome/Edge parse it regardless, Firefox may ignore the manifest until the
  server maps the type — same one-process constraint as before (no server
  changes allowed in this milestone).
- Dynamic status toasts remain English; the static shell, toolbar, sheets,
  touch settings and tour are fully localized (de/es/fr reviewed by hand).
- Physical-mouse wheel/click precision can drift while the zoom layer is
  active (noVNC's own event path doesn't know about the CSS transform); the
  touch paths used on phones/tablets compensate for it.
- Zoom is disabled in Remote-resize mode because noVNC measures the desktop
  size through the transformed element.

## Gate results

- `node --check web/touch.js web/app/*.js` — pass
- Inline module extracted from `web/index.html` (CI regex) + `node --check` — pass
- Every `$('id')` / `getElementById` referenced from JS resolves to an HTML id
  (or a documented runtime-created element) — pass
- `python3 scripts/check-links.py` — pass
- `web/manifest.webmanifest` parses as JSON; all four locale files have
  identical key sets — pass
- HTML tag balance check — pass
- Headless-Chrome smoke tests: shell boots with no console errors, theme
  applied pre-paint, toast hidden, install prompt captured; direct-touch tap
  simulation against a stubbed RFB maps to the correct canvas pixel
- `python3 -m pytest` — 30 passed (unchanged server suite)

Part of #3 · milestone **v2.2.0**.
