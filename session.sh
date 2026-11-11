#!/bin/sh
# Watchdog: keep the X/VNC session on :1 (port 5901) AND audio alive forever.
# - VNC:   restarts Xtigervnc within seconds if it dies, so browser clients
#          auto-reconnect (minimal openbox session, nothing can log it out)
# - Audio: ensures /run/user/1000 exists, runs a PulseAudio daemon with a
#          null sink (RemoteOut) so desktop apps have something to play to
#          and ffmpeg can capture its monitor
# Run detached:  setsid nohup ./session.sh > /tmp/vnc-watchdog.log 2>&1 &
LOG=/tmp/vnc-watchdog.log
RUNDIR=/run/user/$(id -u)
UID_=$(id -u)
GID_=$(id -g)
A=0

ensure_audio() {
  if [ ! -d "$RUNDIR" ]; then
    sudo -n mkdir -p "$RUNDIR" 2>/dev/null
    sudo -n chown "$UID_:$GID_" "$RUNDIR" 2>/dev/null
    chmod 700 "$RUNDIR" 2>/dev/null
  fi
  export XDG_RUNTIME_DIR="$RUNDIR"
  if ! pactl info >/dev/null 2>&1; then
    pulseaudio -D --exit-idle-time=-1 --disable-shm >/dev/null 2>&1
    sleep 0.7
  fi
  if pactl info >/dev/null 2>&1; then
    if ! pactl list short sinks 2>/dev/null | grep -q remote_out; then
      pactl load-module module-null-sink sink_name=remote_out \
        sink_properties=device.description=RemoteOut >/dev/null 2>&1
      pactl set-default-sink remote_out >/dev/null 2>&1
      echo "$(date '+%H:%M:%S') audio: null sink created" >> "$LOG"
    fi
  else
    echo "$(date '+%H:%M:%S') audio: pulseaudio failed to start" >> "$LOG"
  fi
}

while true; do
  # audio check every ~30s (A increments every 2s loop)
  A=$((A + 1))
  [ $((A % 15)) -eq 1 ] && ensure_audio

  if ss -tln 2>/dev/null | grep -q ':5901 '; then
    sleep 2
    continue
  fi
  echo "$(date '+%H:%M:%S') VNC down - (re)starting :1" >> "$LOG"
  vncserver -kill :1 >/dev/null 2>&1
  rm -f /tmp/.X11-unix/X1 2>/dev/null
  vncserver :1 -geometry 1280x800 -localhost yes -SecurityTypes None \
    >> /tmp/vnc-session.log 2>&1
  echo "$(date '+%H:%M:%S') vncserver rc=$?" >> "$LOG"
  sleep 2
done
