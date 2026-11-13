"""VNC websocket proxy: /websockify — RFC6455 websocket to VNC TCP.

Ownership: the client-side websocket for the VNC stream. Design notes in
ADR-0003 (websocket-to-TCP proxy) and ADR-0001 (one process, one port).

Reliability (#29, #39):
- Backpressure: client->upstream bytes buffer through a bounded
  bytearray; a peer that lets its buffer exceed RD_WS_HIGH_WATER
  (default 4 MiB), a frame larger than RD_WS_MAX_MESSAGE, or one that is
  not writable for RD_WS_SEND_TIMEOUT seconds is dropped, so a slow
  mobile client can never balloon server memory. Upstream->client bytes
  are forwarded immediately (blocking write, bounded by kernel buffers)
  so no unbounded internal queue exists in that direction either.
- Buffer reuse: the pending buffers are bytearrays reused across frames
  instead of per-frame allocations.
- Reconnect: if the VNC (upstream) connection dies while the client
  websocket is still open, the proxy retries connecting up to
  RD_VNC_RETRIES times with short backoff. Client frames are buffered
  during the window and sent in order to whichever upstream connection
  is current — no bytes are replayed out of order. If every attempt
  fails, the client is closed with a clear close code (1013).
- The UI-side "reconnecting" state is owned by web/ (noVNC reconnects at
  a higher level); this module just closes the client cleanly on give-up.
"""

import select
import socket
import threading
import time

from . import config, limits, logging as log, ws

_stats_lock = threading.Lock()
_stats = {
    "active": 0,
    "opened": 0,
    "closed": 0,
    "tx_bytes": 0,
    "rx_bytes": 0,
    "dropped_bytes": 0,
    "slow_drops": 0,
    "reconnects": 0,
}

_live_lock = threading.Lock()
_live = set()  # handlers with an upgraded client socket (shutdown support, #25)

_PEEK = 65536  # upstream read chunk
_SELECT_TICK = 0.5  # loop tick
_BACKOFF_MAX = 4.0


def stats_snapshot():
    with _stats_lock:
        return dict(_stats)


def _bump(key, n=1):
    with _stats_lock:
        _stats[key] += n


class _Pending:
    """Bounded FIFO of bytes with buffer reuse (#29)."""

    __slots__ = ("buf", "limit")

    def __init__(self, limit):
        self.buf = bytearray()
        self.limit = limit

    def extend(self, data):
        """Append data; returns False (buffer unchanged) when the append
        would cross the high-water mark — the caller drops the peer."""
        if len(self.buf) + len(data) > self.limit:
            return False
        self.buf += data
        return True

    def prepend(self, data):
        """Put bytes back at the FRONT (push-back after a failed upstream
        send, so a reconnect retries them first — order preserved)."""
        self.buf[:0] = data

    def take(self):
        """Detach the whole buffer (reusing its allocation); None if empty."""
        if not self.buf:
            return None
        out = bytes(self.buf)
        self.buf.clear()
        return out

    def __len__(self):
        return len(self.buf)


def _connect(timeout=5):
    return socket.create_connection((config.VNC_HOST, config.VNC_PORT), timeout=timeout)


def _client_writable(conn, timeout):
    try:
        _, w, _ = select.select([], [conn], [], timeout)
        return bool(w)
    except Exception:
        return False


def _send_to_client(handler, data):
    """Write one frame to the client websocket, bounded by the send
    timeout (#29). Returns False when the client is gone or too slow."""
    if not data:
        return True
    if not _client_writable(handler.connection, config.WS_SEND_TIMEOUT):
        return False
    try:
        handler.wfile.write(data)
        handler.wfile.flush()
    except Exception:
        return False
    _bump("tx_bytes", len(data))
    return True


def _pump(handler, target, c2s, c2c, c2s_lock, stop, last_activity):
    """Move bytes for one upstream connection until it or the client ends.

    Returns 'upstream' when the upstream connection died (client may be
    kept for a reconnect), or 'client'/'idle' when the client side ends.
    Single-writer: only this thread writes to the client socket and the
    upstream socket, so byte order is preserved in both directions.
    """
    while not stop.is_set():
        # 1. flush buffered client frames to the upstream, in order
        with c2s_lock:
            up = c2s.take()
        if up is not None:
            try:
                target.sendall(up)
            except Exception:
                # upstream died mid-send: push the bytes back so the
                # reconnect sends them first — nothing is lost, order kept
                with c2s_lock:
                    c2s.prepend(up)
                return "upstream"
            _bump("rx_bytes", len(up))
            last_activity[0] = time.time()
        # 2. flush control replies (pongs/close) to the client
        ctrl = c2c.take()
        if ctrl is not None and not _send_to_client(handler, ctrl):
            stop.set()
            return "client"
        if stop.is_set():
            return "client"
        # 3. forward upstream bytes to the client
        try:
            ready, _, _ = select.select([target], [], [], _SELECT_TICK)
            if ready:
                data = target.recv(_PEEK)
                if not data:
                    return "upstream"
                last_activity[0] = time.time()
                if not _send_to_client(handler, ws._ws_frame(data)):
                    stop.set()
                    return "client"
        except Exception:
            return "upstream"
        # 4. idle supervision (#28): close silent connections
        if time.time() - last_activity[0] > config.WS_IDLE_TIMEOUT:
            log.log("ws proxy: idle timeout", peer=handler.client_address[0])
            return "idle"
    return "client"


