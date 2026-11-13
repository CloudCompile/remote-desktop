"""v2.3.0 clipboard tests (#70 #71 #72): pinning, tags, search, image
storage, sync gating — with faked xclip output (mocked _run_xclip)."""

import json
import os
import threading
import time

import pytest

from remote_desktop import clipboard as cb
from remote_desktop import config, state


@pytest.fixture
def clip_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "CLIP_HISTORY_FILE", str(tmp_path / ".cliphistory.json"))
    monkeypatch.setattr(cb, "CLIP_IMAGE_DIR", str(tmp_path / "cache" / "remote-clipboard"))
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    cb._clip_history.clear()
    cb._clip_last_sel.update({"clipboard": None, "primary": None})
    yield tmp_path
    cb._clip_history.clear()


# ----------------------------------------------------------------- #70


def test_pin_survives_trim(clip_tmp):
    for n in range(cb.CLIP_HISTORY_MAX + 20):
        cb.clip_add(f"item {n}", "desktop")
    pinned = cb.clip_history()[0]["id"]
    assert cb.clip_pin(pinned, True) is True
    for n in range(cb.CLIP_HISTORY_MAX):
        cb.clip_add(f"more {n}", "desktop")
    hist = cb.clip_history()
    assert hist[0]["id"] == pinned  # pinned sorts first
    assert sum(1 for it in hist if it["id"] == pinned) == 1
    # unpinned portion still capped
    assert len([it for it in hist if not it["pinned"]]) <= cb.CLIP_HISTORY_MAX


def test_unpin_returns_to_retention(clip_tmp):
    cb.clip_add("temp", "desktop")
    cid = cb.clip_history()[0]["id"]
    cb.clip_pin(cid, True)
    cb.clip_pin(cid, False)
    for n in range(cb.CLIP_HISTORY_MAX + 5):
        cb.clip_add(f"push {n}", "desktop")
    assert all(it["id"] != cid for it in cb.clip_history())  # trimmed like normal


def test_search_matches_full_text_not_just_preview(clip_tmp):
    long_text = "needle" + "x" * 500  # far beyond the 160-char preview
    cb.clip_add(long_text, "desktop")
    hits = cb.clip_history(q="needle")
    assert len(hits) == 1 and hits[0]["len"] == len(long_text)
    assert cb.clip_history(q="no-such-needle") == []


def test_tag_filter_and_listing(clip_tmp):
    cb.clip_add("one", "desktop")
    cb.clip_add("two", "desktop")
    cid = cb.clip_history()[0]["id"]
    assert cb.clip_tag(cid, ["work", "important"]) is True
    assert cb.clip_tags() == ["work", "important"]
    assert cb.clip_tag(cid, "urgent, work") is True  # comma string accepted, replaces the set
    assert cb.clip_tags() == ["urgent", "work"]
    assert [it["preview"] for it in cb.clip_history(tag="WORK")] == ["two"]  # case-insensitive
    assert cb.clip_tag("missing", ["x"]) is False


def test_clear_preserves_pins(clip_tmp):
    cb.clip_add("keep me", "desktop")
    cid = cb.clip_history()[0]["id"]
    cb.clip_pin(cid, True)
    cb.clip_add("drop me", "desktop")
    cb.clip_clear()
    left = cb.clip_history()
    assert len(left) == 1 and left[0]["id"] == cid


def test_history_file_format_additive(clip_tmp):
    """v2.2-era entries (no pinned/tags/kind) load with defaults (#70)."""
    old = [
        {"id": "abc123", "text": "legacy item", "time": 1600000000, "source": "desktop"},
        {"bogus": True},
        {"id": "dup", "text": "x", "time": 1, "source": "desktop"},
        {"id": "dup", "text": "y", "time": 2, "source": "desktop"},
    ]
    with open(cb.CLIP_HISTORY_FILE, "w") as f:
        json.dump(old, f)
    cb._clip_load()
    hist = cb.clip_history()
    assert len(hist) == 2  # bad entry dropped, dup deduped (newest wins)
    legacy = next(it for it in hist if it["id"] == "abc123")
    assert legacy["pinned"] is False and legacy["tags"] == [] and legacy["kind"] == "text"


# ----------------------------------------------------------------- #71


def test_clip_add_image_stores_file_and_preview(clip_tmp, monkeypatch):
    monkeypatch.setattr(cb, "_image_preview", lambda path, raw: "data:image/png;base64,AAA")
    data = b"\x89PNG-fake-image-bytes"
    item = cb.clip_add_image(data)
    assert item["kind"] == "image" and item["len"] == len(data)
    assert os.path.isfile(item["path"]) and item["path"].endswith(item["id"] + ".png")
    assert item["preview"].startswith("data:image/png")
    # same image again -> same sha path (deduped on disk)
    item2 = cb.clip_add_image(data)
    assert item2["path"] == item["path"]


