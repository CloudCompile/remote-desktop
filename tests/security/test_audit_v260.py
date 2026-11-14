"""Tamper-evident audit log (#113): chain, detection, rotation, privacy."""

import json
import os

import pytest

from remote_desktop import audit


@pytest.fixture
def alog(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_FILE", str(p))
    monkeypatch.setattr(audit, "_chains", {})  # fresh chain state per test
    return p


class H:
    """Minimal request-handler stand-in."""

    client_address = ("198.51.100.7", 4444)
    headers = {}
    rid = "r-abc"
    user = "admin"


def test_records_carry_actor_ip_target_result(alog):
    audit.event(audit.AUTH, "login", user="admin", handler=H(), target="1.2.3.4", result="ok")
    rec = audit.read()[0]
    assert rec["kind"] == "auth" and rec["action"] == "login"
    assert rec["user"] == "admin" and rec["ip"] == "198.51.100.7" and rec["rid"] == "r-abc"
    assert rec["target"] == "1.2.3.4" and rec["result"] == "ok"
    assert rec["prev"] == "0" * 64 and len(rec["h"]) == 64


def test_chain_verifies_and_detects_edits(alog):
    for i in range(5):
        audit.event(audit.FILES, "delete", user="admin", handler=H(), target=f"f{i}.txt")
    ok, first_bad, checked = audit.verify()
    assert ok and first_bad == -1 and checked == 5

    # edit an OLD record in place -> chain must break there
    lines = open(alog).read().splitlines()
    rec = json.loads(lines[1])
    rec["target"] = "tampered.txt"
    lines[1] = json.dumps(rec)
    open(alog, "w").write("\n".join(lines) + "\n")
    audit._chains.clear()
    ok, first_bad, checked = audit.verify()
    assert not ok and first_bad == 1


def test_chain_detects_deletion(alog):
    for i in range(5):
        audit.event(audit.FILES, "delete", user="admin", handler=H(), target=f"f{i}.txt")
    lines = open(alog).read().splitlines()
    del lines[2]  # drop one middle record
    open(alog, "w").write("\n".join(lines) + "\n")
    audit._chains.clear()
    ok, first_bad, _ = audit.verify()
    assert not ok and first_bad == 2


def test_rotation_keeps_recent_events(alog, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_MAX", 2048)
    monkeypatch.setattr(audit, "AUDIT_KEEP", 2)
    for i in range(80):
        audit.event(audit.FILES, "upload", user="admin", handler=H(), target=f"f{i}.txt", size=i)
    rotated = [f for f in os.listdir(tmp_dir(alog)) if f.startswith("audit.jsonl.")]
    assert rotated, "expected rotation generations"
    # RECENT events survive the rotation seam (#113 acceptance): the live
    # file plus the kept generations. With AUDIT_KEEP=2 the oldest
    # generation is legitimately pruned, so f0 is gone; f70 (rotated or
    # live) and f79 (live) must both still read back.
    targets = [r.get("target") for r in audit.read(limit=1000)]
    assert "f79.txt" in targets and "f70.txt" in targets and "f0.txt" not in targets
    # verify walks exactly the kept generations (AUDIT_KEEP=2 here): the
    # chain is intact across the seam for everything retained; pruned
    # generations are gone by design, so `checked` is the retained count.
    ok, first_bad, checked = audit.verify()
    assert ok and checked == len(audit.read(limit=10_000)) and checked < 80


def tmp_dir(path):
    return os.path.dirname(str(path))


def test_never_records_secrets(alog):
    # the event API records actions, never credential material (#113/#24)
    audit.event(audit.AUTH, "login", user="admin", handler=H(), result="failed")
    blob = open(alog).read()
    for banned in ("password", "cookie", "token=", "clipboard"):
        assert banned not in blob.lower()


def test_append_failure_never_raises(alog, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_FILE", "/proc/nope/audit.jsonl")
    monkeypatch.setattr(audit, "_chains", {})
    audit.event(audit.AUTH, "login", user="x")  # must not raise
    audit._chains.clear()
