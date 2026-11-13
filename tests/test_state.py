"""Runtime state tests: pins, display state, versioned backups (#30, #31)."""

import io
import json
import os
import tarfile
import time

import pytest

from remote_desktop import state


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """Isolated pins file + clip history + state dir."""
    monkeypatch.setattr(state, "PINS_FILE", str(tmp_path / "state" / ".pins.json"))
    monkeypatch.setattr(state._clipboard, "CLIP_HISTORY_FILE", str(tmp_path / "state" / ".cliphistory.json"))
    monkeypatch.setattr(state.config, "STATE_DIR", str(tmp_path / "xdg" / "remote-desktop"))
    monkeypatch.setattr(state, "STATE_VERSION", state.STATE_VERSION)  # no-op; keep namespace explicit
    os.makedirs(tmp_path / "state", exist_ok=True)
    return tmp_path


# ------------------------------------------------------------------ pins


def test_pins_roundtrip(state_home):
    state.save_pins(["a.desktop", "b.desktop"])
    assert state.load_pins() == ["a.desktop", "b.desktop"]
    state.save_pins([123, "ok", "x" * 500])
    assert state.load_pins() == ["ok"]  # non-strings dropped, long ids trimmed


def test_pins_missing_file(state_home):
    assert state.load_pins() == []


# ---------------------------------------------------------- display state


def test_display_state_roundtrip_and_bounds(state_home):
    d = state.save_display_state(1024, 768, 120)
    assert d["geometry"] == "1024x768" and d["dpi"] == 120
    assert state.load_display_state()["geometry"] == "1024x768"


def test_display_state_clamped(state_home):
    d = state.save_display_state(1, 99999, 96)
    assert d["geometry"] == f"{state.GEOMETRY_MIN[0]}x{state.GEOMETRY_MAX[1]}"


def test_display_state_missing(state_home):
    assert state.load_display_state() is None


# ---------------------------------------------------------------- backups


def _make_live_state(state_home):
    state.save_pins(["pinned.desktop"])
    with open(state._clipboard.CLIP_HISTORY_FILE, "w") as f:
        json.dump([{"id": "x", "text": "copied", "time": 1, "source": "desktop"}], f)


def test_snapshot_creates_versioned_dir_with_manifest(state_home):
    _make_live_state(state_home)
    bdir = state.snapshot("test")
    assert bdir and os.path.isdir(bdir)
    assert "v1-" in os.path.basename(bdir)
    m = json.load(open(os.path.join(bdir, "manifest.json")))
    assert m["state_version"] == state.STATE_VERSION
    assert set(m["files"]) == {"pins", "cliphistory"}
    for f in m["files"].values():
        assert f["sha256"]


def test_snapshot_without_state_files(state_home):
    assert state.snapshot("empty") is not None  # manifest only


def test_backup_pruned_to_keep_5(state_home, monkeypatch):
    monkeypatch.setattr(state, "BACKUP_KEEP", 5)
    for i in range(7):
        # distinct dir names: freeze a counter into the reason
        d = state.snapshot(f"t{i}")
        assert d
    backups = os.listdir(state.backups_dir())
    assert len(backups) == 5
    assert sorted(backups) == sorted(backups)[-5:]  # newest kept


def test_list_backups(state_home):
    state.snapshot("list")
    rows = state.list_backups()
    assert len(rows) == 1
    assert rows[0]["manifest"]["reason"] == "list"


# ------------------------------------------------------- export / import


def test_export_import_roundtrip(state_home, tmp_path):
    _make_live_state(state_home)
    archive = tmp_path / "state.tar.gz"
    r = state.export_state(str(archive))
    assert r["ok"] is True
    assert archive.is_file()
    assert (os.stat(str(archive)).st_mode & 0o777) == 0o600  # restrictive perms
    # wipe live state, then import
    os.remove(state.PINS_FILE)
    os.remove(state._clipboard.CLIP_HISTORY_FILE)
    r = state.import_state(str(archive))
    assert r["ok"] is True, r
    assert sorted(r["imported"]) == ["cliphistory", "pins"]
    assert state.load_pins() == ["pinned.desktop"]
    data = json.load(open(state._clipboard.CLIP_HISTORY_FILE))
    assert data[0]["text"] == "copied"


def test_export_excludes_secrets(state_home, tmp_path, monkeypatch):
    _make_live_state(state_home)
    monkeypatch.setattr(state.config, "PASS_FILE", str(tmp_path / ".passhash"))
    (tmp_path / ".passhash").write_text("$6$secret$")
    archive = tmp_path / "state.tar.gz"
    assert state.export_state(str(archive))["ok"]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert ".passhash" not in names
    assert set(names) <= {".pins.json", ".cliphistory.json", "manifest.json"}


def test_import_refuses_newer_schema(state_home, tmp_path):
    archive = tmp_path / "future.tar.gz"
    manifest = io.BytesIO(json.dumps({"state_version": state.STATE_VERSION + 1}).encode())
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = manifest.getbuffer().nbytes
        tar.addfile(info, manifest)
    before = state.load_pins()
    r = state.import_state(str(archive))
    assert r["ok"] is False
    assert "newer" in r["error"]
    assert state.load_pins() == before  # live state untouched


def test_import_refuses_corrupt(state_home, tmp_path):
    archive = tmp_path / "corrupt.tar.gz"
    manifest = io.BytesIO(json.dumps({"state_version": 1}).encode())
    payload = b"{not json"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = manifest.getbuffer().nbytes
        tar.addfile(info, manifest)
        info2 = tarfile.TarInfo(".pins.json")
        info2.size = len(payload)
        tar.addfile(info2, io.BytesIO(payload))
    r = state.import_state(str(archive))
    assert r["ok"] is False
    assert "rejected" in r["error"]


def test_import_missing_archive(state_home, tmp_path):
    assert state.import_state(str(tmp_path / "nope.tar.gz"))["ok"] is False
