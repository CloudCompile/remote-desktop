"""Tests for the v2.4.0 files & transfer work.

- #91 path containment (symlinks, traversal payloads, TOCTOU model)
- #84 shared roots with per-root read-only flags
- #86 Unicode normalisation, collisions and header safety
"""

import glob
from pathlib import Path
import hashlib
import json
import os
import stat as stat_mod
import time

import pytest

from remote_desktop import config, files as rd_files


# ---------------------------------------------------------------- #91 containment


@pytest.fixture(autouse=True)
def clean_roots():
    """Keep the global ROOTS table pristine between tests."""
    saved, saved_errs = list(rd_files.ROOTS), list(rd_files.ROOT_ERRORS)
    yield
    rd_files.ROOTS[:] = saved
    rd_files.ROOT_ERRORS[:] = saved_errs


@pytest.fixture
def root(tmp_path):
    """A populated shared root."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner").mkdir()
    (tmp_path / "hello.txt").write_bytes(b"hello")
    (tmp_path / "sub" / "file.bin").write_bytes(b"\x00\x01\x02")
    return tmp_path


TRAVERSAL_PAYLOADS = [
    "../x",
    "../../etc/passwd",
    "a/../../b",
    "..",
    "../",
    "..\\..\\x",
    "sub/../../../etc/passwd",
    "a\x00b",
    "\x00",
    "/\x00/evil",
]


@pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
def test_split_parts_rejects_traversal_payloads(payload):
    with pytest.raises(rd_files.FilesError):
        rd_files.split_parts(payload)


@pytest.mark.parametrize(
    "payload",
    ["a\x01b", "a\x1fb", "a\x7fb", "a\u200fb", "a\ufeffb", "", ".", "..", "a/b", "a\\b"],
)
def test_valid_name_rejects_control_and_separators(payload):
    with pytest.raises(rd_files.FilesError):
        rd_files.valid_name(payload)


def test_valid_name_accepts_unicode_and_preserves_form():
    nfd = "cafe\u0301.txt"  # decomposed
    nfc = "café.txt"
    assert rd_files.valid_name(nfd) == nfd  # stored form preserved
    assert rd_files.valid_name(nfc) == nfc
    assert rd_files.valid_name("データ.txt") == "データ.txt"
    assert rd_files.name_key(nfd) == rd_files.name_key(nfc)


def test_open_dir_fd_rejects_symlink_parent_outside(root):
    # sub -> /etc: walking through sub must refuse, never resolve outside
    os.symlink("/etc", root / "sub" / "evil_link")
    with pytest.raises(rd_files.FilesError, match="symlink"):
        rd_files.open_dir_fd(str(root), ["sub", "evil_link", "passwd"])


def test_open_file_fd_refuses_symlink_final_component(root):
    os.symlink("/etc/passwd", root / "sub" / "escape.bin")
    with pytest.raises(rd_files.FilesError, match="symlink"):
        rd_files.open_file_fd(str(root), ["sub", "escape.bin"], os.O_RDONLY)


def test_symlink_outside_cannot_be_read_written_or_deleted_through(root):
    os.symlink("/tmp/rd-should-not-exist", root / "sub" / "link")
    # read
    with pytest.raises(rd_files.FilesError):
        fd = rd_files.open_file_fd(str(root), ["sub", "link"], os.O_RDONLY)
        os.close(fd)
    # write (create would follow without O_NOFOLLOW)
    with pytest.raises(rd_files.FilesError):
        rd_files.open_file_fd(str(root), ["sub", "link"], os.O_WRONLY | os.O_CREAT, 0o600)


def test_stat_contained_lstats_never_follows(root):
    os.symlink("/etc/passwd", root / "sub" / "l")
    st = rd_files.stat_contained(str(root), ["sub", "l"])
    assert stat_mod.S_ISLNK(st.st_mode)


def test_open_dir_fd_missing_raises_not_found(root):
    with pytest.raises(rd_files.FilesError, match="not found"):
        rd_files.open_dir_fd(str(root), ["nope"])
    with pytest.raises(rd_files.FilesError, match="not found"):
        rd_files.open_dir_fd(str(root), ["sub", "nope"])


def test_open_dir_fd_rejects_file_as_directory(root):
    with pytest.raises(rd_files.FilesError):
        rd_files.open_dir_fd(str(root), ["hello.txt"])


def test_delete_contained_removes_tree_but_never_traverses_symlinks(root):
    outside = root.parent / "outside.txt"
    outside.write_bytes(b"keep me")
    os.symlink(str(outside), root / "sub" / "steal")
    (root / "sub" / "inner" / "deep.txt").write_bytes(b"x")
    rd_files.delete_contained(str(root), ["sub"])
    assert not (root / "sub").exists()
    assert outside.read_bytes() == b"keep me"  # symlink was unlinked, not followed


def test_split_parts_allows_nested_relative_paths():
    assert rd_files.split_parts("a/b/c.txt") == ["a", "b", "c.txt"]
    assert rd_files.split_parts("") == []
    assert rd_files.split_parts(".") == []


def test_safe_file_path_legacy_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "FILES_DIR", str(tmp_path))
    (tmp_path / "real.txt").write_bytes(b"x")
    os.symlink("/etc/passwd", tmp_path / "evil.txt")
    assert rd_files._safe_file_path("real.txt") == str(tmp_path / "real.txt")
    assert rd_files._safe_file_path("evil.txt") is None
    p = rd_files._safe_file_path("../evil")  # basename-stripped, still contained
    assert p == str(tmp_path / "evil")
    assert rd_files._safe_file_path(".hidden") is None


def test_root_fd_survives_component_swap(root):
    """TOCTOU model: once the directory fd is open, a later rename of the
    component cannot change what our fd refers to (#91)."""
    fd = rd_files.open_dir_fd(str(root), ["sub"])
    try:
        os.rename(root / "sub", root / "sub2")  # swap the directory away
        st = os.fstat(fd)
        assert stat_mod.S_ISDIR(st.st_mode)
        # the fd still lists the ORIGINAL directory's contents
        assert sorted(os.listdir(fd)) == ["file.bin", "inner"]
    finally:
        os.close(fd)
        os.rename(root / "sub2", root / "sub")


# ---------------------------------------------------------------- #84 roots


def test_parse_roots_default_single_root(tmp_path):
    roots, errs = rd_files.parse_roots("", str(tmp_path / "downloads"))
    assert errs == []
    assert len(roots) == 1
    assert roots[0].path == os.path.realpath(str(tmp_path / "downloads"))
    assert roots[0].label == "Downloads"
    assert roots[0].readonly is False


def test_parse_roots_spec_with_labels_and_flags():
    roots, errs = rd_files.parse_roots("/data/proj:Projects:ro,/srv/media:Media:quota=1000", "/home/x/Downloads")
    assert errs == []
    assert [(r.rid, r.label, r.readonly, r.quota) for r in roots] == [
        ("r0", "Projects", True, 0),
        ("r1", "Media", False, 1000),
    ]
    assert roots[0].path == "/data/proj"


def test_parse_roots_reports_invalid_entries():
    roots, errs = rd_files.parse_roots("relative/path:Bad,/ok:Ok:ro", "/home/x/Downloads")
    assert len(errs) == 1 and "absolute" in errs[0]
    assert [r.label for r in roots] == ["Ok"]


def test_parse_roots_bad_quota_reported():
    roots, errs = rd_files.parse_roots("/a:A:quota=nope,/b:B", "/home/x/Downloads")
    assert len(errs) == 1 and "quota" in errs[0]
    assert roots[0].quota == 0


def test_parse_roots_empty_after_junk_falls_back():
    roots, errs = rd_files.parse_roots(",,,", "/home/x/Downloads")
    assert len(roots) == 1 and roots[0].label == "Downloads"
    assert errs == []


def test_setup_roots_creates_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "VALUES", {"files_roots": str(tmp_path / "newroot") + ":Fresh", "files_dir": str(tmp_path / "dl")}
    )
    errs = rd_files.setup_roots()
    assert errs == []
    assert (tmp_path / "newroot").is_dir()
    assert rd_files.resolve_root("fresh").label == "Fresh"


