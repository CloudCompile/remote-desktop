# Known limitations

Honest limits of the current design. Each item notes where a fix is
planned; anything without a plan is a deliberate trade-off.

## Transport & sessions

- **Single user.** One password, one shared session. Someone you share the
  URL+password with can do anything you can. Multi-user isolation is
  planned for v2.6.0.
- **X11 only.** The desktop must be Xvnc/X11; Wayland hosts are out of
  scope until v3.0.0 (#129).
- **CSRF.** State-changing endpoints rely on SameSite=Lax cookies but do
  not yet send origin-checked tokens (#116, v2.6.0).
- **No CSRF-free static auth.** `/api/health` is public and unauthenticated
  by design; it returns version/uptime only, no host details.

## Files

- **Uploads are capped at 2 GB** and buffered through the server process.
- **A crash mid-upload no longer corrupts existing files** (uploads are
  atomic since v2.0.1), but a partial upload leaves a hidden `.upload-*.part`
  file that is cleaned up after 24 h.
- **~/Downloads is the only root.** Multiple shared roots with permissions
  are planned for v2.4.0 (#84).

## Audio & media

- **Desktop audio out only, no capture of the desktop mic.** Mic/webcam
  *input* works from desktop-browser clients (#97).
- **Camera-in requires v4l2loopback**, a kernel module; without it the UI
  explains what is missing.
- **Audio buffers can drift** on long sessions over lossy networks; jitter
  recovery improvements are planned (#99).
- **HTTPS is required for mic/camera capture** — browsers gate getUserMedia
  to secure origins. The bundled tunnel setups provide this.

## Security notes

- Passwords are stored as crypt SHA-512 in `.passhash` (mode 600). Logins
  are rate-limited per IP.
- Clipboard history (`.cliphistory.json`) and pins (`.pins.json`) contain
  user data — keep them private; both are written 0600.
- The VNC endpoint is localhost-only; auth is at the HTTP layer.

## Data-loss risks fixed in v2.0.1

- Uploads and clipboard-history writes are now atomic (temp file + rename).
- A corrupt clipboard-history file is quarantined, not fatal.
- ffmpeg children are reaped on disconnect and on server shutdown.
