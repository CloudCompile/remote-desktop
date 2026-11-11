#!/bin/sh
# Start the remote desktop stack: ONE process, ONE port.
#   http://127.0.0.1:8090          -> login page / remote desktop UI
#   ws://127.0.0.1:8090/websockify -> proxied to the VNC server
#
# Usage:
#   ./start.sh                    # VNC at 127.0.0.1:5901
#   ./start.sh 127.0.0.1:5902     # custom VNC host:port
#   VNC_PORT=5902 ./start.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

VNC="${1:-}"
if [ -z "$VNC" ]; then VNC="${VNC_HOST:-127.0.0.1}:${VNC_PORT:-5901}"; fi
VNC_HOSTpart="${VNC%%:*}"; VNC_PORTpart="${VNC##*:}"

pkill -f "python3 $DIR/control.py" 2>/dev/null || true
sleep 0.3

VNC_HOST="$VNC_HOSTpart" VNC_PORT="$VNC_PORTpart" \
  nohup python3 "$DIR/control.py" >> /tmp/remote-control.stdout 2>&1 &
echo "started: http://127.0.0.1:8090  (pid $!, VNC $VNC)"
echo "logs: /tmp/remote-control.log /tmp/remote-control.stdout"