def websocket_proxy(handler):
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or "upgrade" not in (handler.headers.get("Connection", "") + "").lower():
        handler._json(400, {"error": "expected websocket upgrade"})
        return
    if not limits.gate(handler, "ws", "websocket proxies"):
        return
    try:
        target = _connect()
    except Exception as e:
        limits.release("ws")
        log.log(f"VNC connect failed ({config.VNC_HOST}:{config.VNC_PORT}): {e}")
        handler._json(502, {"error": f"cannot reach VNC server {config.VNC_HOST}:{config.VNC_PORT}"})
        return

    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", ws._accept_key(key))
    handler.end_headers()
    handler.close_connection = True
    with _stats_lock:
        _stats["opened"] += 1
        _stats["active"] += 1
    with _live_lock:
        _live.add(handler)
    log.log(f"ws proxy open  {handler.client_address[0]} -> {config.VNC_HOST}:{config.VNC_PORT}")

    stop = threading.Event()
    c2s = _Pending(config.WS_HIGH_WATER)  # client -> upstream (buffered during reconnects)
    c2c = _Pending(config.WS_HIGH_WATER)  # control replies back to the client (pongs)
    c2s_lock = threading.Lock()
    reader = ws._WsReader(handler.rfile)

    def client_to_target():
        """Reader thread: parse client frames into c2s (bounded). All socket
        writes happen on the pump thread, so frame order is preserved."""
        try:
            while not stop.is_set():
                fr = reader.read()
                if fr is None:
                    break
                op, payload = fr
                if op == 0x8:  # close
                    c2c.extend(ws._ws_close_frame())
                    break
                if op == 0x9:  # ping -> pong (echoed to client)
                    if not c2c.extend(bytes((0x8A, len(payload))) + payload):
                        with _stats_lock:
                            _stats["slow_drops"] += 1
                        break
                    continue
                if op in (0x1, 0x2, 0x0) and payload:  # text/binary/continuation
                    with c2s_lock:
                        ok = c2s.extend(payload)
                    if not ok:
                        with _stats_lock:
                            _stats["dropped_bytes"] += len(payload)
                            _stats["slow_drops"] += 1
                        log.log(
                            "ws proxy: client buffer overflow; dropping slow client", peer=handler.client_address[0]
                        )
                        break
        except Exception:
            pass
        finally:
            stop.set()

    t = threading.Thread(target=client_to_target, daemon=True)
    t.start()

    retries_left = config.VNC_RETRIES
    backoff = config.VNC_BACKOFF
    last_activity = [time.time()]
    try:
        while True:
            status = _pump(handler, target, c2s, c2c, c2s_lock, stop, last_activity)
            if status != "upstream" or stop.is_set():
                break
            # upstream lost while the client is still open -> reconnect (#39)
            try:
                target.close()
            except Exception:
                pass
            new_target = None
            attempt = 0
            while attempt < retries_left and not stop.is_set():
                with _stats_lock:
                    _stats["reconnects"] += 1
                log.log(
                    f"ws proxy: upstream lost, reconnecting in {backoff:.2f}s (attempt {attempt + 1}/{retries_left})"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                try:
                    new_target = _connect()
                    break
                except Exception as e:
                    log.log(f"ws proxy: reconnect attempt failed: {e}")
                    attempt += 1
            if new_target is None:
                # permanent failure: close the client with a clear message
                _send_to_client(handler, ws._ws_close_frame(1013, b"vnc-unavailable"))
                log.log("ws proxy: upstream permanently unavailable; closing client", peer=handler.client_address[0])
                break
            target = new_target
            last_activity[0] = time.time()
            # c2s keeps buffering meanwhile; order is preserved because the
            # pump is the only writer and flushes the buffer before reads.
    except Exception:
        pass
    finally:
        stop.set()
        try:
            target.close()
        except Exception:
            pass
        t.join(timeout=2)
        with _stats_lock:
            _stats["active"] -= 1
            _stats["closed"] += 1
        with _live_lock:
            _live.discard(handler)
        limits.release("ws")
        log.log(f"ws proxy close {handler.client_address[0]}")


def shutdown_all():
    """Force-close every live client socket so proxy threads wake and exit
    during graceful shutdown (#25)."""
    with _live_lock:
        handlers = list(_live)
    for h in handlers:
        try:
            h.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            h.connection.close()
        except Exception:
            pass
    return len(handlers)