def test_oversized_image_skipped_with_notice(clip_tmp, monkeypatch):
    monkeypatch.setattr(cb, "CLIP_IMAGE_MAX", 16)
    item = cb.clip_add_image(b"x" * 32)
    assert item["skipped"] is True and item["kind"] == "image"
    assert not item.get("path")  # nothing was written to disk


def test_image_path_containment(clip_tmp, monkeypatch):
    monkeypatch.setattr(cb, "_image_preview", lambda path, raw: "")
    item = cb.clip_add_image(b"data")
    cb._clip_history.insert(0, item)  # as the watcher would
    assert cb.clip_image_path(item["id"]) == item["path"]
    assert cb.clip_image_path("missing") is None
    # a text item is not an image
    cb.clip_add("just text", "desktop")
    tid = cb.clip_history()[0]["id"]
    assert cb.clip_image_path(tid) is None
    # a tampered path outside the image dir never resolves
    evil = dict(item, id="evil1", path="/etc/passwd")
    cb._clip_history.insert(0, evil)
    assert cb.clip_image_path("evil1") is None


def test_preview_budget(clip_tmp, monkeypatch):
    """The inline preview stays within the 64KB data-URI budget."""
    big = bytes(range(256)) * 400  # ~100KB raw
    monkeypatch.setattr(cb, "CLIP_IMAGE_MAX", 10 * 1024 * 1024)
    monkeypatch.setattr(
        cb, "_ffmpeg_png_preview", lambda path, width: b"\xff\xd8" + b"z" * (128 * 1024)  # always too big
    )
    item = cb.clip_add_image(big)
    assert len(item["preview"]) <= cb.CLIP_PREVIEW_MAX or item["preview"] == ""


def test_uri_list_capture_deduped(clip_tmp, monkeypatch):
    monkeypatch.setattr(
        cb, "_run_xclip",
        lambda cmd, timeout=4: (
            (0, "file:///home/u/report.pdf", "")
            if "-t" in cmd and cmd[cmd.index("-t") + 1] == "text/uri-list"
            else (1, "", "")
        ),
    )
    t = threading.Thread(target=cb.clipboard_watcher, args=(None,), daemon=True)
    t.start()
    time.sleep(1.8)  # one watcher tick
    hist = cb.clip_history()
    assert any(it["kind"] == "uri" and "report.pdf" in it["preview"] for it in hist)


# ----------------------------------------------------------------- #72


def test_sync_disabled_by_default(clip_tmp, monkeypatch):
    monkeypatch.setattr(state, "load_settings", lambda: {"clip_sync": False})
    cb.clip_add("secret", "desktop")
    r = cb.clip_sync(None)
    assert r == {"enabled": False, "items": []}


def test_sync_enabled_serves_items(clip_tmp, monkeypatch):
    monkeypatch.setattr(state, "load_settings", lambda: {"clip_sync": True})
    cb.clip_add("first", "desktop")
    cb.clip_add("second", "desktop")
    r = cb.clip_sync(None)  # first poll: newest only
    assert r["enabled"] is True and [i["text"] for i in r["items"]] == ["second"]
    newest = r["items"][0]["id"]
    assert [i["text"] for i in cb.clip_sync(newest)["items"]] == []  # up to date
    cb.clip_add("third", "desktop")  # a new desktop copy arrives
    assert [i["text"] for i in cb.clip_sync(newest)["items"]] == ["third"]
    # a trimmed/stale cursor falls back to the newest item, never deadlocks
    assert [i["text"] for i in cb.clip_sync("gone-from-history")["items"]] == ["third"]


def test_sync_off_stops_serving(clip_tmp, monkeypatch):
    monkeypatch.setattr(state, "load_settings", lambda: {"clip_sync": True})
    cb.clip_add("x", "desktop")
    assert cb.clip_sync(None)["enabled"] is True
    monkeypatch.setattr(state, "load_settings", lambda: {"clip_sync": False})
    assert cb.clip_sync(None)["enabled"] is False


# --------------------------------------------------- settings persistence


def test_settings_roundtrip_and_validation(clip_tmp):
    assert state.load_settings() == {"clip_sync": False, "geo_remember": True}  # defaults
    assert state.save_setting("clip_sync", True)["clip_sync"] is True
    assert state.load_settings()["clip_sync"] is True
    assert state.save_setting("clip_sync", "yes") is None  # booleans only
    assert state.save_setting("unknown", True) is None
