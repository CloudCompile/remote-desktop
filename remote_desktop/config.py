"""Configuration: one schema for every setting (#23).

Ownership: every RD_* / VNC_* environment variable is declared in SCHEMA
here and parsed only here. Parsing never raises at import time — a bad
value falls back to the default and is reported by validate(), which
main() runs before the listen socket is bound. No module may read
os.environ for settings directly (the X11/XDG *child* environment
composition in _env()/_pa_env() is the one exception: it is configuration).

Generated docs: `python3 -m remote_desktop.config` rewrites
docs/CONFIGURATION.md from SCHEMA — the documented env table is generated,
never hand-maintained. Secrets (marked secret=True) are redacted there.
"""

import os
import re

VERSION = "2.4.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
HOME = os.path.expanduser("~")

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
DEFAULT_STATE_DIR = os.path.join(XDG_DATA_HOME, "remote-desktop")


class ConfigError(Exception):
    """Raised by check() when startup validation fails (actionable message)."""


class _Setting:
    """One row of the configuration schema.

    name: settings key (also the module-level constant name in upper case)
    env:  environment variable that overrides the default
    kind: parser used for the raw string (str/int/float/port/path/csv/bool/
          bytes/duration/level/enum/format/geometry/dpi)
    default: value (or zero-arg callable evaluated at parse time)
    """

    def __init__(self, name, env, kind, default, description, secret=False):
        self.name, self.env, self.kind = name, env, kind
        self.default, self.description, self.secret = default, description, secret


