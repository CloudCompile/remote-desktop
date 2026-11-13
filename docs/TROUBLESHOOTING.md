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

## Audio

The audio pipeline is: apps play into a PulseAudio sink → ffmpeg captures
the sink's **monitor** and encodes Opus/WebM (or raw PCM) → the browser
plays it over a chunked HTTP stream. The UI's **Audio → Diagnostics** view
shows the same state this section asks you to inspect; **Copy diagnostics**
redacts host details first (#106).

### Environment: PulseAudio vs PipeWire

Desktop audio is driven through `pactl`, which speaks to either daemon:

```bash
pactl info | grep Server            # "PulseAudio (on PipeWire ...)" = PipeWire
systemctl --user status pipewire pulseaudio --no-pager | head
```

- PipeWire users need `pipewire-pulse` (provides the PulseAudio socket)
  and, for desktop audio capture, nothing else — `pactl` commands are
  translated. Sink capture works the same way.
- Pure PulseAudio users need the daemon running for the session user, and
  `XDG_RUNTIME_DIR` pointing at the same session the desktop runs in.
- The server pins `PULSE_SERVER` to the user's socket when it exists
  (`$XDG_RUNTIME_DIR/pulse/native`) and passes the pulse cookie.

### Symptom → cause → fix matrix (#107)

| Symptom | Likely cause | Verify (command) | Fix |
|---|---|---|---|
| **Silent everywhere, test tone inaudible** | No sink at all (daemon down) | `pactl list short sinks` empty, `pactl info` fails | Start the daemon: `systemctl --user start pipewire` (PipeWire) or `pulseaudio --start`; then restart the session (`session.sh` recreates the `remote_out` sink) |
| **Silent in apps, test tone audible** | Apps play into a different sink | `pactl list short sink-inputs` shows inputs on another sink | Set the default: `pactl set-default-sink remote_out`, or move them: Audio sheet → Applications (per-app volume, #95) |
| **Test tone audible, browser silent** | Transport or playback side | Audio sheet → Diagnostics: `codec` shows browser caps; buffer counters stay at 0 | Check `/api/audio.opus` reaches the browser (proxy buffering! set `X-Accel-Buffering no` / disable `proxy_buffering`); try the PCM codec option |
| **Silent after backgrounding the tab / switching networks** | Dropped stream or suspended AudioContext | Diagnostics: audio shows the **Retry** state | Tap **Retry** (or Start); audio also auto-resumes on reconnect (#124) |
| **Silent after PulseAudio restart** | Capture source vanished | `pactl list short sinks` no longer lists `remote_out` | Restart the session or wait: the next audio start recreates the sink and reconnects the pipeline (#124) |
| **Crackling / stutter** | Encoder starved or buffer too small | Diagnostics: `health` shows underruns climbing | Pick a lower quality tier (Low / 48 kbps); persistent crackle with 0% CPU → check `ffmpeg` in `/tmp/remote_audio.log` |
| **Audio drifts / delay grows over an hour** | Clock drift between capture and playback | Diagnostics: `buffer` grows unbounded | Fixed by the adaptive buffer + live-edge resync (#99); if it persists, stop/start the stream |
| **"Unsupported codec" / silent Safari** | Browser lacks WebM/Opus MSE | Diagnostics: `browser MSE/Opus NO` | Use the **PCM** codec option, or a browser with Opus MSE (Chrome, Firefox 49+, Safari 17.1+); audio never starts ffmpeg on incompatible clients (#100) |
| **Mic/cam missing in desktop apps** | `remote_mic` source or v4l2loopback absent | `pactl list short sources` (look for `remote_mic`); `lsmod \| grep v4l2loopback` | `sudo modprobe v4l2loopback`; the mic source is created on first use (#97) |
| **Recording disappears after a crash** | Final fragment truncated | `ls -la ~/Downloads/Recordings/` | Restart the server: fragment files (`.part`) are renamed to playable `.webm` at startup (#102) |

### Capture checks outside the app

```bash
# is there a sink, and what would be captured?
pactl list short sinks
pactl get-default-sink
# can ffmpeg encode from it? (5 s, writes nothing)
ffmpeg -f pulse -i "$(pactl get-default-sink).monitor" -t 5 -f null -
# what does the server see?
curl -s -H "Cookie: $RD_COOKIE" https://<host>/api/audio/diagnostics | python3 -m json.tool
```

ffmpeg errors from every audio path land in `/tmp/remote_audio.log`.

### Codec and browser support (#100/#107)

| Browser | Opus/WebM via MSE | Raw PCM via WebAudio | Default |
|---|---|---|---|
| Chrome / Edge / Android | yes | yes | Opus |
| Firefox 49+ | yes | yes | Opus |
| Safari 17.1+ (macOS/iOS) | yes | yes | Opus |
| Older Safari | no | yes | PCM fallback + notice |
| Browsers without WebAudio | — | no | audio disabled with a clear message; no ffmpeg is spawned |

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
