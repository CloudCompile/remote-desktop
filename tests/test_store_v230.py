"""v2.3.0 store tests (#66 #67 #69): backend detection, serial queue,
log tails, cancel, and the curated collections."""

import json
import threading
import time

import pytest

from remote_desktop import config, store


class FakeBackend(store.StoreBackend):
    name = "fake"
    env_name = "fake-get"

    @property
    def supported(self):
        return True

    def capabilities(self):
        return {"search": True, "install": True, "remove": True, "update": True, "status": True}

    def is_installed(self, pkg):
        return pkg == "installed-pkg"


@pytest.fixture
def fresh_queue(monkeypatch):
    monkeypatch.setattr(store, "_queue", [])
    monkeypatch.setattr(store, "_JOBS", {})
    monkeypatch.setattr(store, "_worker_running", False)  # worker thread spawns on demand


def test_valid_pkg_name():
    assert store.valid_pkg_name("vlc")
    assert store.valid_pkg_name("librewolf-ish.2")
    assert not store.valid_pkg_name("../evil")
    assert not store.valid_pkg_name("a b")
    assert not store.valid_pkg_name("under_score")  # not valid in Debian names
    assert not store.valid_pkg_name("")
    assert not store.valid_pkg_name(None)


def test_backend_detection_prefers_apt(monkeypatch):
    monkeypatch.setattr(store, "_BACKENDS", (store.AptBackend(), store.DnfBackend(), store.PacmanBackend()))
    store.reset_backend_cache()
    monkeypatch.setattr(
        "shutil.which", lambda name, path=None: "/usr/bin/" + name if name in ("apt-get", "dnf") else None
    )
    info = store.backend_info()
    assert info["backend"] == "apt" and info["supported"] is True
    assert info["capabilities"]["install"] and info["capabilities"]["remove"]
    store.reset_backend_cache()


def test_backend_stub_detected_unsupported(monkeypatch):
    monkeypatch.setattr(store, "_BACKENDS", (store.DnfBackend(),))
    store.reset_backend_cache()
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/usr/bin/dnf" if name == "dnf" else None)
    info = store.backend_info()
    assert info["backend"] == "dnf" and info["supported"] is False
    assert not info["capabilities"]["install"]
    assert "not implemented" in info["error"]
    store.reset_backend_cache()


def test_backend_none(monkeypatch):
    monkeypatch.setattr(store, "_BACKENDS", ())
    store.reset_backend_cache()
    info = store.backend_info()
    assert info["backend"] == "none" and info["supported"] is False
    store.reset_backend_cache()


def test_unsupported_backend_refuses_operations(monkeypatch):
    monkeypatch.setattr(store, "detect_backend", lambda: store.DnfBackend())  # unsupported
    assert store.install_pkg("vlc")["ok"] is False
    assert store.remove_pkg("vlc")["ok"] is False
    assert store.update_pkg("vlc")["ok"] is False
    assert store.store_search("vlc") == []


def test_apt_commands():
    be = store.AptBackend()
    assert be.install_cmd("vlc") == ["sudo", "apt-get", "install", "-y", "vlc"]
    assert be.remove_cmd("vlc") == ["sudo", "apt-get", "remove", "-y", "vlc"]
    assert be.update_cmd("vlc") == ["sudo", "apt-get", "install", "--only-upgrade", "-y", "vlc"]


def test_queue_runs_one_at_a_time(monkeypatch, fresh_queue):
    monkeypatch.setattr(store, "_supported_or_none", lambda: FakeBackend())
    active = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def fake_run_job(job):
        with lock:
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
        time.sleep(0.15)
        with lock:
            active["n"] -= 1
        job["state"] = "done"

    monkeypatch.setattr(store, "_run_job", fake_run_job)
    for pkg in ("a", "b", "c"):
        r = store.install_pkg(pkg)
        assert r["ok"] is True
    deadline = time.time() + 10
    while time.time() < deadline:
        with store._queue_lock:
            if not store._queue:
                break
        time.sleep(0.05)
    assert active["peak"] == 1  # strictly serial (#66)


def test_queue_position_reported(monkeypatch, fresh_queue):
    monkeypatch.setattr(store, "_supported_or_none", lambda: FakeBackend())
    monkeypatch.setattr(store, "_run_job", lambda job: time.sleep(5))  # park the worker
    store.install_pkg("first")
    store.install_pkg("second")
    st = store.install_status("second")
    assert st["installing"] is True and st["queue_pos"] == 2
    store.cancel_pkg("second")
    st = store.install_status("second")
    assert st["queue_pos"] is None and st["state"] == "cancelled"


def test_install_status_log_tail(tmp_path, monkeypatch, fresh_queue):
    logf = tmp_path / "job.log"
    logf.write_text("".join(f"line{i}\n" for i in range(40)))
    monkeypatch.setattr(
        store,
        "_JOBS",
        {"vlc": {"pkg": "vlc", "op": "install", "state": "running", "ts": time.time(), "log": str(logf)}},
    )
    st = store.install_status("vlc")
    assert len(st["log"]) == store.LOG_TAIL_LINES
    assert st["log"][-1] == "line39"
    assert st["installing"] is True


def test_install_status_persisted_job_interrupted(tmp_path, monkeypatch, fresh_queue):
    store.reset_backend_cache()
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"pkg": "vlc", "op": "install", "state": "running", "ts": time.time(), "log": "x"}]))
    monkeypatch.setattr(store, "JOBS_FILE", jobs)
    monkeypatch.setattr(store, "_JOBS", {})
    store.restore_jobs()
    assert store.install_status("vlc")["state"] == "interrupted"


def test_duplicate_job_refused(monkeypatch, fresh_queue):
    monkeypatch.setattr(store, "_supported_or_none", lambda: FakeBackend())
    monkeypatch.setattr(store, "_run_job", lambda job: time.sleep(5))
    assert store.install_pkg("dup")["ok"] is True
    r = store.install_pkg("dup")
    assert r["ok"] is False and "already has a" in r["error"]
    assert store.cancel_pkg("dup")["ok"] is True


def test_collections_shipped_and_filtered():
    cols = store.collections()
    assert len(cols) == 3
    for c in cols:
        assert len(c["apps"]) == 5
        assert all(store.valid_pkg_name(a["pkg"]) for a in c["apps"])
    # override file replaces the shipped catalog
    # (collections() reads config.STATE_DIR first — tested via tmp copy)


def test_collections_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    (tmp_path / "collections.json").write_text(
        json.dumps(
            {"collections": [{"id": "mine", "label": "Mine", "apps": [{"pkg": "foo", "label": "Foo", "desc": ""}]}]}
        )
    )
    cols = store.collections()
    assert len(cols) == 1 and cols[0]["id"] == "mine"


def test_collections_invalid_pkgs_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    (tmp_path / "collections.json").write_text(
        json.dumps(
            {
                "collections": [
                    {
                        "id": "x",
                        "label": "X",
                        "apps": [
                            {"pkg": "ok1", "label": "Ok", "desc": ""},
                            {"pkg": "../evil", "label": "Evil", "desc": ""},
                        ],
                    }
                ]
            }
        )
    )
    cols = store.collections()
    assert [a["pkg"] for a in cols[0]["apps"]] == ["ok1"]
