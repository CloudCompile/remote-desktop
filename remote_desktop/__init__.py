"""remote_desktop — the control server as a package (#22).

Module map (ownership notes at the top of each module):

    config.py    every RD_*/VNC_* setting: one schema, validation, generated docs
    log.py       structured logging: levels, request ids, text/json, rotation
    procs.py     child-process registry + subprocess helpers
    auth.py      cookie tokens, password check, login rate limiting
    http.py      BaseHandler plumbing + static files
    server.py    routes, bounded server, graceful shutdown, CLI
    ws.py        RFC6455 primitives (reader, frames, upgrade)
    vncproxy.py  /websockify: websocket -> VNC TCP with backpressure + reconnect
    audio.py     PulseAudio helpers, audio-out source/status
    media_in.py  device-in websockets (mic, webcam)
    clipboard.py X clipboard + persisted history
    files.py     upload/download directory
    apps.py      .desktop discovery, icons, launching
    store.py     apt app-store search/install
    windows.py   xdotool window management, xrandr resize
    stats.py     system stats + public health snapshot
    ready.py     dependency readiness probes (/api/ready)
    state.py     pins, display state, versioned backups/export/import

No module binds sockets, starts threads or touches X at import time;
the server starts only via server.main() (see control.py).
"""

from . import (
    apps,
    audio,
    auth,
    clipboard,
    config,
    files,
    http,
    limits,
    log,
    media_in,
    procs,
    ready,
    state,
    stats,
    vncproxy,
    windows,
    ws,
)
from . import server  # noqa: F401  (routes; imports the modules above)

# Public surface, re-exported so `from remote_desktop import *` (and the
# legacy `control` shim) expose the same names the single-file module did —
# including the underscore helpers the test-suite and scripts rely on.
from .apps import (
    APP_CATALOG,
    APP_DIRS,
    FALLBACK_APPS,
    find_app_window,
    find_icon,
    icon_path_for_app,
    launch_desktop,
    list_desktop_apps,
    self_launch_name,
    _parse_desktop,
)
from .audio import (
    AUDIO_LOG,
    MIC_RATE,
    MIC_SINK,
    MIC_SOURCE,
    audio_source,
    audio_status,
    camera_device,
    ensure_mic_source,
    _pa,
)
from .auth import (
    AUTH_USER,
    LOGIN_ATTEMPTS,
    LOGIN_ATTEMPTS_MAX_IPS,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW,
    PASS_FILE,
    PASS_HASH,
    SECRET,
    TOKEN_TTL,
    TRUSTED_PROXIES,
    _client_ip,
    _login_allowed,
    _make_token,
    _parse_cookie,
    _verify_password,
    _verify_token,
)
from .clipboard import (
    CLIP_HISTORY_FILE,
    CLIP_HISTORY_MAX,
    CLIP_ITEM_MAX,
    clip_add,
    clip_clear,
    clip_history,
    clip_item,
    clip_remove,
    clipboard_get,
    clipboard_set,
    clipboard_watcher,
    flush,
    prime_watcher,
    _clip_get,
    _clip_history,
    _clip_load,
    _clip_save,
    _clip_last_sel,
    _clip_set,
)
from .config import (
    BASE_DIR,
    BIND_HOST,
    CAM_DEVICE,
    CONFIG_FILE,
    DEFAULT_STATE_DIR,
    DPI,
    GEOMETRY,
    HOME,
    LOG_FILE,
    LOG_FORMAT,
    LOG_LEVEL,
    MAX_AUDIO,
    MAX_CONNECTIONS,
    MAX_MEDIA_IN,
    MAX_WS,
    PORT,
    READY_CACHE,
    STATE_DIR,
    VNC_BACKOFF,
    VNC_HOST,
    VNC_PORT,
    VNC_RETRIES,
    WEB_DIR,
    WS_HIGH_WATER,
    WS_IDLE_TIMEOUT,
    WS_MAX_MESSAGE,
    WS_SEND_TIMEOUT,
)
from .files import FILES_DIR, UPLOAD_LIMIT, _safe_file_path, cleanup_partial_uploads, list_files
from .http import MIME, MAX_BODY, BaseHandler, send_file
from .limits import acquire, caps, counts, full, gate, release, snapshot as limits_snapshot
from .log import log, request_id as log_request_id, setup as log_setup
from .media_in import camera_status, mic_status, websocket_camera, websocket_mic
from .config import _env, _pa_env, VERSION
from .procs import (
    child_procs,
    _child_procs_lock,
    die_with_parent as _die_with_parent,
    reap_children as _reap_children,
    _run,
    spawn_stream as _spawn_stream,
    active_children,
    reap_children,
    run_detached,
)
from .ready import PROBES, probe_ffmpeg, probe_pulse, probe_vnc, probe_x, ready_cache_clear, ready_once, ready_snapshot
from .server import BoundedThreadingHTTPServer, Handler, main
from .state import (
    PINS_FILE,
    STATE_VERSION,
    export_state,
    import_state,
    list_backups,
    load_display_state,
    load_pins,
    save_display_state,
    save_pins,
    snapshot,
    _prune_backups,
)
from .stats import health, system_stats
from .vncproxy import websocket_proxy, stats_snapshot as vnc_stats_snapshot
from .ws import WS_GUID, _read_exact, _ws_close_frame, _ws_frame, _ws_read_frame, _ws_pong, _WsReader, _ws_upgrade