SCHEMA = [
    _Setting("auth_user", "RD_USER", "str", "admin", "Login username shown on the login page."),
    _Setting(
        "pass_file",
        "RD_PASSFILE",
        "path",
        lambda: os.path.join(BASE_DIR, ".passhash"),
        "File holding the crypt SHA-512 password hash (create with: `openssl passwd -6`). Contents are never logged.",
        secret=True,
    ),
    _Setting(
        "files_dir",
        "RD_FILES_DIR",
        "path",
        lambda: os.path.join(HOME, "Downloads"),
        "Directory for file upload/download (created if missing). Used as the default (single) shared root.",
    ),
    _Setting(
        "files_roots",
        "RD_FILES_ROOTS",
        "str",
        "",
        'Shared roots for the file manager (#84): comma-separated PATH[:LABEL[:FLAGS]] with FLAGS "ro" '
        'and/or "quota=BYTES". Empty = single root from RD_FILES_DIR. Example: '
        '"/data/projects:Projects:ro,/srv/media:Media:quota=1073741824".',
    ),
    _Setting(
        "files_reserve",
        "RD_FILES_RESERVE",
        "bytes",
        64 * 1024**2,
        "Free-space reserve per root (#89): uploads that would leave less free space are refused (0 disables).",
    ),
    _Setting(
        "trusted_proxies", "RD_TRUSTED_PROXIES", "csv", "", "Comma-separated proxy IPs allowed to set X-Forwarded-For."
    ),
    _Setting(
        "bind_host",
        "RD_BIND",
        "str",
        "127.0.0.1",
        "Address the control server binds. Keep the default and put an HTTPS proxy in front for remote access.",
    ),
    _Setting("port", "RD_PORT", "port", 8090, "TCP port for the single control server (HTTP + websocket + audio)."),
    _Setting("display", "RD_DISPLAY", "str", ":1", "X display used by xdotool/xclip/xrandr child processes."),
    _Setting("vnc_host", "VNC_HOST", "str", "127.0.0.1", "Host of the VNC server the websocket proxy connects to."),
    _Setting("vnc_port", "VNC_PORT", "port", 5901, "Port of the VNC server (Xvnc :1 by default)."),
    _Setting(
        "geometry",
        "RD_GEOMETRY",
        "geometry",
        "1280x800",
        "Desktop size WIDTHxHEIGHT used by session.sh when starting Xvnc.",
    ),
    _Setting("dpi", "RD_DPI", "dpi", 96, "Font DPI for the desktop session (session.sh / xrandr --dpi)."),
    _Setting("cam_device", "REMOTE_CAM_DEVICE", "str", "", "Webcam device for cam-in (default: first /dev/video*)."),
    _Setting("log_file", "RD_LOG_FILE", "path", "/tmp/remote-control.log", "Structured log sink (rotated)."),
    _Setting("log_level", "RD_LOG_LEVEL", "level", "INFO", "Minimum log level: DEBUG, INFO, WARNING or ERROR."),
    _Setting("log_format", "RD_LOG_FORMAT", "format", "text", "Log record style: text or json."),
    _Setting(
        "max_connections",
        "RD_MAX_CONNECTIONS",
        "int",
        64,
        "Maximum concurrent TCP connections served (hard bound on threads).",
    ),
    _Setting("max_ws", "RD_MAX_WS", "int", 32, "Maximum concurrent /websockify VNC websocket proxies."),
    _Setting("max_audio", "RD_MAX_AUDIO", "int", 8, "Maximum concurrent audio streams (ffmpeg children)."),
    _Setting("max_media_in", "RD_MAX_MEDIA_IN", "int", 2, "Maximum concurrent device-in streams (mic + webcam)."),
    _Setting(
        "audio_shared",
        "RD_AUDIO_SHARED",
        "bool",
        True,
        "Share one ffmpeg capture across audio listeners with the same quality (#105). Set 0 for per-client capture (isolation).",
    ),
    _Setting(
        "rec_max_minutes",
        "RD_REC_MAX_MINUTES",
        "int",
        120,
        "Maximum desktop-audio recording duration in minutes (#102).",
    ),
    _Setting("rec_max_mb", "RD_REC_MAX_MB", "int", 200, "Maximum desktop-audio recording size in MiB (#102)."),
    _Setting(
        "http_timeout", "RD_HTTP_TIMEOUT", "duration", 120, "Seconds an idle keep-alive HTTP connection is kept open."
    ),
    _Setting(
        "ws_idle_timeout",
        "RD_WS_IDLE_TIMEOUT",
        "duration",
        300,
        "Seconds a websocket may stay silent before it is closed.",
    ),
    _Setting(
        "ws_send_timeout",
        "RD_WS_SEND_TIMEOUT",
        "duration",
        5,
        "Seconds to wait for a websocket client to accept bytes before it is dropped as slow.",
    ),
    _Setting(
        "ws_max_message",
        "RD_WS_MAX_MESSAGE",
        "bytes",
        8 * 1024 * 1024,
        "Cap on any single websocket message from the client.",
    ),
    _Setting(
        "ws_high_water",
        "RD_WS_HIGH_WATER",
        "bytes",
        4 * 1024 * 1024,
        "Buffered-bytes high-water mark per websocket direction; a peer exceeding it is disconnected.",
    ),
    _Setting(
        "vnc_retries", "RD_VNC_RETRIES", "int", 5, "Reconnect attempts to the VNC server before a proxy gives up."
    ),
    _Setting(
        "vnc_backoff", "RD_VNC_BACKOFF", "float", 0.5, "Initial reconnect backoff in seconds (doubles, capped at 4s)."
    ),
    _Setting(
        "state_dir",
        "RD_STATE_DIR",
        "path",
        DEFAULT_STATE_DIR,
        "Directory for backups, display state and the state manifest.",
    ),
    _Setting(
        "archive_cap",
        "RD_ARCHIVE_CAP",
        "bytes",
        2 * 1024**3,
        "Maximum uncompressed size when creating or extracting an archive (#85).",
    ),
    _Setting(
        "transfer_retention",
        "RD_TRANSFER_RETENTION",
        "duration",
        24 * 3600,
        "Seconds completed/failed transfer-queue jobs are kept before GC (#88).",
    ),
    _Setting(
        "ready_cache", "RD_READY_CACHE", "duration", 2, "Seconds readiness probes (/api/ready) results are cached."
    ),
    _Setting(
        "config_file",
        "RD_CONFIG",
        "path",
        "",
        "Optional KEY=VALUE config file; real environment variables "
        "override it (precedence: defaults < file < environment).",
    ),
]

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LOG_FORMATS = ("text", "json")
_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)$")


