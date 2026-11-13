# Files & transfer (v2.4.0)

Everything the file feature does: shared roots, the containment model,
transfers, quotas, permissions, storage layout and backup. Endpoint-level
detail lives in [API.md](API.md).

## Shared roots (#84)

```
RD_FILES_ROOTS="/data/projects:Projects:ro,/srv/media:Media:quota=1073741824"
                    └ path        └ label  └ flags      └ quota in bytes
```

- Format: comma-separated `PATH[:LABEL[:FLAGS]]`; flags: `ro` (read-only)
  and `quota=BYTES`. Unset/empty = one root from `RD_FILES_DIR`
  (`~/Downloads`) — the default install behaves exactly as before v2.4.0.
- Missing root directories are created at startup; invalid entries
  (relative paths, bad quota values) are reported at startup (`RD_FILES_ROOTS`
  lines in the log/stderr) and skipped; they also appear in
  `GET /api/files/roots` under `errors`.
- Read-only is enforced **server-side**: upload, mkdir, rename, move, copy,
  delete and chmod on a `ro` root answer 403. The UI only hides the buttons.
- Per-root quotas (#89) count bytes under the root (bounded walk, cached
  60 s) and refuse uploads that would exceed them with HTTP 507 and a
  message naming the configured limit.
- Free space: every listing carries `disk.free/total` and the configured
  `RD_FILES_RESERVE` (default 64 MiB). An upload that would push the
  filesystem below the reserve is refused (507) **before** any byte is
  written. The UI warns at 90% usage (`warn_pct`); `/api/stats` exposes
  host disk usage.

## Path containment model (#91)

The security-relevant rule: **no file operation can touch anything outside
a configured root.** The implementation is openat-style:

