# Deployment

The stack is three long-running pieces:

1. **X session** — Xvnc `:1` + openbox + PulseAudio (or your own X server).
2. **Web server** — `control.py` on `127.0.0.1:8090`.
3. **Tunnel** — Tailscale Funnel (stable) or a Cloudflare tunnel.

## 1. Prerequisites

```sh
sudo apt install python3 tigervnc-standalone-server openbox xdotool xclip \
                 ffmpeg pulseaudio appstream
```

Optional: Chrome/Chromium for `webapps.sh`, `sudo` rules for the app store,
`cloudflared` and/or `tailscale` for public access.

## 2. Install the files

```sh
sudo mkdir -p /opt/remote-desktop
sudo cp -r . /opt/remote-desktop
sudo chown -R "$USER:$USER" /opt/remote-desktop
cd /opt/remote-desktop

openssl passwd -6 'choose-a-password' > .passhash
chmod 600 .passhash .pins.json .cliphistory.json 2>/dev/null || true
```

## 3. systemd units

The units in `systemd/` use `YOUR_USER` and `/opt/remote-desktop` as
placeholders:

```sh
sed -e "s|YOUR_USER|$USER|g" -e "s|/opt/remote-desktop|$PWD|g" \
    systemd/remote-session.service \
    systemd/remote-control.service \
    | sudo tee /etc/systemd/system/... # copy each separately
```

Simpler: copy the three `*.service` files, edit `User=`,
`WorkingDirectory=`, `ExecStart=` and `Environment=RD_USER=`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now remote-session remote-control
systemctl status remote-session remote-control
```

Logs:

```sh
journalctl -u remote-control -f     # server log
cat /tmp/remote-control.log         # request log
tail -f /tmp/vnc-watchdog.log       # VNC/audio watchdog
```

## 4. Public access

### Tailscale Funnel (recommended — stable URL)

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=remote
sudo tailscale funnel --bg 8090
```

- URL: `https://remote.<tailnet>.ts.net`
- Approve the Funnel node attribute when prompted.
- Funnel persists across reboots (`--bg` + `tailscaled` enabled).
- `tailscale funnel status` shows the current routing.

Funnel is public internet. Keep the password strong. For tailnet-only
access use `tailscale serve --bg 8090` instead (no public exposure).

### Cloudflare quick tunnel (ephemeral)

```sh
cloudflared tunnel --url http://127.0.0.1:8090 --no-autoupdate
./tunnel-url.sh
```

The URL changes whenever the tunnel restarts. For a stable custom domain
(e.g. `remote.example.com`) use a **named** Cloudflare tunnel; the domain's
DNS must be hosted on Cloudflare.

## 5. Optional: Chrome web apps

```sh
./webapps.sh
```

Creates `~/.local/share/applications/*.desktop` entries that run
`google-chrome --app=<url> --class=<name>` and downloads icons. Re-running
is safe. Entries show up in the Apps sheet automatically.

## 6. Updating

```sh
cd /opt/remote-desktop
git pull
sudo systemctl restart remote-control
```

The frontend is served from disk, so a browser reload is enough for UI
changes; Python changes need the restart.

### Supported versions

| Release | Status |
|---|---|
| latest tagged release | supported (fixes land as patch releases) |
| previous tagged release | best-effort security fixes only |
| older | unsupported — upgrade |

Tested on Ubuntu 24.04 with Python 3.10–3.12 and TigerVNC. Other Debian
derivatives generally work; see KNOWN-LIMITATIONS.md.

### Upgrading

1. Skim the [release notes](https://github.com/CloudCompile/remote-desktop/releases)
   for breaking changes (they are called out under "Changed" / "Removed").
2. `git pull` on the host, then `sudo systemctl restart remote-control`.
3. Reload the browser. State (pins, clipboard history) is read back from
   `.pins.json` / `.cliphistory.json`; the schemas are forward-compatible
   within a major version.
4. If the release added systemd units, re-run the unit copy step from
   section 3 and `sudo systemctl daemon-reload`.

### Rollback

Roll back by checking out the previous tag — state files are forward
compatible, so no data migration needs undoing:

```sh
cd /opt/remote-desktop
git checkout v2.0.0
sudo systemctl restart remote-control
```

If a rollback was needed because of a bug, please open an issue with the
root cause; per docs/RELEASING.md, tags are never rewritten.

## Hardening checklist

- [ ] Strong password in `.passhash` (mode 600)
- [ ] Port 8090 bound to 127.0.0.1 only
- [ ] TLS termination via Tailscale/Cloudflare — never expose plain HTTP
- [ ] VNC `-localhost yes`, no VNC password (auth is at HTTP layer)
- [ ] Firewall blocks 5901/8090 from untrusted networks
- [ ] Keep `.pins.json` / `.cliphistory.json` private (they contain user data)
- [ ] Sandboxed units installed (#33) — see [systemd/README.md](../systemd/README.md)

## Unit sandboxing (#33)

The shipped units run with `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, `ProtectHome=read-only` (with `ReadWritePaths` for
`/opt/remote-desktop` and the XDG state dir), restricted address families
and `LimitNOFILE`. Two deliberate trade-offs:

- **App store installs**: the store runs `sudo apt-get install` in the
  background, which `NoNewPrivileges` blocks. That is the documented sudo
  exception — set `NoNewPrivileges=no` if you want in-UI installs, or keep
  the sandbox and install packages over SSH.
- **X11 socket**: the X socket lives in `/tmp/.X11-unix`; the control unit
  binds that directory back into its private `/tmp` (`BindPaths`) so
  `xdotool`/`xclip` keep working.

Run `systemd-analyze security remote-control.service` to see the score;
each directive and its relaxation is tabulated in
[systemd/README.md](../systemd/README.md).

## Graceful shutdown (#25)

`SIGTERM`/`SIGINT` stop the listener, close websocket clients, terminate
ffmpeg children (bounded wait) and persist the clipboard history before
exit — inside the `TimeoutStopSec=10` the units set. A second signal
forces an immediate exit. `systemctl stop remote-control` therefore exits
cleanly and leaves no children behind.

## Readiness vs liveness (#40)

- `GET /api/health` — fast, dependency-free liveness (always cheap).
- `GET /api/ready` — readiness: 200 only when X, VNC, PulseAudio and
  ffmpeg are usable; 503 with a per-dependency reason otherwise. Probes
  are cached for `RD_READY_CACHE` seconds and each has its own timeout.
- Wire tunnels or supervisors to `/api/ready`; the UI surfaces the same
  status by dependency name.