def _parse_one(spec, raw):
    """Parse a raw env string (or the default) according to its kind."""
    if raw is None:
        return spec.default() if callable(spec.default) else spec.default
    kind = spec.kind
    if kind == "str":
        return raw
    if kind == "path":
        return os.path.expanduser(raw)
    if kind == "csv":
        return {p.strip() for p in raw.split(",") if p.strip()}
    if kind == "bool":
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    if kind in ("int", "port", "dpi"):
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "duration":
        return float(raw)
    if kind == "bytes":
        return int(raw)
    if kind == "level":
        return raw.upper()
    if kind == "format":
        return raw.lower()
    if kind == "geometry":
        return raw
    raise ValueError(f"unknown kind {kind!r}")


def _read_config_file(path):
    """Parse an optional KEY=VALUE config file (#23). Accepts either the
    env-var name (RD_PORT) or the settings key (port). Returns (values,
    errors); a missing/unreadable file explicitly requested is an error."""
    vals, errs = {}, []
    if not path:
        return vals, errs
    try:
        with open(os.path.expanduser(path)) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    except OSError as e:
        errs.append(f"RD_CONFIG: cannot read config file {path!r}: {e}")
    return vals, errs


def load(env=None):
    """Parse every setting from `env` (default: os.environ).

    Precedence: schema defaults < RD_CONFIG file < real environment.
    Never raises: values that fail to parse fall back to the default and
    are reported as errors, so a typo cannot crash an import.
    Returns (values: dict, errors: list[str]).
    """
    env = os.environ if env is None else env
    values, errors = {}, []
    file_vals, file_errs = _read_config_file(env.get("RD_CONFIG", ""))
    errors.extend(file_errs)
    for spec in SCHEMA:
        raw = env.get(spec.env)
        if raw is None:
            raw = file_vals.get(spec.env, file_vals.get(spec.name))
        try:
            values[spec.name] = _parse_one(spec, raw)
        except Exception as e:
            values[spec.name] = spec.default() if callable(spec.default) else spec.default
            if raw != "" and raw is not None:
                errors.append(f"{spec.env}: invalid value {raw!r} ({e}); using default {values[spec.name]!r}")
    return values, errors


