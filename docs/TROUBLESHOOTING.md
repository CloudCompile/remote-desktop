# Troubleshooting

## The page loads but the desktop stays black

- Check `:5901` is listening: `ss -tlnp | grep 5901`.
- `journalctl -u remote-session` / `/tmp/vnc-watchdog.log`.
- Make sure the browser can reach `/websockify` (same host/port; a proxy
  must forward websocket upgrades).

## Login fails / "logins disabled"

- `.passhash` must exist next to `control.py` (or at `RD_PASSFILE`) and
  contain a crypt(3) hash: `openssl passwd -6 'pw' > .passhash`.
- The username defaults to `admin`; set `RD_USER` to change it.
- Too many failures → 429 for 10 minutes (in-memory rate limit).

## Apps don't open, or the wrong window is focused

- Native apps are matched by `StartupWMClass` / WM_CLASS. If an app reports
  an unexpected class, add `StartupWMClass=` to its `.desktop` file.
- Chrome `--app` windows all share the class `Google-chrome`; the matcher
  uses the WM_CLASS *instance* (the site host) and the title suffix
  (` - App Name`) to tell them apart.
- Terminal apps are matched by exact xterm title.

## Clipboard history is empty

- The watcher uses `xclip` on `DISPLAY` (`RD_DISPLAY`, default `:1`).
- `xclip` must be installed and the X auth must allow the server:
  `XAUTHORITY=~/.Xauthority xclip -o -selection clipboard`.

## No audio

- `session.sh` creates a PulseAudio null sink called `remote_out`; apps
  must play into it (it is set as the default sink).
- Test the pipeline: Audio sheet → **Test** plays a tone.
- `pactl list short sinks`, `pactl info`; ffmpeg errors go to
  `/tmp/remote_audio.log`.

## Bottom of the screen isn't clickable

- The page reserves an inset for an OS navigation bar only when
  `env(safe-area-inset-bottom)` reports one or the app runs in true
  fullscreen. If your device still has a dead strip, set
  **More → Screen edges → Reserved edge** manually (0 disables it).

## The toolbar keeps covering things

- It is click-through between buttons, hides on any touch on the desktop,
  and auto-hides after 6 s. Use the **▲ Controls** pill to bring it back.

## The public URL stopped working

- Cloudflare **quick** tunnels get a new hostname on every restart:
  `./tunnel-url.sh`.
- Tailscale: `sudo tailscale funnel status`; ensure Funnel is enabled for
  the tailnet and `tailscaled` is running.
