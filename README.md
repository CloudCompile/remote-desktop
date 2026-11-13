# Remote Desktop

A browser-based remote desktop built for phones and tablets. One Python
process serves the UI, the control API, the VNC websocket, audio streaming,
clipboard sync and file transfer on a single port — no nginx, no websockify,
no node build step.

Point it at an existing X11 desktop (Xvnc + a lightweight WM), expose it
through a tunnel, open it on your phone, and use the whole desktop:
launch/focus apps, pinch-free trackpad gestures, hear desktop audio, copy
between phone and desktop, and upload/download files.

> **Roadmap:** eight planned releases through v3.0.0, each with a milestone,
> linked issues and a project board. See [ROADMAP.md](ROADMAP.md).
> Known limits are documented in
> [docs/KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md).

```
┌──────────────┐   HTTPS    ┌───────────────────────────────┐   RFB    ┌──────────────┐
│ phone/browser│ ─────────► │ control.py  (127.0.0.1:8090)  │ ───────► │ Xvnc :1      │
│  noVNC UI    │ ◄───────── │  static · API · /websockify   │ ◄─────── │ + openbox    │
└──────────────┘            │  audio · clipboard · files    │          └──────────────┘
                            └───────────────────────────────┘
                              ▲ Cloudflare Tunnel / Tailscale Funnel
```

## Features

- **One process, one port.** `control.py` serves `web/` (noVNC), the JSON
  control API, `/websockify` (a small RFC6455 → TCP proxy) and audio on
  `127.0.0.1:8090`. Every connection gets a thread, so a slow phone cannot
  stall the server.
- **Mobile-first UI.** Bottom toolbar within thumb reach (Apps / Store /
  Keys / Audio / Files / Full / More), top sheets instead of menus, 44–48px
  touch targets. The toolbar hides itself while you use the desktop and
  leaves a small **▲ Controls** handle to bring it back.
- **Trackpad touch layer.** The screen is a trackpad: taps click wherever
  the pointer ring is, one finger moves the pointer with acceleration,
  long-press and two-finger tap right-click, two-finger drag scrolls,
  three-finger tap middle-clicks, double-tap+drag drags windows.
- **Auto-reconnect.** The VNC session reconnects with exponential backoff
  (1s → 15s); no error page, no reload.
- **Screen wake lock** (toggleable) so the phone does not sleep mid-session.
- **App menu from the real system.** Every installed `.desktop` entry is
  discovered automatically (with icons), grouped by category. Launching an
  app that is already running focuses/restores its window instead of making
  a duplicate — including Chrome `--app` windows, which share a WM_CLASS.
- **App store.** Search AppStream/apt, install with `apt-get`, see live
  install status.
- **Pin favorites** — long-press a tile; pins are stored server-side.
- **Clipboard history.** Watches both X CLIPBOARD and PRIMARY selections,
  keeps the last 100 items across restarts, and lets you paste any entry
  back to the desktop or copy it to the phone.
- **Files.** Upload from the phone straight into `~/Downloads` (2 GB cap),
  list, download and delete files there.
- **Audio.** Desktop audio via PulseAudio → ffmpeg, streamed as Opus/WebM
  (~100 kbps, good on mobile data) or raw PCM for lowest latency.
- **Mic & webcam in (desktop browsers).** When you open the UI from a
  desktop PC, its microphone and webcam can be shared *into* the desktop:
  the mic becomes a `remote_mic` PulseAudio source and the webcam a
  v4l2loopback device that desktop apps can use. Controls are hidden on
  phones and tablets; camera-in needs the `v4l2loopback` kernel module.
- **Live system stats** (CPU, RAM, disk, load, uptime) in the More sheet.
- **Auth everywhere.** Password (crypt SHA-512) + signed session cookie on
  every endpoint except `/login`, `/api/login` and `/api/health`; login is
  rate-limited (5 attempts / 10 min / IP).

## Requirements

Tested on Ubuntu 24.04 with an existing X11 session on `:1`.

| Component | Used for |
|---|---|
| Python 3.10+ | server (stdlib only) |
| `xvnc`/TigerVNC, `openbox` | the desktop session |
| `xdotool` | window focus, clicking, typing |
| `xclip` | clipboard read/write |
| `ffmpeg`, `pulseaudio` | audio capture |
| `dpkg`, `appstreamcli`/`apt-cache`, `sudo` | optional app store |
| Chrome/Chromium | optional web-app windows (`webapps.sh`) |
| `v4l2loopback` (dkms) | optional webcam-in device (`sudo modprobe v4l2loopback`) |
| `cloudflared` **or** `tailscale` | optional public HTTPS access |

Install on Debian/Ubuntu:

```sh
sudo apt install python3 tigervnc-standalone-server openbox xdotool xclip \
                 ffmpeg pulseaudio appstream
```

## Quick start

```sh
git clone https://github.com/CloudCompile/remote-desktop.git
cd remote-desktop

# 1. Set a password (hash is read from .passhash; missing file = logins disabled)
openssl passwd -6 'choose-a-password' > .passhash

# 2. Start the X/VNC session (or use your own on :1 / 127.0.0.1:5901)
./session.sh            # Xvnc :1 + openbox + PulseAudio null sink (watchdog loop)

# 3. Start the web server
./start.sh              # http://127.0.0.1:8090
```

