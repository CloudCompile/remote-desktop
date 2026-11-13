"""Websocket (RFC6455) primitives.

Ownership: frame parsing/building, the upgrade handshake and pong helper.
The VNC proxy loop lives in vncproxy.py; device-in websocket endpoints in
media_in.py. Client messages must be masked and are size-capped
(RD_WS_MAX_MESSAGE); frames from the server are built by ws_frame().
"""

import base64
import hashlib
import struct

from . import config

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = config.WS_MAX_MESSAGE


def _read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class _WsReader:
    """Stateful RFC6455 client-message reader (#10): reassembles fragmented
    frames, tolerates interleaved ping/pong/close, enforces client masking
    and a hard size cap. read() returns (opcode, payload) for data messages,
    ping (0x9) and close (0x8); None on EOF, protocol error or oversize."""

    def __init__(self, rfile, max_message=WS_MAX_MESSAGE):
        self.rfile = rfile
        self.max_message = max_message
        self.msg_op = None
        self.buf = bytearray()

    def read(self):
        while True:
            hdr = self.rfile.read(2)
            if len(hdr) < 2:
                return None
            b1, b2 = hdr[0], hdr[1]
            op = b1 & 0x0F
            masked = bool(b2 & 0x80)
            fin = bool(b1 & 0x80)
            ln = b2 & 0x7F
            if ln == 126:
                ext = self.rfile.read(2)
                if len(ext) < 2:
                    return None
                ln = struct.unpack(">H", ext)[0]
            elif ln == 127:
                ext = self.rfile.read(8)
                if len(ext) < 8:
                    return None
                ln = struct.unpack(">Q", ext)[0]
            mk = self.rfile.read(4) if masked else b""
            if masked and len(mk) < 4:
                return None
            if ln > self.max_message or len(self.buf) + ln > self.max_message:
                return None
            data = _read_exact(self.rfile, ln) if ln else b""
            if data is None:
                return None
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if op == 0x8:  # close
                return 0x8, data
            if op in (0x9, 0xA):  # ping/pong (never fragmented)
                return op, data
            if not masked:  # clients must mask (#10)
                return None
            if op != 0x0 and self.msg_op is not None:  # new data frame mid-message
                return None
            if op == 0x0 and self.msg_op is None:  # stray continuation
                return None
            if self.msg_op is None:
                self.msg_op = op
            self.buf += data
            if fin:
                payload = bytes(self.buf)
                op_out = self.msg_op  # set above for any legal data frame
                self.msg_op, self.buf = None, bytearray()
                return op_out, payload


def _ws_read_frame(rfile, max_message=WS_MAX_MESSAGE):
    """Stateless convenience wrapper: read one complete client message from
    a fresh stream. Connection-long readers must hold a _WsReader so
    fragment state survives interleaved control frames."""
    return _WsReader(rfile, max_message).read()


def _ws_frame(data):
    """Wrap raw bytes as a binary server->client frame."""
    ln = len(data)
    if ln < 126:
        return bytes((0x82, ln)) + data
    if ln < 65536:
        return bytes((0x82, 126)) + struct.pack(">H", ln) + data
    return bytes((0x82, 127)) + struct.pack(">Q", ln) + data


def _ws_close_frame(code=1000, reason=b""):
    """Best-effort close frame carrying a status code + short reason."""
    payload = struct.pack(">H", code) + reason[:123]
    return bytes((0x88, len(payload))) + payload


def _accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_upgrade(handler):
    """Complete the RFC6455 upgrade for a server-side websocket endpoint.
    Returns False (and replies) if the request is not a websocket upgrade."""
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or "upgrade" not in (handler.headers.get("Connection", "") + "").lower():
        handler._json(400, {"error": "expected websocket upgrade"})
        return False
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", _accept_key(key))
    handler.end_headers()
    handler.close_connection = True
    return True


def ws_pong(handler, payload):
    try:
        handler.wfile.write(bytes((0x8A, len(payload))) + payload)
        handler.wfile.flush()
    except Exception:
        pass


# legacy aliases (the original single-file names)
_ws_upgrade = ws_upgrade
_ws_pong = ws_pong