__all__ = [
    # modules
    "apps",
    "audio",
    "auth",
    "clipboard",
    "config",
    "files",
    "http",
    "limits",
    "log",
    "media_in",
    "procs",
    "ready",
    "state",
    "stats",
    "vncproxy",
    "windows",
    "ws",
    "server",
    # apps
    "APP_CATALOG",
    "APP_DIRS",
    "FALLBACK_APPS",
    "find_app_window",
    "find_icon",
    "icon_path_for_app",
    "launch_desktop",
    "list_desktop_apps",
    "self_launch_name",
    "_parse_desktop",
    # audio
    "AUDIO_LOG",
    "MIC_RATE",
    "MIC_SINK",
    "MIC_SOURCE",
    "audio_source",
    "audio_status",
    "camera_device",
    "ensure_mic_source",
    "_pa",
    # auth
    "AUTH_USER",
    "LOGIN_ATTEMPTS",
    "LOGIN_ATTEMPTS_MAX_IPS",
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_WINDOW",
    "PASS_FILE",
    "PASS_HASH",
    "SECRET",
    "TOKEN_TTL",
    "TRUSTED_PROXIES",
    "_client_ip",
    "_login_allowed",
    "_make_token",
    "_parse_cookie",
    "_verify_password",
    "_verify_token",
    # clipboard
    "CLIP_HISTORY_FILE",
    "CLIP_HISTORY_MAX",
    "CLIP_ITEM_MAX",
    "clip_add",
    "clip_clear",
    "clip_history",
    "clip_item",
    "clip_remove",
    "clipboard_get",
    "clipboard_set",
    "clipboard_watcher",
    "flush",
    "prime_watcher",
    "_clip_get",
    "_clip_history",
    "_clip_load",
    "_clip_save",
    "_clip_last_sel",
    "_clip_set",
    # config
    "BASE_DIR",
    "BIND_HOST",
    "CAM_DEVICE",
    "CONFIG_FILE",
    "DEFAULT_STATE_DIR",
    "DPI",
    "GEOMETRY",
    "HOME",
    "LOG_FILE",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "MAX_AUDIO",
    "MAX_CONNECTIONS",
    "MAX_MEDIA_IN",
    "MAX_WS",
    "PORT",
    "READY_CACHE",
    "STATE_DIR",
    "VNC_BACKOFF",
    "VNC_HOST",
    "VNC_PORT",
    "VNC_RETRIES",
    "WEB_DIR",
    "WS_HIGH_WATER",
    "WS_IDLE_TIMEOUT",
    "WS_MAX_MESSAGE",
    "WS_SEND_TIMEOUT",
    "VERSION",
    # files
    "FILES_DIR",
    "UPLOAD_LIMIT",
    "_safe_file_path",
    "cleanup_partial_uploads",
    "list_files",
    # http / server
    "MIME",
    "MAX_BODY",
    "BaseHandler",
    "send_file",
    "BoundedThreadingHTTPServer",
    "Handler",
    "main",
    # limits / log / procs
    "acquire",
    "caps",
    "counts",
    "full",
    "gate",
    "release",
    "limits_snapshot",
    "log",
    "log_request_id",
    "log_setup",
    "child_procs",
    "_child_procs_lock",
    "_die_with_parent",
    "_env",
    "_pa_env",
    "_reap_children",
    "_run",
    "_spawn_stream",
    "active_children",
    "reap_children",
    "run_detached",
    # media_in / ready
    "camera_status",
    "mic_status",
    "websocket_camera",
    "websocket_mic",
    "PROBES",
    "probe_ffmpeg",
    "probe_pulse",
    "probe_vnc",
    "probe_x",
    "ready_cache_clear",
    "ready_once",
    "ready_snapshot",
    # state
    "PINS_FILE",
    "STATE_VERSION",
    "export_state",
    "import_state",
    "list_backups",
    "load_display_state",
    "load_pins",
    "save_display_state",
    "save_pins",
    "snapshot",
    "_prune_backups",
    # stats / vncproxy / ws
    "health",
    "system_stats",
    "websocket_proxy",
    "vnc_stats_snapshot",
    "WS_GUID",
    "_read_exact",
    "_ws_close_frame",
    "_ws_frame",
    "_ws_read_frame",
    "_ws_pong",
    "_WsReader",
    "_ws_upgrade",
]

__version__ = config.VERSION
