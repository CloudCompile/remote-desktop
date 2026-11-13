"""Tests for the v2.4.0 files & transfer work.

- #91 path containment (symlinks, traversal payloads, TOCTOU model)
- #84 shared roots with per-root read-only flags
- #86 Unicode normalisation, collisions and header safety
"""

import os
import stat as stat_mod

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
