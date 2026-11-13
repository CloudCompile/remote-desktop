# ADR-0005: File-backed runtime state

- Status: accepted
- Date: 2026-11-01
- Deciders: CloudCompile

## Context

The server persists a little state: pinned apps, clipboard history,
display preferences and the password hash. Running PostgreSQL (or any
daemon) for kilobytes of JSON would dominate the deployment effort; the
process must also survive crashes and full disks without corrupting
state.

## Decision

Plain JSON files next to the app (`/opt/remote-desktop/.pins.json`,
`.cliphistory.json`) or under the XDG state dir
(`~/.local/share/remote-desktop/` — backups, display.json), written
atomically (temp file + fsync + rename), and snapshotted into versioned
backups at startup and on demand (#30). The password hash never enters
backups or exports.

## Consequences

- Inspectable with `cat`, fixable with an editor, migratable with
  `--export-state`/`--import-state`.
- Concurrent writers must coordinate in-process (locks); no multi-host
  sharing — the single-process ADR-0001 makes that moot today.
- Crash safety depends on the atomic-write discipline; `_clip_save` and
  `save_pins` are the reference implementations.

## Revisit triggers

Multi-user or multi-host state, or an audit/replication requirement,
would warrant moving state to SQLite first (stdlib) — not to a server.
