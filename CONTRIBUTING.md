# Contributing

Thanks for taking a look. This is a small, single-process project — keep it
that way where possible.

## Development setup

```sh
git clone https://github.com/CloudCompile/remote-desktop.git
cd remote-desktop
python3 -m py_compile control.py remote_desktop/*.py
sh -n session.sh start.sh webapps.sh tunnel-url.sh
```

### pre-commit hooks (#38)

Formatting and lint run through [ruff](https://docs.astral.sh/ruff/)
(`ruff check` + `ruff format --check`, see `pyproject.toml`) and basic
whitespace hooks:

```sh
pip install pre-commit
pre-commit install          # run on every commit
pre-commit run --all-files  # run on the whole tree
```

CI runs the same hooks in check-only mode. `ruff format` is intentionally
check-only and excludes `scripts/check-links.py` (legacy, unformatted);
don't reformat legacy files inside behaviour changes.

To syntax-check the frontend's inline module, extract the `<script>` block
and run `node --check` on it, or just open the page with the devtools
console.

Run against any VNC server:

```sh
VNC_HOST=127.0.0.1 VNC_PORT=5901 python3 control.py
```

The frontend is plain ES modules + one HTML file; edit and reload, no build.

## Guidelines

- **Stdlib only** for `control.py`. New runtime deps need a good reason and
  must be documented in the README.
- Keep the server bound to `127.0.0.1`; TLS belongs to the tunnel/proxy.
- Any new `/api/*` route must keep the auth check (except an intentional,
  documented public endpoint).
- Sanitise user input: package names, app ids, filenames are validated.
- Do not commit `.passhash`, `.pins.json`, `.cliphistory.json`, logs, or
  anything under `__pycache__/`.
- Frontend: mobile first, 44px+ touch targets, no external CDNs.
- Vendored noVNC files are MPL-2.0 — do not relicense them.

## Pull requests

1. Fork, branch from `main`.
2. Make focused commits with a clear message.
3. Describe the change, how you tested it, and any security impact.
4. Update `README.md` / `docs/` when behaviour or configuration changes.

## Reporting bugs

Use the issue template and include: OS, browser/device, `journalctl -u
remote-control`, and steps to reproduce. Never paste your password hash or
clipboard contents.