`start.sh` accepts a custom VNC target:

```sh
./start.sh 127.0.0.1:5902
VNC_PORT=5902 ./start.sh
```

Open `http://127.0.0.1:8090`, log in, and you are on the desktop.

## Deploy with systemd

`systemd/` contains ready units. Edit `User=`, `WorkingDirectory=`, paths
and `RD_USER=`, then:

```sh
sudo cp systemd/remote-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now remote-session remote-control
```

- `remote-session` — Xvnc `:1` + PulseAudio watchdog (`session.sh`)
- `remote-control` — the web server (`control.py`)
- `remote-tunnel` — optional Cloudflare quick tunnel

## Remote access

Never expose port 8090 directly: the VNC stream is plain TCP and the server
expects to sit behind HTTPS.

**Tailscale Funnel (stable URL, recommended)**

```sh
sudo tailscale up --hostname=remote
sudo tailscale funnel --bg 8090      # https://remote.<tailnet>.ts.net
```

Funnel serves only `*.ts.net` names. For a custom domain, use a Cloudflare
named tunnel with the domain's DNS on Cloudflare.

**Cloudflare quick tunnel (ephemeral URL)**

```sh
cloudflared tunnel --url http://127.0.0.1:8090
./tunnel-url.sh                      # prints the current URL
```

The quick-tunnel URL changes on every restart; `tunnel-url.sh` prefers the
Tailscale Funnel URL when present.

## Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `RD_USER` | `admin` | Login username |
| `RD_PASSFILE` | `./.passhash` | crypt(3) password hash file |
| `RD_DISPLAY` | `:1` | X display used for launched apps |
| `XAUTHORITY` | `~/.Xauthority` | X auth cookie |
| `XDG_RUNTIME_DIR` | `/run/user/<uid>` | PulseAudio runtime dir |
| `RD_FILES_DIR` | `~/Downloads` | Upload/download directory |
| `VNC_HOST` | `127.0.0.1` | VNC host |
| `VNC_PORT` | `5901` | VNC port |

Runtime state (not committed): `.pins.json`, `.cliphistory.json`,
`.passhash`, `__pycache__/`.

## Using it

### Apps

Open **Apps**. Tiles come from installed `.desktop` files; search and
category chips filter them. Tap to launch or focus. Long-press to pin.
Web-only services can be installed as Chrome app-windows with
`./webapps.sh` (idempotent; generates `.desktop` files and icons).

### Clipboard

Everything copied on the desktop (CLIPBOARD or PRIMARY) appears in
**More → Clipboard history**. Paste an entry to the desktop, copy it to the
phone, or delete it. The **Keys** sheet has Copy/Paste helpers that also
push through the mobile clipboard.

### Files

**Files** lists `~/Downloads`; upload from the phone (progress shown),
download to the phone, or delete. Filenames are sanitised and path
traversal is rejected.

### Audio

**Audio → Start** streams Opus by default; PCM is available for lower
latency. **Test** plays a 4-second tone into the null sink so you can check
the pipeline.

## API

All endpoints are JSON unless noted, and require the `rd_session` cookie
except the three public ones. See [`docs/API.md`](docs/API.md) for the full
reference.

```text
GET  /api/health                    public liveness probe
POST /api/login                     {username,password} -> Set-Cookie
GET  /api/apps                      installed .desktop apps
POST /api/launch                    {id} launch/focus an app
POST /api/fit                       fit windows to the display
GET  /api/windows                   visible window list
POST /api/resize?w=&h=              resize the X display
GET  /api/store/search?q=           package search
POST /api/store/install             {pkg}
GET  /api/clipboard/history         last 100 clips
POST /api/clipboard/paste           {id}
POST /api/upload?name=              raw body -> ~/Downloads
GET  /api/audio.opus                live audio stream
GET  /api/stats                     CPU/RAM/disk/load/uptime
GET  /websockify                    VNC websocket (same origin)
```

## Security model

- Intended for a **single trusted user** on a machine you control.
- The VNC server listens on localhost only and is reached through the
  authenticated `/websockify` proxy.
- The login cookie is HMAC-signed with a key derived from the password hash;
  changing the password invalidates all sessions.
- Keep `.passhash` private and run behind HTTPS (Tailscale/Cloudflare).
- In-memory rate limiting resets on restart; it is a speed bump, not a
  full WAF.

See [`SECURITY.md`](SECURITY.md) for reporting vulnerabilities.

## Limitations

- Linux/X11 only (uses `xdotool`, `xclip`, `.desktop`, PulseAudio).
- One desktop, one user, one password — no multi-user accounts.
- The app store assumes Debian/Ubuntu (`dpkg`/`apt`).
- Touch input is trackpad-style; there is no direct-touch mode.
- The vendored noVNC is a trimmed subset, not the full noVNC distribution.

## Repository layout

```text
control.py            server: static files, API, websocket proxy, audio
session.sh            Xvnc :1 + openbox + PulseAudio watchdog
start.sh              manual launcher for control.py
webapps.sh            generate Chrome app .desktop entries + icons
tunnel-url.sh         print the current public URL
web/                  frontend (noVNC core + custom mobile UI)
systemd/              service units
docs/                 architecture, API, deployment, troubleshooting
```

## License

Project code: [MIT](LICENSE). Vendored third-party code: noVNC
(MPL-2.0) and pako (MIT) — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
