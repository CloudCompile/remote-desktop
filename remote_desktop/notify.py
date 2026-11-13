"""Login notifications and suspicious-activity alerts (#123).

Ownership: per-user "new device / new IP" notifications (content-free:
time, device label, coarse IP — never passwords, cookies or clipboard
data, #24/#123), failed-attempt summaries read back from the audit log,
and optional webhook delivery (``RD_NOTIFY_WEBHOOK``). Delivery failure
NEVER blocks login: the notification is recorded locally and the webhook
is attempted on a background thread with a hard timeout.

Disable everything with ``RD_NOTIFY=0``; ``POST /api/notify/test``
injects a test event so operators can verify delivery.
"""

import hashlib
import json
import os
import threading
import time
from urllib import request as urlrequest

from . import config, logging as log

NOTIFY_MAX = 20  # unread notifications kept per user
DEVICES_MAX = 50  # known devices remembered per user
WEBHOOK_TIMEOUT = 3.0

_lock = threading.Lock()
_state = None  # {"users": {user: {"devices": {h: {...}}, "unread": [...]}}}


def _path():
    from . import users

    return os.path.join(users.users_dir(), "notify.json")


def _load():
    global _state
    if _state is not None:
        return _state
    try:
        with open(_path()) as f:
            raw = json.load(f)
        _state = raw if isinstance(raw, dict) and isinstance(raw.get("users"), dict) else {"users": {}}
    except Exception:
        _state = {"users": {}}
    return _state


def _save():
    import os

    try:
        path = _path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _device_key(handler):
    ua = (handler.headers.get("User-Agent", "") if handler else "")[:200]
    return hashlib.sha256(ua.encode()).hexdigest()[:16]


def on_login(user, handler=None):
    """Record a login; notify when the device (or its IP) is new (#123).
    Called AFTER the session is created; must never raise."""
    if not config.NOTIFY_ENABLED or not user:
        return None
    try:
        from . import auth

        ip = auth._client_ip(handler) if handler else "-"
        label = auth.device_label(handler.headers.get("User-Agent", "") if handler else "")
        dkey = _device_key(handler)
        ip_prefix = ".".join(str(ip).split(".")[:3]) if ip.count(".") == 3 else ip
        with _lock:
            st = _load()
            u = st["users"].setdefault(user, {"devices": {}, "unread": []})
            dev = u["devices"].get(dkey)
            reasons = []
            if dev is None:
                reasons.append("new-device")
                dev = u["devices"][dkey] = {"label": label, "first": int(time.time()), "last": 0, "ips": []}
            dev["last"] = int(time.time())
            if ip_prefix and ip_prefix not in dev["ips"]:
                if dev["ips"]:
                    reasons.append("new-ip")
                dev["ips"] = (dev["ips"] + [ip_prefix])[-5:]
            if reasons:
                u["unread"] = (
                    u["unread"]
                    + [
                        {
                            "ts": int(time.time()),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                            "reason": "+".join(reasons),
                            "device": label,
                            "ip_prefix": ip_prefix,
                        }
                    ]
                )[-NOTIFY_MAX:]
            # bound remembered devices
            if len(u["devices"]) > DEVICES_MAX:
                for k in sorted(u["devices"], key=lambda k: u["devices"][k].get("last") or 0)[
                    : len(u["devices"]) - DEVICES_MAX
                ]:
                    del u["devices"][k]
            _save()
        if reasons:
            _webhook(user, reasons, ip_prefix, label)
        return reasons or None
    except Exception as e:
        log.log(f"notify: on_login failed (ignored): {e}")
        return None


def unread(user):
    """Pending notifications for one user (marks them seen, #123)."""
    if not config.NOTIFY_ENABLED or not user:
        return []
    with _lock:
        st = _load()
        u = st["users"].get(user) or {}
        out = list(u.get("unread") or [])
        if out:
            u["unread"] = []
            _save()
    return out


def known_devices(user):
    """Recognised devices for the security settings view."""
    with _lock:
        st = _load()
        u = st["users"].get(user) or {}
        return [
            {"label": d.get("label"), "first": d.get("first"), "last": d.get("last"), "ips": d.get("ips", [])}
            for d in (u.get("devices") or {}).values()
        ]


def failed_attempts(user, window=24 * 3600):
    """Failed-login count for one user from the audit log (#123): the
    security settings surface shows it without exposing any secrets."""
    try:
        from . import audit

        cutoff = time.time() - window
        return sum(
            1
            for e in audit.read(kind=audit.AUTH, q=str(user), limit=1000)
            if e.get("action") == "login" and e.get("result") == "failed" and (e.get("ts") or 0) >= cutoff
        )
    except Exception:
        return 0


def test_event(user):
    """Inject a test notification (admin check-up, #123)."""
    with _lock:
        st = _load()
        u = st["users"].setdefault(user, {"devices": {}, "unread": []})
        u["unread"] = (
            u["unread"]
            + [
                {
                    "ts": int(time.time()),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                    "reason": "test",
                    "device": "test event",
                    "ip_prefix": "-",
                }
            ]
        )[-NOTIFY_MAX:]
        _save()


def _webhook(user, reasons, ip_prefix, label):
    """Fire-and-forget JSON webhook (#123): failures are logged and ignored."""
    url = config.NOTIFY_WEBHOOK
    if not url:
        return
    payload = {
        "event": "login",
        "user": user,
        "reasons": reasons,
        "device": label,
        "ip_prefix": ip_prefix,
        "time": int(time.time()),
    }  # content-free: no passwords, cookies or clipboard data

    def _post():
        try:
            req = urlrequest.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "X-Remote-Desktop-Event": "login"},
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
                r.read(64)
        except Exception as e:
            log.log(f"notify: webhook delivery failed (ignored): {e}")

    threading.Thread(target=_post, daemon=True, name="notify-webhook").start()
