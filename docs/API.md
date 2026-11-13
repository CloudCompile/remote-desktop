# HTTP API

Base URL: `http://127.0.0.1:8090` (put HTTPS in front for remote access).

Auth: login sets an `HttpOnly` cookie `rd_session`. Every endpoint below
requires it **except** `/api/health`, `GET /login` and `POST /api/login`.
JSON errors are `{"ok": false, "error": "..."}` with 4xx/5xx status.

## Public

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/health` | — | `{"ok":true,"time":<unix>}` liveness probe (dependency-free) |
| GET | `/api/ready` | — | Readiness (#40): 200/503 with per-dependency `{x,vnc,pulse,ffmpeg}` status |
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

## Backup / state (#30)

| Method | Path | Description |
|---|---|---|
| POST | `/api/backup` | Snapshot pins + clipboard history into the versioned backups dir |
| GET | `/api/backup` | List backups with their manifests |

Backups live in `RD_STATE_DIR/backups/` (newest 5 kept). CLI:
`control.py --export-state FILE.tar.gz` / `--import-state FILE.tar.gz`
move state between hosts; the password hash is never included.

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

## Files & transfer

Shared roots (#84): `RD_FILES_ROOTS=/data/projects:Projects:ro,/srv/media:Media:quota=1073741824`
(default: single root from `RD_FILES_DIR`). Every path is relative to a root;
`root` is the root id (`r0`, `r1`, …) or its label (case-insensitive). All
operations are fd-contained (#91): symlinks and traversal payloads cannot
escape a root. Limits: 2 GB per file, 8 MiB per chunk (sessions), and the
`RD_FILES_RESERVE` free-space check (#89) refuses uploads that would cross it
(HTTP 507).

| Method | Path | Body / query | Description |
|---|---|---|---|
| GET | `/api/files` | `?root=&path=&sort=name|size|mtime|type&dir=asc|desc&dirs_first=0|1` | Listing (#79/#81): `{ok, roots, root, root_label, readonly, path, entries:[{name,type,size,mtime,mode,owner,group}], dirs, files, disk:{free,total,used,reserve,quota,warn_pct}}` |
| GET | `/api/files/roots` | — | Roots + startup errors (#84) |
| GET | `/api/files/search` | `?root=&path=&q=&limit=` | Bounded recursive search (#81): `{matches:[entry+dir], scanned, truncated}` (caps: 4000 scanned / 200 results / 2 s) |
| GET | `/api/files/content` | `?root=&path=&inline=0|1`, `Range`, `If-Range` | Ranged download (#83): 200/206, `Accept-Ranges`, `ETag`; malformed/multi/unsatisfiable ranges → 416 + `Content-Range: bytes */size`; `inline=1` serves safe MIME types only (#82 — SVG/HTML are never inline) |
| GET | `/api/files/thumb` | `?root=&path=&size=` | Thumbnail (#82): small raster images stream the original; PDF/video/large images go through host tools into an mtime+size-keyed cache; 415 unsafe/unknown type, 501 no tool on host |
| GET | `/api/files/audit` | `?limit=` | Audit records, newest last (#92) |
| GET | `/api/files/transfers` | — | Transfer queue (#88): `{transfers:[{id,kind,name,path,size,offset,state,retries,pct}], aggregate}` |
| GET | `/api/files/upload/status` | `?session=` | One session's acknowledged offset (#78) |
| GET | `/api/download` | `?name=` | Legacy flat download (first root, `application/octet-stream`, now also range-capable) |
| POST | `/api/upload` | `?name=`, raw body | Legacy single-shot upload (atomic #15, 2 GB cap, reserve-checked #89) |
| POST | `/api/files/mkdir` | `{"root","path"}` | Create directory (and parents) |
| POST | `/api/files/rename` | `{"root","path","name"}` | Rename within its directory; NFC/case collisions → 409 (#86) |
| POST | `/api/files/move` | `{"root","paths":[],"dest","root2?","overwrite?"}` | Move (auto-renames on collision) |
| POST | `/api/files/copy` | `{"root","paths":[],"dest","root2?","overwrite?"}` | Copy (cross-root aware) |
| POST | `/api/files/delete` | `{"root","paths":[]}` (or legacy `{"name"}`) | Bulk delete; 200 all-deleted / 207 partial with per-item `failed` |
| POST | `/api/files/chmod` | `{"root","path","mode"}` | Octal mode; setuid/setgid → 403 (#90) |
| POST | `/api/files/archive` | `{"root","paths":[],"format":"zip|tar.gz|tar.xz","name"}` | Create archive (#85, `RD_ARCHIVE_CAP` cap) |
| POST | `/api/files/extract` | `{"root","path","dest"}` | Safe extraction (#85): zip-slip/symlink members refused or skipped, total size capped |
| POST | `/api/files/upload/init` | `{"root","path","name","size","sha256?","chunk_size?","session?"}` | Start or RESUME a session (#78) → `{session, offset, chunk_size, exists}`; resume returns the acknowledged offset |
| POST | `/api/files/upload/chunk` | `?session=&offset=`, raw body, `X-Chunk-Sha256` | One verified chunk; wrong checksum → 400 (offset unchanged, retry), paused → 409, wrong offset → 409 |
| POST | `/api/files/upload/complete` | `{"session"}` | Verify + atomic move into place; incomplete → 409, checksum → 400 |
| POST | `/api/files/upload/cancel` | `{"session"}` | Drop the session and its partial data |
| POST | `/api/files/transfers/pause` | `{"id"}` | Pause (acknowledged bytes kept) (#88) |
| POST | `/api/files/transfers/resume` | `{"id"}` | Resume |
| POST | `/api/files/transfers/cancel` | `{"id"}` | Cancel + clean partial data |
| POST | `/api/files/transfers/retry` | `{"id"}` | Retry from the acknowledged offset |
| POST | `/share` | multipart/form-data (`files`) | PWA share-target upload (#87): saves each part into the chosen root, 303 → `/` |

File-error responses are JSON `{"ok": false, "error": "<client-safe>"}` with
400 (invalid), 403 (read-only root, symlink, setuid), 404 (missing),
409 (exists / paused / incomplete / collision), 413 (too large),
503 (session cap), 507 (reserve/quota exceeded). Every mutating operation
writes one audit record (#92): `ts, action, user, ip, rid, root, path, dest,
size, result` — never file contents.

See [FILES.md](FILES.md) for the containment model, storage layout, quotas
and backup guidance.

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
| GET | `/api/stats` | `{cpu_pct,load,cores,mem_used,mem_total,disk_used,disk_total,uptime_s,conns,ws,geometry}` |
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
