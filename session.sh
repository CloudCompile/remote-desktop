#!/bin/sh
# Watchdog: keep the X/VNC session on $RD_DISPLAY (default :1, port 5901)
# AND audio alive forever (#32).
# - VNC:   restarts Xvnc within seconds if it dies, so browser clients
#          auto-reconnect (minimal openbox session, nothing can log it out)
# - Audio: ensures /run/user/<uid> exists, runs a PulseAudio daemon with a
#          null sink (RemoteOut) so desktop apps have something to play to
#          and ffmpeg can capture its monitor
# - PID:   a pid file with an ownership check guarantees two watchdogs can
#          never both own the display; stale X locks are only removed when
#          no live process owns them
# - TERM/INT: stop Xvnc and PulseAudio, wait for exit, clean the pid file
#
# Geometry/DPI (#31): RD_GEOMETRY (WIDTHxHEIGHT) and RD_DPI, else the last
# client-requested size persisted by the server in
# $XDG_DATA_HOME/remote-desktop/display.json, else 1280x800 / 96.
#
# Run detached:  setsid nohup ./session.sh > /tmp/vnc-watchdog.log 2>&1 &
# Status:        ./session.sh --status   (exit 0 running / 1 stopped)
LOG="${RD_SESSION_LOG:-/tmp/vnc-watchdog.log}"
SESSION_LOG="${RD_SESSION_VNC_LOG:-/tmp/vnc-session.log}"
RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
UID_=$(id -u)
GID_=$(id -g)
DISP="${RD_DISPLAY:-:1}"
N=$(printf %s "$DISP" | tr -d ':')
PORT=$((5900 + N))
XLOCK="/tmp/.X${N}-lock"
XSOCKET="/tmp/.X11-unix/X${N}"
PIDFILE="$RUNDIR/remote-session${N}.pid"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DISPLAY_STATE="$DATA_HOME/remote-desktop/display.json"
A=0

# structured log lines (#32)
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

alive() {
  # alive <pid>: true iff the pid exists (kill -0 fails with EPERM for
  # foreign processes that do exist, so fall back to /proc)
  [ -n "${1:-}" ] && { kill -0 "$1" 2>/dev/null || [ -d "/proc/$1" ]; }
}

wait_gone() {
  # wait_gone <pid> <seconds>
  i=0
  while alive "$1" && [ "$i" -lt "$2" ]; do
    sleep 1
    i=$((i + 1))
  done
  alive "$1"
}

resolve_geometry() {
  if [ -n "${RD_GEOMETRY:-}" ]; then
    echo "$RD_GEOMETRY"
    return
  fi
  if [ -r "$DISPLAY_STATE" ] && command -v python3 >/dev/null 2>&1; then
    g=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("geometry",""))' "$DISPLAY_STATE" 2>/dev/null)
    case "$g" in
      [0-9]*x[0-9]*) echo "$g" && return ;;
    esac
  fi
  echo "1280x800"
}

resolve_dpi() {
  if [ -n "${RD_DPI:-}" ]; then
    echo "$RD_DPI"
    return
  fi
  if [ -r "$DISPLAY_STATE" ] && command -v python3 >/dev/null 2>&1; then
    d=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("dpi",""))' "$DISPLAY_STATE" 2>/dev/null)
    case "$d" in
      [0-9]*) echo "$d" && return ;;
    esac
  fi
  echo "96"
}

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
      log "event=audio-null-sink status=created"
    fi
  else
    log "event=audio status=pulseaudio-failed"
  fi
}

start_vnc() {
  # ownership check before starting Xvnc (#32): a lock with a live owner
  # means another Xvnc owns this display — never start a second one
  if [ -f "$XLOCK" ]; then
    XPID=$(cat "$XLOCK" 2>/dev/null | tr -dc '0-9')
    if alive "$XPID"; then
      log "event=vnc-already-running display=$DISP pid=$XPID"
      return 0
    fi
    # stale lock/socket: clean only after confirming no live process owns it
    log "event=stale-xlock display=$DISP pid=${XPID:-unknown} action=clean"
    rm -f "$XLOCK" "$XSOCKET" 2>/dev/null
  fi
  GEOM=$(resolve_geometry)
  DPIC=$(resolve_dpi)
  vncserver -kill "$DISP" >/dev/null 2>&1
  log "event=vnc-start display=$DISP geometry=$GEOM dpi=$DPIC port=$PORT"
  vncserver "$DISP" -geometry "$GEOM" -localhost yes -SecurityTypes None \
    >> "$SESSION_LOG" 2>&1
  rc=$?
  log "event=vnc-start-done rc=$rc"
  if [ "$rc" -eq 0 ] && command -v xrandr >/dev/null 2>&1; then
    # apply the configured DPI to the fresh session (report failures, #31)
    if ! DISPLAY="$DISP" XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}" \
      xrandr --dpi "$DPIC" >> "$SESSION_LOG" 2>&1; then
      log "event=xrandr-dpi status=failed dpi=$DPIC"
    fi
  fi
  return "$rc"
}

stop_session() {
  log "event=stopping signal=$1"
  vncserver -kill "$DISP" >/dev/null 2>&1
  if [ -f "$XLOCK" ]; then
    XPID=$(cat "$XLOCK" 2>/dev/null | tr -dc '0-9')
    [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
    wait_gone "$XPID" 5 && log "event=xvnc-stop timeout" || log "event=xvnc-stop ok"
  fi
  pulseaudio -k >/dev/null 2>&1
  rm -f "$PIDFILE" 2>/dev/null
  log "event=stopped"
  exit 0
}

if [ "${1:-}" = "--status" ]; then
  if [ -f "$PIDFILE" ] && alive "$(cat "$PIDFILE" 2>/dev/null)"; then
    echo "running pid=$(cat "$PIDFILE") display=$DISP port=$PORT"
    exit 0
  fi
  echo "stopped display=$DISP"
  exit 1
fi

# watchdog ownership (#32): refuse to run twice on the same display
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null)
  if alive "$OLD" && [ "$OLD" != "$$" ]; then
    log "event=already-running pid=$OLD display=$DISP"
    exit 0
  fi
  log "event=stale-pidfile pid=${OLD:-unknown} action=takeover"
fi
echo $$ > "$PIDFILE"
trap 'stop_session TERM' TERM
trap 'stop_session INT' INT
log "event=watchdog-start pid=$$ display=$DISP port=$PORT"

while true; do
  # audio check every ~30s (A increments every 2s loop)
  A=$((A + 1))
  [ $((A % 15)) -eq 1 ] && ensure_audio

  if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    sleep 2
    continue
  fi
  log "event=vnc-down display=$DISP action=restart"
  start_vnc
  sleep 2
done