def validate(values=None):
    """Validate parsed settings. Returns a list of actionable error strings
    (empty when the configuration is good). Runs before the socket binds."""
    if values is None:
        values, import_errors = load()
    else:
        import_errors = []
    errs = list(import_errors)
    v = values

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    for name in ("port", "vnc_port"):
        need(1 <= v[name] <= 65535, f"{_env_of(name)}: port must be 1-65535, got {v[name]}")
    need(bool(v["vnc_host"]), "VNC_HOST: must not be empty")
    need(bool(v["auth_user"]), "RD_USER: must not be empty")
    need(bool(v["pass_file"]), "RD_PASSFILE: must not be empty")
    need(
        _GEOMETRY_RE.match(str(v["geometry"])) is not None,
        f"RD_GEOMETRY: expected WIDTHxHEIGHT (e.g. 1280x800), got {v['geometry']!r}",
    )
    m = _GEOMETRY_RE.match(str(v["geometry"]))
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        need(320 <= w <= 7680, f"RD_GEOMETRY: width {w} out of range 320-7680")
        need(240 <= h <= 4320, f"RD_GEOMETRY: height {h} out of range 240-4320")
    need(48 <= v["dpi"] <= 512, f"RD_DPI: must be 48-512, got {v['dpi']}")
    need(v["log_level"] in _LOG_LEVELS, f"RD_LOG_LEVEL: one of {'|'.join(_LOG_LEVELS)}, got {v['log_level']!r}")
    need(v["log_format"] in _LOG_FORMATS, f"RD_LOG_FORMAT: one of {'|'.join(_LOG_FORMATS)}, got {v['log_format']!r}")
    for name in ("max_connections", "max_ws", "max_audio", "max_media_in"):
        need(1 <= v[name] <= 10_000, f"{_env_of(name)}: must be 1-10000, got {v[name]}")
    need(1 <= v["rec_max_minutes"] <= 1440, f"RD_REC_MAX_MINUTES: must be 1-1440, got {v['rec_max_minutes']}")
    need(1 <= v["rec_max_mb"] <= 4096, f"RD_REC_MAX_MB: must be 1-4096, got {v['rec_max_mb']}")
    for name in ("http_timeout", "ws_idle_timeout", "ready_cache"):
        need(1 <= v[name] <= 86_400, f"{_env_of(name)}: seconds must be 1-86400, got {v[name]}")
    need(1 <= v["ws_send_timeout"] <= 120, f"RD_WS_SEND_TIMEOUT: seconds must be 1-120, got {v['ws_send_timeout']}")
    need(
        1024 <= v["ws_max_message"] <= 64 * 1024 * 1024,
        f"RD_WS_MAX_MESSAGE: bytes must be 1KiB-64MiB, got {v['ws_max_message']}",
    )
    need(
        64 * 1024 <= v["ws_high_water"] <= 256 * 1024 * 1024,
        f"RD_WS_HIGH_WATER: bytes must be 64KiB-256MiB, got {v['ws_high_water']}",
    )
    need(0 <= v["vnc_retries"] <= 100, f"RD_VNC_RETRIES: must be 0-100, got {v['vnc_retries']}")
    need(0.05 <= v["vnc_backoff"] <= 30, f"RD_VNC_BACKOFF: seconds must be 0.05-30, got {v['vnc_backoff']}")
    need(
        0 <= v["files_reserve"] <= 1024 * 1024**3,
        f"RD_FILES_RESERVE: bytes must be 0-1GiB, got {v['files_reserve']}",
    )
    need(
        1024 <= v["archive_cap"] <= 64 * 1024**3,
        f"RD_ARCHIVE_CAP: bytes must be 1KiB-64GiB, got {v['archive_cap']}",
    )
    need(
        60 <= v["transfer_retention"] <= 30 * 86_400,
        f"RD_TRANSFER_RETENTION: seconds must be 60-2592000, got {v['transfer_retention']}",
    )
    fd = v["files_dir"]
    need(not os.path.exists(fd) or os.path.isdir(fd), f"RD_FILES_DIR: {fd!r} exists and is not a directory")
    return errs


def check(values=None):
    """validate(), raising ConfigError with a readable multi-line message."""
    errs = validate(values)
    if errs:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errs))


def ensure_dirs(values):
    """Create directories the server writes into. Returns error strings."""
    errs = []
    for name in ("files_dir", "state_dir"):
        d = values[name]
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            errs.append(f"{_env_of(name)}: cannot create {d!r}: {e}")
    return errs


def _env_of(name):
    return SCHEMA_BY_NAME[name].env


SCHEMA_BY_NAME = {s.name: s for s in SCHEMA}

# --------------------------------------------------------------- module values
# Parsed once at import so sibling modules and the legacy `control` shim can
# read plain constants. Import-time parsing never raises (see load()); the
# same errors are re-reported by validate() at startup.

VALUES, _IMPORT_ERRORS = load()

