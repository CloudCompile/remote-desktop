#!/bin/sh
# Print the public URL of the remote desktop.
# Prefers the stable Tailscale Funnel name; falls back to the Cloudflare
# quick-tunnel URL (which changes whenever that tunnel restarts).
url=$(tailscale funnel status 2>/dev/null | grep -oE 'https://[a-z0-9.-]+\.ts\.net' | head -1)
if [ -n "$url" ]; then
  echo "$url"
  exit 0
fi
sudo -n journalctl -u remote-tunnel --no-pager 2>/dev/null \
  | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
