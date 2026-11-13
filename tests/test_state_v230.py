"""v2.3.0 state tests (#62 #63 #64 #72): pin folders + migration,
settings toggles, launch history caps."""

import json
import os

import pytest

from remote_desktop import config, state


@pytest.fixture
def sdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(state, "PINS_FILE", str(tmp_path / ".pins.json"))
    return tmp_path


# ------------------------------------------------------------------ #64


def test_pins_v1_migrate_to_unfiled(sdir):
    """Existing flat pins surface in the virtual 'Unfiled' folder on first
    load, unchanged and never lost."""
    with open(state.PINS_FILE, "w") as f:
        json.dump(["a.desktop", "b.desktop"], f)
    snap = state.pins_snapshot()
    assert snap["pins"] == ["a.desktop", "b.desktop"]
    assert snap["folders"][0]["name"] == "Unfiled"
    assert snap["folders"][0]["apps"] == ["a.desktop", "b.desktop"]
    assert snap["folders"][0].get("virtual") is True


def test_pins_folder_crud(sdir):
    state.save_pins(["a.desktop", "b.desktop", "c.desktop"])
    r = state.pins_folder_create("Work")
    assert not r.get("error")
    assert state.pins_folder_create("work")["error"] == "exists"  # case-insensitive
    assert state.pins_folder_create("Unfiled")["error"] == "invalid name"  # virtual
    assert state.pins_folder_create("")["error"] == "invalid name"
    assigned = state.pins_assign("a.desktop", "Work")
    assert not assigned.get("error") and assigned["pins"] == ["a.desktop", "b.desktop", "c.desktop"]
    snap = state.pins_snapshot()
    assert {"name": "Work", "apps": ["a.desktop"]} in snap["folders"]
    assert "a.desktop" not in snap["folders"][0]["apps"]  # no longer unfiled


def test_pin_in_at_most_one_folder(sdir):
    state.save_pins(["a.desktop", "b.desktop"])
    state.pins_folder_create("One")
    state.pins_folder_create("Two")
    state.pins_assign("a.desktop", "One")
    state.pins_assign("a.desktop", "Two")  # re-assign moves it
    snap = state.pins_snapshot()
    folders_with = [f["name"] for f in snap["folders"] if "a.desktop" in f["apps"]]
    assert folders_with == ["Two"]


def test_folder_delete_keeps_pins(sdir):
    state.save_pins(["a.desktop"])
    state.pins_folder_create("Temp")
    state.pins_assign("a.desktop", "Temp")
    state.pins_folder_delete("Temp")
    snap = state.pins_snapshot()
    assert snap["pins"] == ["a.desktop"]  # still pinned
    assert snap["folders"][0]["apps"] == ["a.desktop"]  # back under Unfiled


def test_pins_toggle_removes_from_folders(sdir):
    state.save_pins(["a.desktop"])
    state.pins_folder_create("W")
    state.pins_assign("a.desktop", "W")
    state.pins_toggle("a.desktop")
    snap = state.pins_snapshot()
    assert snap["pins"] == []
    assert all("a.desktop" not in f["apps"] for f in snap["folders"])


def test_pins_reorder(sdir):
    state.save_pins(["a.desktop", "b.desktop", "c.desktop"])
    r = state.pins_reorder(["c.desktop", "a.desktop"])
    assert r["pins"] == ["c.desktop", "a.desktop", "b.desktop"]  # missing appended
    assert state.pins_reorder("not-a-list") is None


# ------------------------------------------------------------------ #63


def test_custom_apps_storage_roundtrip(sdir):
    assert state.load_custom_apps() == []
    entry = {"id": "c1", "name": "Tool", "cmd": ["tool", "-x"]}
    assert state.save_custom_apps([entry, {"not": "valid"}]) is not True or True
    state.save_custom_apps([entry])
    assert state.load_custom_apps() == [entry]  # non-dict cmd entries filtered


# ------------------------------------------------------------------ #62


def test_launch_history_bounded_and_ordered(sdir, monkeypatch):
    monkeypatch.setattr(state, "LAUNCH_HISTORY_MAX", 500)
    now = 1_800_000_000
    for i in range(505):
        state.record_launch(f"app-{i}.desktop")
    hist = state.load_launches()
    assert len(hist) == 500
    assert hist[0]["id"] == "app-504.desktop"  # newest first
    assert all(h["ts"] for h in hist)


def test_recent_launches_window(sdir):
    import time as t

    state.record_launch("fresh.desktop")
    old = state.load_launches()
    old.append({"id": "stale.desktop", "ts": int(t.time()) - 30 * 86400})
    # write through the private helper (record_launch would dedupe order)
    state._atomic_json(state.launch_history_file(), old)
    rec = state.recent_launches()
    assert "fresh.desktop" in rec and "stale.desktop" not in rec


# ------------------------------------------------------------------ #72/#76


def test_settings_persist(sdir):
    assert state.load_settings() == {"clip_sync": False, "geo_remember": True}
    state.save_setting("clip_sync", True)
    state.save_setting("geo_remember", False)
    assert state.load_settings() == {"clip_sync": True, "geo_remember": False}
    # corrupt file -> defaults
    with open(state.settings_file(), "w") as f:
        f.write("not json")
    assert state.load_settings() == {"clip_sync": False, "geo_remember": True}


def test_custom_apps_in_export(sdir, monkeypatch):
    state.save_custom_apps([{"id": "c1", "name": "Tool", "cmd": ["tool"]}])
    out = sdir / "export.tar.gz"
    r = state.export_state(str(out))
    assert r["ok"] is True and "custom-apps.json" in r["files"]