def test_setup_roots_reports_uncreatable(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(config, "VALUES", {"files_roots": str(blocker / "sub"), "files_dir": str(tmp_path / "dl")})
    errs = rd_files.setup_roots()
    assert len(errs) == 1 and "cannot create" in errs[0]


def test_resolve_root_by_id_label_case(tmp_path):
    rd_files.ROOTS[:] = [
        rd_files.Root("r0", "/a", "Projects"),
        rd_files.Root("r1", "/b", "Media"),
    ]
    assert rd_files.resolve_root("r1").label == "Media"
    assert rd_files.resolve_root("projects").label == "Projects"
    assert rd_files.resolve_root("MEDIA").label == "Media"
    assert rd_files.resolve_root("nope") is None
    assert rd_files.resolve_root("").label == "Projects"  # default = first
    rd_files.ROOTS.clear()
    rd_files.ROOTS.extend(rd_files.parse_roots("", "/home/x/Downloads")[0])


def test_require_writable_ro():
    r = rd_files.Root("r0", "/a", "A", readonly=True)
    with pytest.raises(rd_files.FilesError, match="read-only"):
        rd_files.require_writable(r)
    rd_files.require_writable(rd_files.Root("r1", "/b", "B"))


# ---------------------------------------------------------------- #86 names & headers


def test_safe_display_name_strips_unsafe():
    ascii_part, full = rd_files.safe_display_name("data€.txt")
    assert ascii_part == "data.txt"
    assert full == "data€.txt"


def test_safe_display_name_fallback_when_no_ascii():
    ascii_part, full = rd_files.safe_display_name("数据.txt")
    assert ascii_part == ".txt"
    assert full == "数据.txt"


def test_content_disposition_rfc5987():
    hdr = rd_files.content_disposition("café.txt")
    assert hdr.startswith('attachment; filename="caf.txt"')
    assert "filename*=UTF-8''caf%C3%A9.txt" in hdr


def test_content_disposition_ascii_only_is_simple():
    assert rd_files.content_disposition("plain.txt", inline=True) == 'inline; filename="plain.txt"'


def test_content_disposition_strips_newlines_and_quotes():
    hdr = rd_files.content_disposition('evil".txt\r\nX-Inject: 1')
    assert (
        "\r" not in hdr
        and "\n" not in hdr
        and '"' not in hdr[hdr.index('filename="') + 10 : hdr.index('"', hdr.index('filename="') + 10)]
    )


def test_check_collision_blocks_case_and_normalisation(tmp_path):
    (tmp_path / "Cafe.txt").write_bytes(b"x")
    # same key (casefold) different spelling -> refuse
    with pytest.raises(rd_files.FilesError, match="collides"):
        rd_files.check_collision(tmp_path, "cafe.TXT")
    # identical name is fine (overwrite path)
    rd_files.check_collision(tmp_path, "Cafe.txt")
    # NFC/NFD variants collide too
    (tmp_path / "café.txt").write_bytes(b"x")  # NFC
    with pytest.raises(rd_files.FilesError, match="collides"):
        rd_files.check_collision(tmp_path, "cafe\u0301.txt")


def test_check_collision_tolerant_on_listdir_error(tmp_path):
    # not a directory -> listdir fails -> treated as no collision
    rd_files.check_collision(tmp_path / "nope.txt", "x")


# ---------------------------------------------------------------- #79 listing & operations


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """Two roots (one read-only) wired through setup_roots."""
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    (a / "zeta.txt").write_bytes(b"zz")
    (a / "renamed.txt").write_bytes(b"rr")
    (a / "sub").mkdir()
    (a / "sub" / "deep.txt").write_bytes(b"deep")
    vals = dict(config.VALUES)
    vals.update(
        {
            "files_roots": f"{a}:Alpha,{b}:Beta:ro",
            "files_dir": str(a),
            "files_reserve": 0,
            "archive_cap": 8 * 1024**2,
            "transfer_retention": 3600,
        }
    )
    monkeypatch.setattr(config, "VALUES", vals)
    rd_files.setup_roots()
    return rd_files.resolve_root("alpha")


def test_list_dir_groups_and_types(mgr):
    d = rd_files.list_dir(mgr, "")
    assert d["path"] == ""
    assert [e["name"] for e in d["entries"]] == ["sub", "renamed.txt", "zeta.txt"]  # dirs first, then name
    sub = next(e for e in d["entries"] if e["name"] == "sub")
    assert sub["type"] == "dir" and sub["size"] == 0
    f = next(e for e in d["entries"] if e["name"] == "zeta.txt")
    assert f["type"] == "file" and f["size"] == 2
    assert f["mode"].startswith("-")


def test_list_dir_sort_modes(mgr):
    by_size = rd_files.list_dir(mgr, "", sort="size", desc=True)
    sizes = [e["size"] for e in by_size["entries"] if e["type"] == "file"]
    assert sizes == sorted(sizes, reverse=True)
    dirs_last = rd_files.list_dir(mgr, "", dirs_first=False)
    assert dirs_last["entries"][-1]["type"] != "dir"


def test_list_dir_hidden_dotfiles_invisible(mgr):
    (Path(mgr.path) / ".secret").write_bytes(b"x")
    d = rd_files.list_dir(mgr, "")
    assert ".secret" not in [e["name"] for e in d["entries"]]


def test_mkdir_nested_and_refusals(mgr):
    r = rd_files.mkdir(mgr, "new/deeper")
    assert r["path"] == "new/deeper"
    assert (Path(mgr.path) / "new" / "deeper").is_dir()
    with pytest.raises(rd_files.FilesError):
        rd_files.mkdir(mgr, "zeta.txt/under")  # existing file in the way
    with pytest.raises(rd_files.FilesError):
        rd_files.mkdir(mgr, "")


def test_rename_and_collisions(mgr):
    r = rd_files.rename(mgr, "zeta.txt", "zeta2.txt")
    assert r["path"] == "zeta2.txt"
    assert (Path(mgr.path) / "zeta2.txt").exists() and not (Path(mgr.path) / "zeta.txt").exists()
    with pytest.raises(rd_files.FilesError, match="collides"):
        rd_files.rename(mgr, "zeta2.txt", "RENAMED.TXT")  # case-variant of an existing file
    with pytest.raises(rd_files.FilesError):
        rd_files.rename(mgr, "missing.txt", "x.txt")
    with pytest.raises(rd_files.FilesError, match="separator"):
        rd_files.rename(mgr, "zeta2.txt", "a/b.txt")


def test_move_into_folder_and_auto_rename(mgr):
    r = rd_files.transfer(mgr, "zeta.txt", mgr, "sub")
    assert r["dest"] == "sub/zeta.txt"
    assert (Path(mgr.path) / "sub" / "zeta.txt").read_bytes() == b"zz"
    assert not (Path(mgr.path) / "zeta.txt").exists()
    # auto-rename on collision: renamed.txt -> sub/renamed (2).txt
    (Path(mgr.path) / "sub" / "renamed.txt").write_bytes(b"existing")
    r2 = rd_files.transfer(mgr, "renamed.txt", mgr, "sub")
    assert r2["dest"] == "sub/renamed (2).txt"
    assert (Path(mgr.path) / "sub" / "renamed.txt").read_bytes() == b"existing"  # untouched


def test_copy_keeps_original_and_overwrite(mgr):
    r = rd_files.transfer(mgr, "zeta.txt", mgr, "sub", copy=True)
    assert r["dest"] == "sub/zeta.txt"
    assert (Path(mgr.path) / "zeta.txt").exists()  # original kept
    (Path(mgr.path) / "sub" / "zeta.txt").write_bytes(b"existing")
    # overwrite replaces the destination bytes (copy semantics keep the src)
    rd_files.transfer(mgr, "zeta.txt", mgr, "sub", overwrite=True, copy=True)
    assert (Path(mgr.path) / "sub" / "zeta.txt").read_bytes() == b"zz"
    assert (Path(mgr.path) / "zeta.txt").exists()
    # ...while overwrite=False auto-renames
    r2 = rd_files.transfer(mgr, "zeta.txt", mgr, "sub", copy=True)
    assert r2["dest"] == "sub/zeta (2).txt"


def test_copy_dir_tree(mgr):
    rd_files.transfer(mgr, "sub", mgr, "subcopy", copy=True)
    assert (Path(mgr.path) / "subcopy" / "deep.txt").read_bytes() == b"deep"
    assert (Path(mgr.path) / "sub" / "deep.txt").exists()  # source intact


def test_cross_root_move_copies_then_deletes(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.bin").write_bytes(b"data123")
    vals = dict(config.VALUES)
    vals.update({"files_roots": f"{a}:A,{b}:B", "files_dir": str(a)})
    monkeypatch.setattr(config, "VALUES", vals)
    rd_files.setup_roots()
    ra, rb = rd_files.resolve_root("a"), rd_files.resolve_root("b")
    r = rd_files.transfer(ra, "x.bin", rb, "")
    assert r["dest"] == "x.bin"
    assert (b / "x.bin").read_bytes() == b"data123"
    assert not (a / "x.bin").exists()


def test_bulk_delete_reports_partial_failures(mgr):
    r = rd_files.delete_many(mgr, ["renamed.txt", "no-such.bin", "sub"])
    assert r["ok"] is False
    assert r["deleted"] == ["renamed.txt", "sub"]
    assert len(r["failed"]) == 1 and r["failed"][0]["path"] == "no-such.bin"
    r2 = rd_files.delete_many(mgr, ["zeta.txt"])
    assert r2["ok"] is True and r2["deleted"] == ["zeta.txt"]


def test_readonly_root_refuses_all_writes(mgr):
    beta = rd_files.resolve_root("beta")
    for fn in (
        lambda: rd_files.mkdir(beta, "d"),
        lambda: rd_files.rename(beta, "x", "y"),
        lambda: rd_files.transfer(mgr, "zeta.txt", beta, "", copy=False),
        lambda: rd_files.chmod(beta, "x", 0o600),
    ):
        with pytest.raises(rd_files.FilesError, match="read-only"):
            fn()


# ---------------------------------------------------------------- #81 search


def test_search_finds_nested_and_is_bounded(mgr):
    (Path(mgr.path) / "sub" / "findme.img").write_bytes(b"x")
    r = rd_files.search(mgr, "", "findme")
    assert len(r["matches"]) == 1
    assert r["matches"][0]["dir"] == "sub"
    assert r["matches"][0]["type"] == "file"
    # bounded results
    for i in range(30):
        (Path(mgr.path) / f"many{i}.log").write_bytes(b"x")
    r2 = rd_files.search(mgr, "", "many", limit=5)
    assert len(r2["matches"]) == 5 and r2["truncated"] is True
    # empty query: no work
    assert rd_files.search(mgr, "", "")["matches"] == []


# ---------------------------------------------------------------- #83 ranges


def test_parse_range_absent():
    assert rd_files.parse_range(None, 100) is None
    assert rd_files.parse_range("", 100) is None


def test_parse_range_partial_suffix_and_clamp():
    assert rd_files.parse_range("bytes=0-4", 100) == (0, 4)
    assert rd_files.parse_range("bytes=95-", 100) == (95, 99)
    assert rd_files.parse_range("bytes=0-999", 100) == (0, 99)  # clamped
    assert rd_files.parse_range("bytes=-3", 100) == (97, 99)
    assert rd_files.parse_range("bytes=-100", 5) == (0, 4)


def test_parse_range_invalid_and_multi():
    assert rd_files.parse_range("bytes=0-4,6-9", 100) == "invalid"  # multi-range refused
    assert rd_files.parse_range("bytes=abc", 100) == "invalid"
    assert rd_files.parse_range("bytes=-", 100) == "invalid"
    assert rd_files.parse_range("items=0-4", 100) == "invalid"
    assert rd_files.parse_range("bytes=200-300", 100) == "invalid"  # unsatisfiable
    assert rd_files.parse_range("bytes=5-4", 100) == "invalid"  # start > end
    assert rd_files.parse_range("bytes=0-4", 0) == "invalid"  # empty file


# ---------------------------------------------------------------- #78 chunked uploads


@pytest.fixture
def sess(tmp_path, monkeypatch):
    """One writable root + sessions dir under a fake state dir."""
    a = tmp_path / "alpha"
    a.mkdir()
    st = tmp_path / "state"
    st.mkdir()
    vals = dict(config.VALUES)
    vals.update({"files_roots": f"{a}:Alpha", "files_dir": str(a), "state_dir": str(st), "files_reserve": 0})
    monkeypatch.setattr(config, "VALUES", vals)
    rd_files.setup_roots()
    return rd_files


def test_upload_session_roundtrip(sess):
    root = rd_files.resolve_root("alpha")
    r = rd_files.upload_init(root, "", "big.bin", size=10, chunk_size=65536)
    sid = r["session"]
    assert r["offset"] == 0 and r["chunk_size"] == 65536
    b1 = b"0123456789"
    rd_files.upload_chunk(sid, 0, b1[:6])
    r2 = rd_files.upload_chunk(sid, 6, b1[6:])
    assert r2["offset"] == 10
    done = rd_files.upload_complete(sid)
    assert done["size"] == 10 and done["name"] == "big.bin"
    assert (Path(root.path) / "big.bin").read_bytes() == b1
    meta_state = rd_files.transfers_snapshot()
    assert any(t["state"] == "done" and t["id"] == sid for t in meta_state["transfers"])


def test_upload_chunk_checksum_reject_and_retry(sess):
    root = rd_files.resolve_root("alpha")
    r = rd_files.upload_init(root, "", "f.bin", size=8, chunk_size=8)
    sid = r["session"]
    good = hashlib.sha256(b"gooddata").hexdigest()
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.upload_chunk(sid, 0, b"bad-data", good)
    assert e.value.status == 400
    # offset unchanged -> the client retries the SAME chunk
    r2 = rd_files.upload_chunk(sid, 0, b"gooddata", good)
    assert r2["offset"] == 8
    assert rd_files.transfers_snapshot()["transfers"][0]["retries"] == 1


def test_upload_chunk_offset_mismatch_is_409(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=100)["session"]
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.upload_chunk(sid, 50, b"x" * 10)
    assert e.value.status == 409


def test_upload_resume_reports_acknowledged_offset(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=12, chunk_size=6)["session"]
    rd_files.upload_chunk(sid, 0, b"aaaaaa")
    r = rd_files.upload_init(root, "", "f.bin", size=12, chunk_size=6, resume=sid)
    assert r["offset"] == 6  # resumption continues at the acknowledged offset
    rd_files.upload_chunk(sid, 6, b"bbbbbb")
    rd_files.upload_complete(sid)
    assert (Path(root.path) / "f.bin").read_bytes() == b"aaaaaabbbbbb"


def test_upload_paused_refuses_chunks_with_409(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=10)["session"]
    rd_files.transfer_pause(sid)
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.upload_chunk(sid, 0, b"0123456789")
    assert e.value.status == 409
    rd_files.transfer_resume(sid)
    rd_files.upload_chunk(sid, 0, b"0123456789")
    rd_files.upload_complete(sid)


def test_upload_complete_verifies_whole_file_sha(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=4, sha256=hashlib.sha256(b"abcd").hexdigest())["session"]
    rd_files.upload_chunk(sid, 0, b"abcd")
    rd_files.upload_complete(sid)
    sid2 = rd_files.upload_init(root, "", "g.bin", size=4, sha256="0" * 64)["session"]
    rd_files.upload_chunk(sid2, 0, b"nope")
    with pytest.raises(rd_files.FilesError, match="checksum"):
        rd_files.upload_complete(sid2)
    assert not (Path(root.path) / "g.bin").exists()


def test_upload_complete_incomplete_is_409(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=100)["session"]
    rd_files.upload_chunk(sid, 0, b"partial")
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.upload_complete(sid)
    assert e.value.status == 409


def test_upload_session_cap(sess):
    root = rd_files.resolve_root("alpha")
    for _ in range(rd_files.SESSIONS_MAX):
        rd_files.upload_init(root, "", "f.bin", size=1)
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.upload_init(root, "", "f.bin", size=1)
    assert e.value.status == 503


def test_upload_session_gc_expires(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "f.bin", size=10)["session"]
    done = rd_files.upload_init(root, "", "d.bin", size=10)["session"]
    rd_files.upload_chunk(done, 0, b"0123456789")
    rd_files.upload_complete(done)
    # age the abandoned session past the TTL
    sdir = os.path.join(rd_files.sessions_dir(), sid)
    meta = json.load(open(os.path.join(sdir, "meta.json")))
    meta["updated"] = int(time.time()) - rd_files.SESSION_TTL - 10
    rd_files._session_write_meta(sdir, meta)
    assert rd_files.gc_transfers(retention=3600) >= 1
    assert not os.path.exists(sdir)
    assert os.path.exists(os.path.join(rd_files.sessions_dir(), done))  # fresh done job kept


def test_upload_gc_retention_for_done_jobs(sess):
    root = rd_files.resolve_root("alpha")
    sid = rd_files.upload_init(root, "", "d.bin", size=4)["session"]
    rd_files.upload_chunk(sid, 0, b"abcd")
    rd_files.upload_complete(sid)
    sdir = os.path.join(rd_files.sessions_dir(), sid)
    meta = json.load(open(os.path.join(sdir, "meta.json")))
    meta["updated"] = int(time.time()) - 7200
    rd_files._session_write_meta(sdir, meta)
    rd_files.gc_transfers(retention=3600)
    assert not os.path.exists(sdir)


def test_upload_init_quota_refusal(sess):
    a = sess.resolve_root("alpha")
    a.quota = 100  # tiny quota
    with pytest.raises(rd_files.FilesError) as e:
        sess.upload_init(a, "", "f.bin", size=1000)
    assert e.value.status == 507 and "quota" in str(e.value)


# ---------------------------------------------------------------- #88 transfer queue


def test_transfer_queue_snapshot_and_lifecycle(sess):
    root = sess.resolve_root("alpha")
    sid = sess.upload_init(root, "", "q.bin", size=8)["session"]
    sess.upload_chunk(sid, 0, b"12345678")
    snap = sess.transfers_snapshot()
    t = next(t for t in snap["transfers"] if t["id"] == sid)
    assert t["state"] == "running" and t["pct"] == 100
    assert snap["aggregate"]["running"] >= 1
    sess.transfer_pause(sid)
    assert sess.transfers_snapshot()["transfers"][0]["state"] == "paused"
    sess.transfer_retry(sid)
    assert sess.transfers_snapshot()["transfers"][0]["state"] == "running"
    sess.transfer_cancel(sid)
    assert not os.path.exists(os.path.join(sess.sessions_dir(), sid))


def test_transfer_retry_increments_retries(sess):
    root = sess.resolve_root("alpha")
    sid = sess.upload_init(root, "", "q.bin", size=8)["session"]
    sess.transfer_pause(sid)
    sess.transfer_retry(sid)
    assert sess.transfers_snapshot()["transfers"][0]["retries"] == 1


# ---------------------------------------------------------------- #89 disk & reserve


def test_disk_info_fields(mgr):
    d = rd_files.disk_info(mgr)
    assert d["free"] > 0 and d["total"] > 0 and "reserve" in d and "quota" in d


def test_quota_check_reserve_refusal(tmp_path, monkeypatch):
    a = tmp_path / "alpha"
    a.mkdir()
    vals = dict(config.VALUES)
    vals.update({"files_roots": f"{a}:Alpha", "files_dir": str(a), "files_reserve": 1 << 40})
    monkeypatch.setattr(config, "VALUES", vals)
    rd_files.setup_roots()
    root = rd_files.resolve_root("alpha")
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.quota_check(root, 1 << 10)
    assert e.value.status == 507 and "reserve" in str(e.value)
    vals["files_reserve"] = 0
    monkeypatch.setattr(config, "VALUES", vals)
    rd_files.quota_check(root, 1 << 10)  # disabled reserve allows


# ---------------------------------------------------------------- #90 chmod


def test_chmod_sets_mode(mgr):
    p = Path(mgr.path) / "zeta.txt"
    os.chmod(p, 0o600)
    r = rd_files.chmod(mgr, "zeta.txt", 0o755)
    assert r["mode"] == "0o755"
    assert (p.stat().st_mode & 0o777) == 0o755
    os.chmod(p, 0o600)


def test_chmod_refuses_setuid_and_bad_paths(mgr):
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.chmod(mgr, "zeta.txt", 0o4755)
    assert e.value.status == 403
    with pytest.raises(rd_files.FilesError) as e2:
        rd_files.chmod(mgr, "no-such", 0o600)
    assert e2.value.status in (400, 404)


# ---------------------------------------------------------------- #92 audit


def test_audit_record_fields_and_no_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "AUDIT_FILE", str(tmp_path / "audit.jsonl"))

    class H:
        client_address = ("198.51.100.5", 9999)
        headers = {}
        rid = "abc123"

    rd_files.audit("upload", H(), root=rd_files.Root("r0", "/x", "Main"), path="dir/file.bin", size=42)
    ev = rd_files.audit_list()[0]
    assert ev["action"] == "upload" and ev["size"] == 42
    assert ev["ip"] == "198.51.100.5" and ev["rid"] == "abc123"
    assert ev["root"] == "Main" and ev["result"] == "ok"
    # never file contents
    assert "file.bin" in json.dumps(ev)  # path recorded
    assert len(json.dumps(ev)) < 600


def test_audit_rotates(tmp_path, monkeypatch):
    af = tmp_path / "audit.jsonl"
    monkeypatch.setattr(rd_files, "AUDIT_FILE", str(af))
    monkeypatch.setattr(rd_files, "AUDIT_MAX", 2048)
    monkeypatch.setattr(rd_files, "AUDIT_KEEP", 2)
    for i in range(60):
        rd_files.audit("delete", None, path=f"f{i}.txt")
    assert af.exists()
    rotated = glob.glob(str(af) + ".*")
    assert rotated, "expected rotation to produce audit.jsonl.N"


def test_audit_failure_never_breaks(tmp_path, monkeypatch):
    monkeypatch.setattr(rd_files, "AUDIT_FILE", "/proc/nope/audit.jsonl")
    rd_files.audit("upload", None, path="x")  # must not raise


# ---------------------------------------------------------------- #85 archives


def test_archive_zip_roundtrip(mgr):
    r = rd_files.archive_create(mgr, ["zeta.txt", "sub"], "zip", "bundle")
    assert r["path"] == "bundle.zip" and r["count"] >= 2
    import zipfile

    import zipfile

    with zipfile.ZipFile(Path(mgr.path) / "bundle.zip") as z:
        assert set(z.namelist()) >= {"zeta.txt", "sub/deep.txt"}
    dest = Path(mgr.path) / "out"
    dest.mkdir()
    x = rd_files.archive_extract(mgr, "bundle.zip", "out")
    assert x["files"] >= 2 and x["skipped"] == 0
    assert (dest / "sub" / "deep.txt").read_bytes() == b"deep"
    assert (dest / "zeta.txt").read_bytes() == b"zz"


def test_archive_targz_roundtrip(mgr):
    rd_files.archive_create(mgr, ["sub"], "tar.gz", "t")
    dest = Path(mgr.path) / "outg"
    dest.mkdir()
    rd_files.archive_extract(mgr, "t.tar.gz", "outg")
    assert (dest / "sub" / "deep.txt").read_bytes() == b"deep"


def test_archive_tarxz_roundtrip(mgr):
    rd_files.archive_create(mgr, ["zeta.txt"], "tar.xz", "tx")
    dest = Path(mgr.path) / "outx"
    dest.mkdir()
    rd_files.archive_extract(mgr, "tx.tar.xz", "outx")
    assert (dest / "zeta.txt").read_bytes() == b"zz"


def test_archive_unsupported_format(mgr):
    with pytest.raises(rd_files.FilesError, match="unsupported format"):
        rd_files.archive_create(mgr, ["zeta.txt"], "rar", "a")
    with pytest.raises(rd_files.FilesError, match="unsupported archive"):
        rd_files.archive_extract(mgr, "zeta.txt", "")


def test_zip_slip_refused(mgr):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", b"nope")
        z.writestr("ok.txt", b"fine")
    (Path(mgr.path) / "evil.zip").write_bytes(buf.getvalue())
    dest = Path(mgr.path) / "slip"
    dest.mkdir()
    with pytest.raises(rd_files.FilesError, match="traversal"):
        rd_files.archive_extract(mgr, "evil.zip", "slip")
    assert not (Path(mgr.path).parent / "evil.txt").exists()
    # nothing was extracted (the refusal aborts the batch)
    assert not (dest / "ok.txt").exists()


def test_zip_symlink_members_skipped(mgr):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zi = zipfile.ZipInfo("link.txt")
        zi.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
        z.writestr(zi, "/etc/passwd")
        z.writestr("real.txt", b"ok")
    (Path(mgr.path) / "links.zip").write_bytes(buf.getvalue())
    dest = Path(mgr.path) / "linkout"
    dest.mkdir()
    x = rd_files.archive_extract(mgr, "links.zip", "linkout")
    assert x["skipped"] == 1 and x["files"] == 1
    assert not (dest / "link.txt").exists() or not (dest / "link.txt").is_symlink()


def test_tar_hardlink_and_dev_members_skipped(mgr):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        data = b"ok-data"
        info = tarfile.TarInfo("data.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        lnk = tarfile.TarInfo("hard.txt")
        lnk.type = tarfile.LNKTYPE
        lnk.linkname = "/etc/passwd"
        t.addfile(lnk)
        dev = tarfile.TarInfo("dev")
        dev.type = tarfile.CHRTYPE
        t.addfile(dev)
    (Path(mgr.path) / "h.tar").write_bytes(buf.getvalue())
    dest = Path(mgr.path) / "tarout"
    dest.mkdir()
    x = rd_files.archive_extract(mgr, "h.tar", "tarout")
    assert x["files"] == 1 and x["skipped"] == 2
    assert (dest / "data.txt").read_bytes() == b"ok-data"


def test_zip_bomb_cap(mgr, monkeypatch):
    import io
    import zipfile

    vals = dict(config.VALUES)
    vals["archive_cap"] = 1024  # 1 KiB cap
    monkeypatch.setattr(config, "VALUES", vals)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("big.bin", b"x" * 4096)
    (Path(mgr.path) / "bomb.zip").write_bytes(buf.getvalue())
    dest = Path(mgr.path) / "bombout"
    dest.mkdir()
    with pytest.raises(rd_files.FilesError, match="cap"):
        rd_files.archive_extract(mgr, "bomb.zip", "bombout")


def test_member_validation():
    assert rd_files._member_parts("a/b/c.txt") == ["a", "b", "c.txt"]
    assert rd_files._member_parts("/abs/path") == ["abs", "path"]  # made relative
    assert rd_files._member_parts("./ok") == ["ok"]
    for bad in ("../x", "a\\b", "a\x00b", "a/../b", ""):
        with pytest.raises(rd_files.FilesError):
            rd_files._member_parts(bad)


# ---------------------------------------------------------------- #82 thumbnails


def test_thumbnail_small_image_streams_original(mgr):
    import base64

    # 1x1 PNG
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    (Path(mgr.path) / "tiny.png").write_bytes(png)
    kind, mime = rd_files.thumbnail(mgr, "tiny.png", 64)
    assert kind == "source" and mime == "image/png"


def test_thumbnail_refuses_unsafe_and_unknown(mgr):
    (Path(mgr.path) / "bad.svg").write_bytes(b"<svg/>")
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.thumbnail(mgr, "bad.svg", 64)
    assert e.value.status == 415
    (Path(mgr.path) / "data.bin").write_bytes(b"\x00" * 64)
    with pytest.raises(rd_files.FilesError) as e2:
        rd_files.thumbnail(mgr, "data.bin", 64)
    assert e2.value.status == 415


def test_thumbnail_pdf_without_tools_is_501(mgr, monkeypatch):
    (Path(mgr.path) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(rd_files, "_thumb_tool", lambda kind: None)
    with pytest.raises(rd_files.FilesError) as e:
        rd_files.thumbnail(mgr, "doc.pdf", 64)
    assert e.value.status == 501


def test_thumbnail_generated_and_cached(mgr, monkeypatch):
    (Path(mgr.path) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fakejpeg")

    monkeypatch.setattr(rd_files, "_thumb_tool", lambda kind: ["tool", "-s", "{size}", None] if kind == "pdf" else None)
    monkeypatch.setattr(rd_files.subprocess, "run", fake_run)
    kind, mime = rd_files.thumbnail(mgr, "doc.pdf", 128)
    assert kind.endswith(".jpg") and mime == "image/jpeg"
    assert calls and calls[0][-1].endswith(".jpg")
    assert os.path.exists(kind)  # cache file persisted
    # cached second call: no new tool run
    n = len(calls)
    kind2, _ = rd_files.thumbnail(mgr, "doc.pdf", 128)
    assert kind2 == kind and len(calls) == n


def test_thumbnail_cache_invalidates_on_change(mgr, monkeypatch):
    (Path(mgr.path) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(rd_files, "_thumb_tool", lambda kind: ["tool", None])
    monkeypatch.setattr(rd_files.subprocess, "run", lambda cmd, **kw: open(cmd[-1], "wb").write(b"j"))
    k1, _ = rd_files.thumbnail(mgr, "doc.pdf", 64)
    (Path(mgr.path) / "doc.pdf").write_bytes(b"%PDF-1.5 different")
    k2, _ = rd_files.thumbnail(mgr, "doc.pdf", 64)
    assert k1 != k2  # mtime+size changed -> fresh cache key


def test_thumb_tool_probe_prefers_specialised():
    import shutil

    if shutil.which("pdftoppm"):
        assert rd_files._thumb_tool("pdf")[0] == "pdftoppm"
    if shutil.which("convert"):
        assert rd_files._thumb_tool("image")[0] == "convert"
