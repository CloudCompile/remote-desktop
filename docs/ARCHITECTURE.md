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

## Server (`control.py` → `remote_desktop/` package)

Single process, standard library only. Since #22, `control.py` is a thin
entry point (`from remote_desktop.server import main`); the implementation
lives in the `remote_desktop/` package:

| Module | Owns |
|---|---|
| `config.py` | every `RD_*`/`VNC_*` setting: one schema, startup validation, generated docs ([CONFIGURATION.md](CONFIGURATION.md)) |
| `log.py` | structured logging: levels, request ids, text/JSON, rotation to `/tmp/remote-control.log` |
| `procs.py` | child-process registry (ffmpeg etc.) + subprocess helpers |
| `auth.py` | cookie tokens, crypt password check, login rate limiting |
| `http.py` | `BaseHandler` plumbing (JSON replies, request ids, body parsing) + static files |
| `server.py` | routes, bounded `ThreadingHTTPServer`, graceful shutdown, CLI |
| `ws.py` | RFC6455 primitives: frame reader/builder, upgrade handshake |
| `vncproxy.py` | `/websockify`: websocket → VNC TCP with backpressure and reconnect ([ADR-0003](adr/ADR-0003-websocket-tcp-proxy-for-vnc.md)) |
| `audio.py` | PulseAudio helpers, audio-out source discovery |
| `media_in.py` | device-in websockets (desktop mic, webcam) |
| `clipboard.py` | X clipboard access + persisted multi-item history |
| `files.py` | upload/download directory, path sanitising |
| `apps.py` | `.desktop` discovery, icons, launch-or-focus |
| `store.py` | apt app-store search/install |
| `windows.py` | xdotool window management, xrandr resize fallback |
| `stats.py` | system stats + public `/api/health` snapshot |
| `ready.py` | dependency readiness probes behind `/api/ready` |
| `state.py` | pins, display state, versioned backups/export/import ([ADR-0005](adr/ADR-0005-file-backed-state.md)) |

No module binds sockets, starts threads or touches X at import time;
`python3 control.py` (or `from remote_desktop.server import main`) is the
only way the server starts. Decisions behind this layout: [ADR-0001](adr/ADR-0001-single-process-one-port.md)
(single process, one port), [ADR-0002](adr/ADR-0002-stdlib-only.md) (stdlib-only).

| Area | Notes |
|---|---|
| HTTP | `ThreadingHTTPServer` (bounded, #28); each request/websocket/audio stream gets a thread |
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
- `core/` — vendored noVNC RFB/websock/display/decoders/input (MPL-2.0, [ADR-0004](adr/ADR-0004-vendored-novnc-client.md)).
- `app/` — vendored noVNC app utilities (only a subset is used).

### Scaling vs geometry (#31)

noVNC scales the remote framebuffer to the browser viewport locally
(`resize=scale`); the *remote* resolution only controls sharpness and
server-side window sizes. Set it deliberately with `RD_GEOMETRY`/`RD_DPI`
(session start), resize from the UI (persisted in
`~/.local/share/remote-desktop/display.json`), and prefer the UI's resize
over pinch-zoom so text stays crisp on high-DPI phones.

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
