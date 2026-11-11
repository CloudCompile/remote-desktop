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

## Tailscale instead of Cloudflare

Do not install `remote-tunnel.service`. After `tailscale up`, persist the
funnel with:

```sh
sudo tailscale funnel --bg 8090
```

`tailscaled` is enabled by the package, and `--bg` makes the funnel config
survive reboots.