1. Names are validated *before* any filesystem call: no path separators, no
   `..` components, no null bytes, no control characters, no leading dots
   for new files, ≤ 255 bytes (#86).
2. The root directory is opened once per operation (`open`, `O_DIRECTORY`)
   and every path component is opened relative to the previous descriptor
   with `O_NOFOLLOW`. A symlink anywhere in the path — including one
   swapped in mid-request — is refused (`ELOOP`), never followed.
3. The final component is opened with `O_NOFOLLOW` as well, so there is no
   verify-then-use window: reads, writes, chmod and renames all operate on
   descriptors that were validated by the kernel at open time. Once open,
   an fd keeps referring to the same inode even if the directory is
   renamed underneath it.
4. Directory deletion walks descriptors only (scandir + openat + unlinkat);
   symlinks are unlinked, never traversed. Archive extraction validates
   every member path the same way (no absolute paths, no `..`, link
   members skipped, total uncompressed size capped — see below).
5. Writes go through temp files in the destination directory, fsync, then
   `rename` — a crash or full disk never leaves a truncated visible file
   (the v2.0.1 atomic-upload guarantee, kept for every path).

Unicode (#86): comparison uses NFC + casefold while the stored name is
preserved byte-for-byte. A name that differs from an existing entry only by
case or normalisation (e.g. `café.txt` NFC vs NFD, `Cafe` vs `cafe`) is
refused with 409 instead of silently overwriting. Downloads send an
RFC 5987 `filename*=UTF-8''` header so Android/iOS keep a usable filename.

## Transfers (#78, #83, #88)

**Uploads** use chunked, resumable sessions:

```
client                              server
  │ POST /api/files/upload/init ────► creates STATE_DIR/files-sessions/<sid>/
  │◄── {session, offset, exists}      meta.json + part.bin
  │ POST /api/files/upload/chunk      append at offset, fsync,
  │   (raw bytes + X-Chunk-Sha256) ►  verify sha256 → ack {offset}
  │ POST /api/files/upload/complete   verify size + whole-file sha256,
  │◄── {path, size}                   copy → temp in destination → rename
```

- A dropped connection resumes with `init` + `session=` and gets the last
  **acknowledged** offset back (fsync'd bytes only) — a 1 GB upload
  interrupted at 60% continues near 60%.
- A corrupted chunk (checksum mismatch) is rejected with 400 and the
  offset unchanged, so the client retries exactly that chunk.
- Abandoned sessions (no update for 24 h) and finished jobs older than
  `RD_TRANSFER_RETENTION` are garbage-collected at startup and by a
  background sweep every 15 min.
- Session cap: 16 concurrent upload sessions (503 beyond).
- Duplicates: `init` answers `exists: true`; the client offers
  overwrite / rename / skip (#80).

**Downloads** support `Range`/`If-Range` (#83): `curl -C -` resumes,
over-long ends are clamped, malformed/multi/unsatisfiable ranges get 416
with `Content-Range: bytes */size`, and the stream never buffers the whole
file (1 MiB chunks from the open fd). The persistent queue (#88) exposes
list/pause/resume/cancel/retry for upload sessions; pause stops network
use without losing acknowledged bytes; retry continues from the
acknowledged offset without re-selecting the file.

## Previews (#82)

- Small raster images (≤ 8 MiB) stream inline directly.
- PDFs, videos and large images are thumbnail-ed by host tools
  (`pdftoppm`, ImageMagick `convert`, `ffmpeg` — whichever exists) run
  **argv-only, never a shell**, on a private temp copy of the source taken
  through the contained fd; uploaded content is never executed.
- Thumbnails are cached in the state dir keyed by
  `sha256(root, path, mtime, size, size)` — any change invalidates.
- `image/svg+xml`, `text/html` and `application/xhtml+xml` are **never**
  rendered inline; unsupported types fall back to a download action.

## Archives (#85)

- Create: zip / tar.gz / tar.xz (stdlib) from any selection, capped by
  `RD_ARCHIVE_CAP` (default 2 GiB uncompressed); the plan is built before
  a single byte is written.
- Extract: every member name is validated (no absolute paths, no `..`, no
  control characters — the zip-slip defence), symlink/hardlink/device
  members are skipped and reported, the uncompressed total is capped
  (zip-bomb guard), and members land through the same contained
  temp+rename path as uploads. A malicious archive cannot write outside
  the destination.
- Unsupported formats (`rar`, …) fail with a clear 400 message.

## Permissions (#90)

Listings show mode (`-rw-r--r--`), owner and group. `chmod` works on
selected files inside writable roots and is applied to an already-open
`O_NOFOLLOW` descriptor. Setuid/setgid bits are refused with an
explanation (403); chown is not offered. Every permission change is
audited (#92) so the v2.6 audit log can consume it.

## Audit log (#92)

- One JSONL record per mutating file operation (upload, download, delete,
  mkdir, rename, move, copy, chmod, archive, extract, transfer ops):
  `{ts, iso, action, user, ip, rid, root, path, dest, size, result}` —
  never file contents.
- Stored as `STATE_DIR/files-audit.jsonl`, rotated at 512 KiB with 3
  generations kept (`files-audit.jsonl.1` newest) — recent entries are
  never lost by a rotation.
- Read via the authenticated, read-only `GET /api/files/audit?limit=`.

## Storage layout & backup (#93)

```
~/.local/share/remote-desktop/      (RD_STATE_DIR)
├── backups/<version>-<ts>-<reason>/  versioned state snapshots (startup/API)
├── display.json                      last client-requested geometry
├── settings.json                     opt-in feature toggles
├── launch-history.json, custom-apps.json, .pins.json (BASE_DIR)
├── files-sessions/<sid>/             in-flight chunked uploads
│   ├── meta.json                     {root, dir, name, size, offset, state}
│   └── part.bin                      the partial upload (fsync'd chunks)
├── files-thumbs/                     thumbnail cache (safe to delete)
└── files-audit.jsonl(.1..3)          audit trail
```

**Backup guidance.** What to back up: `RD_STATE_DIR` (settings, display
state, audit) plus the shared roots themselves. Safe to omit: `files-thumbs/`
(regenerated on demand), `files-sessions/` (in-flight uploads only —
pausing a transfer before the backup keeps its acknowledged bytes in
`part.bin`), and `backups/` (snapshots of the state files themselves).
Never include `.passhash` (BASE_DIR) in a backup — it is the password
hash. `python3 control.py --export-state FILE.tar.gz` exports the state
files (secrets excluded by design).

## Mobile transfer troubleshooting

| Symptom | Cause / fix |
|---|---|
| Upload stalls, then fails on mobile data | Chunk not acknowledged within the network timeout — press ↻ on the queue item; it resumes from the acknowledged offset (no re-upload of earlier chunks). |
| "chunk checksum mismatch" loop | Usually a proxy mangling the body; try TLS directly (Funnel/Cloudflare) — the server never accepts a bad chunk twice without a retry. |
| 507 on upload | Free-space reserve or per-root quota would be crossed; the message names the limit. Delete files or raise `RD_FILES_RESERVE` / the root's `quota=`. |
| Downloads restart instead of resuming | The browser download manager resends `Range` automatically; when using the in-app fetch progress, the queue item keeps the offset — reload the page and retry. |
| Photos shared from the gallery land in the wrong place | Share-target uploads go to the default root; select the target root in the Files sheet before sharing, or move them in the manager. |
| Camera button does nothing | The tab needs camera permission for `capture=` inputs; deny/allow is remembered per device — reset site permissions. |
| Filenames look mangled on Android/iOS | Filenames are sent as RFC 5987 `filename*`; very old browsers see the ASCII-only fallback. Avoid control characters (they are rejected server-side anyway). |
| Thumbnail missing for PDF/video | No `pdftoppm`/`convert`/`ffmpeg` on the host → 501 and a download fallback; install `poppler-utils` or `imagemagick` to enable. |
