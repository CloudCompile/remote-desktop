#!/usr/bin/env python3
"""
Remote Desktop control server — entry point (#22).

The implementation lives in the remote_desktop/ package:
  - serves the web/ frontend (noVNC) as static files
  - JSON control API (window management, app launch, clipboard, audio)
  - websocket -> VNC proxy (/websockify)
  - low-latency audio streaming from the PulseAudio monitor
  - device-in streams (mic, webcam), clipboard and file transfer

One process, stdlib only, listens on 127.0.0.1:8090 by default. Put an
HTTPS reverse proxy (caddy/nginx/tailscale funnel) in front for remote
access. CLI:  --check-config | --export-state FILE | --import-state FILE

This file is a thin shim kept for backwards compatibility: `python3
control.py` starts the server and `import control` still exposes the
historical names (tests and scripts may import either control or
remote_desktop).
"""

import hashlib  # noqa: F401  (legacy re-exports: control.hashlib in old tests)
import hmac  # noqa: F401

from remote_desktop import *  # noqa: F401,F403
from remote_desktop.server import main

if __name__ == "__main__":
    raise SystemExit(main())
