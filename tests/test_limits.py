"""Concurrency limit tests (#28)."""


class FakeHandler:
    def __init__(self):
        self.headers = {}
        self.sent = None

    def _json(self, code, obj, extra=None):
        self.sent = (code, obj, extra)


def test_acquire_release_counts():
    from remote_desktop import limits

    before = limits.counts()["audio"]
    assert limits.acquire("audio") is True
    assert limits.counts()["audio"] == before + 1
    limits.release("audio")
    assert limits.counts()["audio"] == before


def test_release_never_goes_negative():
    from remote_desktop import limits

    for _ in range(3):
        limits.release("media_in")
    assert limits.counts()["media_in"] == 0


def test_full_and_gate_503_with_retry_after():
    from remote_desktop import limits

    h = FakeHandler()
    before = limits.counts()["ws"]
    # fill the ws pool
    taken = []
    while limits.acquire("ws"):
        taken.append(True)
    assert limits.full("ws") is True
    assert limits.gate(h, "ws", "websocket proxies") is False
    code, obj, extra = h.sent
    assert code == 503
    assert extra["Retry-After"] == str(limits.RETRY_AFTER)
    assert "capacity" in obj["error"]
    for _ in taken:
        limits.release("ws")
    assert limits.counts()["ws"] == before


def test_gate_passes_when_capacity():
    from remote_desktop import limits

    h = FakeHandler()
    assert limits.gate(h, "media_in", "device-in streams") is True
    limits.release("media_in")


def test_caps_reflect_config():
    from remote_desktop import config, limits

    caps = limits.caps()
    assert caps["connections"] == config.MAX_CONNECTIONS
    assert caps["audio"] == config.MAX_AUDIO
    assert caps["media_in"] == config.MAX_MEDIA_IN


def test_snapshot_shape():
    from remote_desktop import limits

    snap = limits.snapshot()
    assert set(snap["caps"]) == {"connections", "ws", "audio", "media_in"}
    assert set(snap["active"]) == set(snap["caps"])
