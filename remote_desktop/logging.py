"""Structured logging with levels and request IDs (#24).

Ownership: the only module allowed to emit or configure log records.
Sink: config.LOG_FILE (default /tmp/remote-control.log) via a rotating
file handler, plus a console handler on stderr for WARNING+.

- Level: RD_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR), configurable without
  code changes.
- Format: RD_LOG_FORMAT=text|json. Timestamps are kept in both.
- Request IDs: `request_id` is a ContextVar set per HTTP request by the
  handler; every record emitted from that request's thread carries it
  (as `rid`, "-" outside a request). Threads not serving HTTP (audio,
  clipboard watcher) log with their own stream/stream-id fields instead.
- Privacy: secrets (password hashes, tokens, clipboard *contents*) are
  never logged; callers pass ids and counts, not payloads.
"""

import contextvars
import json
import logging
import logging.handlers
import os
import threading
import time

from . import config

request_id = contextvars.ContextVar("request_id", default="-")

_logger = logging.getLogger("remote_desktop")
_configured = threading.Event()


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.rid = request_id.get()
        return True


def _text_formatter():
    return logging.Formatter("%(asctime)s %(levelname)s [%(rid)s] %(message)s", datefmt="%H:%M:%S")


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        rec = {
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "rid": record.rid,
            "module": record.name,
            "msg": record.getMessage(),
        }
        return json.dumps(rec)


def setup(level=None, fmt=None, log_file=None, console=True):
    """Configure the root package logger. Idempotent; called from main()
    and lazily on first log()."""
    level = (level or config.LOG_LEVEL).upper()
    fmt = (fmt or config.LOG_FORMAT).lower()
    log_file = log_file or config.LOG_FILE
    _logger.setLevel(getattr(logging, level, logging.INFO))
    _logger.handlers.clear()
    _logger.propagate = False
    ctx = _ContextFilter()
    try:
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=2)
        fh.setFormatter(_JsonFormatter() if fmt == "json" else _text_formatter())
        fh.addFilter(ctx)
        _logger.addHandler(fh)
    except OSError:
        pass  # unwritable sink must never take the server down
    if console:
        import sys

        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(_text_formatter())
        ch.addFilter(ctx)
        _logger.addHandler(ch)
    _configured.set()
    return _logger


def log(msg, level=logging.INFO, **fields):
    """Compatibility entry point used across the codebase: `log("...")`.

    Text records keep the familiar `HH:MM:SS LEVEL [rid] msg` shape;
    extra keyword fields are appended (text) or included (json)."""
    if not _configured.is_set():
        setup()
    if fields:
        extra = " " + " ".join(f"{k}={v}" for k, v in fields.items())
        msg = f"{msg}{extra}"
    _logger.log(level, msg)


def event(level, name, **fields):
    """Structured event: `event("info", "ws_close", peer=..., bytes=...)`."""
    log(name, level=level, **fields)


def debug(msg, **fields):
    log(msg, level=logging.DEBUG, **fields)


def warning(msg, **fields):
    log(msg, level=logging.WARNING, **fields)


def error(msg, **fields):
    log(msg, level=logging.ERROR, **fields)


def log_file_tail(lines=40, log_file=None):
    """Last `lines` lines of the sink — used in test failure reports (#34)."""
    path = log_file or config.LOG_FILE
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return "(log file unavailable: %s)" % path


def rotating_files(log_file=None):
    """Files the sink rotates through (for ops docs and tests)."""
    path = log_file or config.LOG_FILE
    return [path] + [f"{path}.{i}" for i in (1, 2)] if os.path.exists(path) else [path]
