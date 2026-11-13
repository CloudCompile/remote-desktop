# HTTP API

Base URL: `http://127.0.0.1:8090` (put HTTPS in front for remote access).

Auth: login sets an `HttpOnly` cookie `rd_session`. Every endpoint below
requires it **except** `/api/health`, `GET /login` and `POST /api/login`.
JSON errors are `{"ok": false, "error": "..."}` with 4xx/5xx status.

## Public

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/health` | — | `{"ok":true,"time":<unix>}` liveness probe |
| GET | `/login` | — | Login page |
| POST | `/api/login` | `{"username","password"}` | Sets `rd_session`; 401 on failure, 429 when rate-limited |
| POST | `/api/logout` | — | Clears the cookie |

## Session / display

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/auth_check` | — | `{"ok":true}` if the cookie is valid |
| GET | `/api/windows` | — | Visible windows `[{id,class,name}]` |
| POST | `/api/fit` | — | Resize known app windows to the display |
| POST | `/api/resize` | `?w=&h=` | `xrandr --size` fallback (VNC SetDesktopSize is preferred by the UI) |

## Apps

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/apps` | — | Installed apps `[{id,name,desc,cmd,terminal,categories,wmclass}]` |
| POST | `/api/launch` | `{"id": "firefox.desktop"}` | Launch, or focus/restore an existing window |
| POST | `/api/open/<name>` | — | Legacy shortcuts: `discord`, `chrome`, `firefox`, `nautilus`, `terminal` |
| POST | `/api/paste` | `{"text": "..."}` | Type text into the focused window with `xdotool` |

## App store (Debian/Ubuntu)

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/store` | — | Curated catalog with `installed` flags |
| GET | `/api/store/search` | `?q=` | AppStream search, falls back to `apt-cache` |
| POST | `/api/store/install` | `{"pkg": "vlc"}` | Starts `sudo apt-get install -y` in the background |
| GET | `/api/store/status` | `?pkg=` | `{"installed":bool,"installing":bool}` |
| POST | `/api/store/launch` | `{"name": "vlc"}` | Launch a catalog entry by name |

## Pins

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/pins` | — | `{"pins": ["...desktop", ...]}` |
| POST | `/api/pins/toggle` | `{"id": "..."}` | Add/remove a pin, returns the new list |

## Clipboard

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/clipboard` | — | `{"clipboard","primary","text"}` |
| POST | `/api/clipboard/set` | `{"text": "..."}` | Set the X clipboard |
| GET | `/api/clipboard/history` | — | `{"items":[{id,preview,source,ts}]}` |
| GET | `/api/clipboard/item` | `?id=` | Full text of one history item |
| POST | `/api/clipboard/paste` | `{"id": "..."}` | Put a history item on the X clipboard |
| POST | `/api/clipboard/remove` | `{"id": "..."}` | Delete one item |
| POST | `/api/clipboard/clear` | — | Clear history |

## Files (`~/Downloads` by default)

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/files` | — | `{"files":[{name,size,mtime}],"dir":"..."}` |
| GET | `/api/download` | `?name=` | File download (`application/octet-stream`) |
| POST | `/api/upload` | `?name=`, raw body | Upload a file (2 GB cap) |
| POST | `/api/files/delete` | `{"name": "..."}` | Delete a file |

## Audio

| Method | Path | Description |
|---|---|---|
| GET | `/api/audio/status` | PulseAudio/sink status |
| GET | `/api/audio.opus` | Live Opus/WebM stream (`audio/webm; codecs="opus"`) |
| GET | `/api/audio/raw` | Live PCM s16le 44.1 kHz stereo stream |
| GET | `/api/audio/test` | Plays a 4 s 440 Hz test tone into the sink |

## Devices in (desktop browser clients)

The web UI shows these controls only on desktop browsers; the endpoints
accept any authenticated client. Mic in creates a `remote_mic` PulseAudio
source (a null sink `remote_mic_sink` remapped into a capture source),
fed with s16le / 48 kHz / mono PCM from websocket binary frames via
ffmpeg. Cam in needs the `v4l2loopback` module on the desktop and decodes
WebM (MediaRecorder) frames onto the first `/dev/video*` device — set
`REMOTE_CAM_DEVICE` to pick a specific one.

| Method | Path | Description |
|---|---|---|
| GET | `/api/mic` | WebSocket; binary frames = raw s16le 48 kHz mono PCM |
| GET | `/api/mic/status` | Virtual mic source status |
| GET | `/api/camera` | WebSocket; binary frames = WebM video chunks |
| GET | `/api/camera/status` | v4l2loopback/device status; 409 if already in use |

## Stats and icons

| Method | Path | Description |
|---|---|---|
| GET | `/api/stats` | `{cpu_pct,load,cores,mem_used,mem_total,disk_used,disk_total,uptime_s}` |
| GET | `/icon/<app-id>` | Resolved .desktop icon (PNG/SVG/JPEG) |

## Websocket

| Path | Description |
|---|---|
| `/websockify` | RFB over WebSocket, proxied to `VNC_HOST:VNC_PORT` |

## Example

```sh
curl -c jar -X POST localhost:8090/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"secret"}'
curl -b jar localhost:8090/api/apps | jq '.apps[0]'
```
