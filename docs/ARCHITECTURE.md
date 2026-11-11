# Architecture

```
browser (phone)                     host (Linux/X11)
┌──────────────────────┐            ┌─────────────────────────────────────────┐
│ web/index.html       │  HTTPS     │ control.py  (ThreadingHTTPServer :8090) │
│  ├─ core/rfb.js      │ ─────────► │  ├─ static: web/                        │
│  ├─ touch.js         │            │  ├─ /api/*: JSON control API            │
│  └─ mobile toolbar   │ ◄───────── │  ├─ /websockify: RFC6455 → TCP 5901     │
└──────────────────────┘   RFB over │  ├─ /api/audio*: ffmpeg → HTTP          │
          │              websocket  │  └─ /icon/<id>: .desktop icons          │
          │                         └───────────────┬─────────────────────────┘
          │                                         │ xdotool / xclip / xrandr
          │                                         ▼
          │                         ┌─────────────────────────────────────────┐
          │                         │ Xvnc :1 (127.0.0.1:5901)                │
          │                         │  └─ openbox + user apps                 │
          │                         │ PulseAudio null sink "remote_out"        │
          │                         └─────────────────────────────────────────┘
          └── tunnel (Tailscale Funnel / Cloudflare) terminates TLS
```

## Server (`control.py`)

Single Python file, standard library only.

| Area | Notes |
|---|---|
| HTTP | `ThreadingHTTPServer`; each request/websocket/audio stream gets a thread |
| Auth | `.passhash` (crypt SHA-512) → HMAC-SHA256 cookie `rd_session`, 7-day TTL, `HttpOnly`, `SameSite=Lax`; 5 attempts / 10 min / IP |
| Static | `web/`, path-traversal guarded; `/` redirects to `/login` |
| Websocket | Minimal RFC6455 implementation: handshake, frame parse/build, proxies bytes to the VNC TCP socket both ways |
| Apps | Scans `/usr/share/applications` and `~/.local/share/applications`, filters `NoDisplay`/`Hidden`, parses `Exec`, `StartupWMClass`, `--class=` |
| Window focus | `xdotool search --onlyvisible --class/--classname/--name`; prefers the WM_CLASS *instance* so Chrome's browser window is not confused with its `--app` windows |
| Audio | `ffmpeg -f pulse` on the monitor source; Opus/WebM or PCM, chunked HTTP |
| Clipboard | `xclip` for CLIPBOARD and PRIMARY; a watcher thread records changes into `.cliphistory.json` (100 items) |
| Files | `~/Downloads`, basename-only, `realpath` containment, 2 GB upload cap |
| Stats | `/proc/stat`, `/proc/meminfo`, `/proc/uptime`, `shutil.disk_usage` |

## Frontend (`web/`)

- `index.html` — custom mobile shell: login flow, connect overlay, status
  pill, bottom toolbar, top sheets, pointer/touch handling glue.
- `touch.js` — trackpad gesture layer. Keeps the cursor in remote
  framebuffer coordinates and converts to screen coordinates when calling
  noVNC's `_sendMouse`, so it survives zoom/resize.
- `core/` — vendored noVNC RFB/websock/display/decoders/input (MPL-2.0).
- `app/` — vendored noVNC app utilities (only a subset is used).

## Session (`session.sh`)

Watchdog loop:

1. If `:5901` is not listening, start `vncserver :1` (1280×800, localhost,
   no VNC password — auth is at the HTTP layer).
2. Every ~30 s ensure PulseAudio is running and the `remote_out` null sink
   exists; apps play into it and ffmpeg captures its monitor.

Restarting the VNC server is safe: browser clients reconnect automatically.

## Data flow for a tap

1. `touch.js` gets a `touchstart/touchend`; if it was a tap, it clicks at
   the cursor ring's remote coordinates.
2. `rfb._sendMouse(x, y, mask)` encodes an RFB PointerEvent.
3. `/websockify` proxies the frame to `Xvnc:5901`.
4. X delivers the click to the app under the cursor.
