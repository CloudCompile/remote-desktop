# systemd units

Replace the placeholders before installing:

- `YOUR_USER` — the Linux account that owns the X session and runs the server
- `/opt/remote-desktop` — where you cloned this repo

```sh
sed -e "s|YOUR_USER|$USER|g" -e "s|/opt/remote-desktop|$PWD|g" \
    systemd/remote-session.service > /tmp/remote-session.service
sed -e "s|YOUR_USER|$USER|g" -e "s|/opt/remote-desktop|$PWD|g" \
    systemd/remote-control.service > /tmp/remote-control.service

sudo cp /tmp/remote-session.service /tmp/remote-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now remote-session remote-control
```

`remote-tunnel.service` is optional and assumes `cloudflared` is at
`/usr/local/bin/cloudflared`.

## Sandboxing (#33)

All three units ship with sandboxing enabled (NoNewPrivileges, PrivateTmp,
ProtectSystem=full, ProtectHome=read-only with explicit ReadWritePaths,
restricted address families and LimitNOFILE). Verify your deployment with:

```sh
systemd-analyze security remote-control.service
```

Directives you may need to relax, and how:

| Directive | Effect when enabled | How to relax |
|---|---|---|
| `NoNewPrivileges=yes` | the **app store's** background `sudo apt-get install` cannot elevate (the documented sudo exception) | set `NoNewPrivileges=no` in `remote-control.service`, or install packages from an SSH session |
| `PrivateTmp=yes` + `BindPaths=-/tmp/.X11-unix` | the unit's `/tmp` is private; the X11 socket dir is bound back in so `xdotool`/`xclip` work | remove `PrivateTmp` if a tool needs other `/tmp` files |
| `ProtectHome=read-only` | the server cannot write into `$HOME` | the needed paths are already in `ReadWritePaths`; add more only if you change `RD_FILES_DIR` |
| `MemoryMax` (commented) | no memory ceiling by default | uncomment and size it (audio streaming needs headroom) |
| `TimeoutStopSec=10` | graceful stop budget (#25); a second SIGTERM forces an immediate exit | raise it if your streams need longer to drain |

`remote-session.service` intentionally stays less sandboxed: Xvnc writes
`/tmp/.X11-unix` sockets and `~/.vnc`, and PulseAudio needs the runtime
dir — the unit limits what it can without breaking the session.

## Readiness wiring (#40)

`GET /api/ready` answers 200 only when X, VNC, PulseAudio and ffmpeg are
usable (cached probes, short timeouts). Wire a supervisor to it:

```ini
# remote-control.service [Service] addition
ExecStartPost=/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do curl -fs http://127.0.0.1:8090/api/ready && exit 0; sleep 1; done; exit 1'
```

`/api/health` stays the fast liveness probe; use `/api/ready` for
readiness (tunnel health checks can point at it too).

## Tailscale instead of Cloudflare

Do not install `remote-tunnel.service`. After `tailscale up`, persist the
funnel with:

```sh
sudo tailscale funnel --bg 8090
```

`tailscaled` is enabled by the package, and `--bg` makes the funnel config
survive reboots.