AUTH_USER = VALUES["auth_user"]
PASS_FILE = VALUES["pass_file"]
FILES_DIR = VALUES["files_dir"]
TRUSTED_PROXIES = VALUES["trusted_proxies"]
BIND_HOST = VALUES["bind_host"]
PORT = VALUES["port"]
DISPLAY = VALUES["display"]
VNC_HOST = VALUES["vnc_host"]
VNC_PORT = VALUES["vnc_port"]
GEOMETRY = VALUES["geometry"]
DPI = VALUES["dpi"]
CAM_DEVICE = VALUES["cam_device"]
LOG_FILE = VALUES["log_file"]
LOG_LEVEL = VALUES["log_level"]
LOG_FORMAT = VALUES["log_format"]
MAX_CONNECTIONS = VALUES["max_connections"]
MAX_WS = VALUES["max_ws"]
MAX_AUDIO = VALUES["max_audio"]
MAX_MEDIA_IN = VALUES["max_media_in"]
AUDIO_SHARED = VALUES["audio_shared"]
REC_MAX_MINUTES = VALUES["rec_max_minutes"]
REC_MAX_MB = VALUES["rec_max_mb"]
HTTP_TIMEOUT = VALUES["http_timeout"]
WS_IDLE_TIMEOUT = VALUES["ws_idle_timeout"]
WS_SEND_TIMEOUT = VALUES["ws_send_timeout"]
WS_MAX_MESSAGE = VALUES["ws_max_message"]
WS_HIGH_WATER = VALUES["ws_high_water"]
VNC_RETRIES = VALUES["vnc_retries"]
VNC_BACKOFF = VALUES["vnc_backoff"]
STATE_DIR = VALUES["state_dir"]
READY_CACHE = VALUES["ready_cache"]
CONFIG_FILE = VALUES["config_file"]

# Child-process environment (X11 + PulseAudio) used by every spawned tool.
XAUTHORITY = os.environ.get("XAUTHORITY", os.path.join(HOME, ".Xauthority"))
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

ENV = {
    "DISPLAY": DISPLAY,
    "XAUTHORITY": XAUTHORITY,
    "XDG_RUNTIME_DIR": XDG_RUNTIME_DIR,
}


def _env():
    return {**os.environ, **ENV}


def _pa_env():
    """Child env with PULSE_SERVER/cookie pinned to this user's daemon."""
    e = _env()
    sock = os.path.join(XDG_RUNTIME_DIR, "pulse", "native")
    if os.path.exists(sock):
        e["PULSE_SERVER"] = "unix:" + sock
    cookie = os.path.join(HOME, ".config", "pulse", "cookie")
    if os.path.exists(cookie):
        e["PULSE_COOKIE"] = cookie
    return e


def geometry_wh(value=None):
    """Split a WIDTHxHEIGHT string into (w, h); (None, None) if invalid."""
    m = _GEOMETRY_RE.match(str(value if value is not None else GEOMETRY))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


# ------------------------------------------------------------- generated docs


def format_env_table():
    """Render the generated environment table for docs/CONFIGURATION.md."""
    lines = [
        "<!-- Generated by `python3 control.py --gen-config-docs` — do not edit by hand. -->",
        "",
        "| Setting | Type | Default | Description |",
        "|---|---|---|---|",
    ]
    for s in SCHEMA:
        default = s.default() if callable(s.default) else s.default
        if s.secret:
            default = "(redacted)"
        elif s.kind == "path" and isinstance(default, str):
            home = os.path.expanduser("~")
            if default.startswith(home):
                default = default.replace(home, "~", 1)
            if default == "":
                default = "(empty)"
        desc = s.description.replace("\n", " ")
        lines.append(f"| `{s.env}` | {s.kind} | `{default}` | {desc} |")
    return "\n".join(lines) + "\n"


def write_env_doc(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "CONFIGURATION.md")
    body = (
        "# Configuration\n\n"
        "Every setting, its environment variable, default and meaning — "
        "generated from `remote_desktop/config.py` (issue #23). Secrets are "
        "redacted. Validate a deployment with `python3 control.py --check-config`.\n\n"
        + format_env_table()
        + "\nPrecedence: schema defaults < `RD_CONFIG` KEY=VALUE file < "
        "real environment variables.\n"
    )
    with open(path, "w") as f:
        f.write(body)
    return path


if __name__ == "__main__":
    print(write_env_doc(), "written")
