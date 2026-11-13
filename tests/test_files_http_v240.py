"""HTTP integration tests for the v2.4.0 files & transfer API (#78-#93).

Runs the REAL server in a subprocess (same harness as test_integration)
with a second read-only root and a quota root, and exercises every new
endpoint through plain urllib: roots, listing, mkdir/rename/move/copy,
bulk delete, chunked resumable uploads, the transfer queue, ranged
downloads, search, thumbnails, archives, chmod, audit and the PWA share
target. Only pytest + stdlib.
"""

import hashlib
import io
import json
import os
import socket
import tarfile
import urllib.parse
import zipfile

import pytest

from test_integration import Server, TEST_HASH, TEST_PASSWORD, _free_port


class FilesServer(Server):
    """Server with two extra roots: one read-only, one with a tiny quota."""

    def __init__(self, tmp_path, port, vnc_port, passfile):
        super().__init__(tmp_path, port, vnc_port, passfile)
        self.ro_dir = tmp_path / "ro-root"
        self.quota_dir = tmp_path / "quota-root"
        self.ro_dir.mkdir(exist_ok=True)
        self.quota_dir.mkdir(exist_ok=True)
        self.env["RD_FILES_ROOTS"] = ",".join(
            [
                f"{self.files_dir}:Main",
                f"{self.ro_dir}:Locked:ro",
                f"{self.quota_dir}:Tiny:quota=256",
            ]
        )


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("files-http")
    vnc_sock = socket.socket()
    vnc_sock.bind(("127.0.0.1", 0))
    vnc_port = vnc_sock.getsockname()[1]
    vnc_sock.close()
    passfile = tmp_path / ".passhash"
    passfile.write_text(TEST_HASH + "\n")
    s = FilesServer(tmp_path, _free_port(), vnc_port, passfile)
    s.start()
    yield s
    rc = s.stop()
    assert rc is None or rc == 0, f"server did not exit cleanly: {rc} \n{s.log_tail()}"


_COOKIE = []


def _login(s):
    """One login per server (the endpoint is rate-limited by IP)."""
    if not _COOKIE:
        code, headers, _ = s.request("/api/login", data={"username": "admin", "password": TEST_PASSWORD})
        assert code == 200
        _COOKIE.append(headers["Set-Cookie"].split(";")[0])
    return _COOKIE[0]


def _get(s, cookie, path, headers=None):
    return s.request(path, cookie=cookie, headers=headers or {})


def _post(s, cookie, path, data=None, raw=None, headers=None):
    return s.request(path, cookie=cookie, data=data, raw=raw, headers=headers or {})


def test_roots_listed_with_permissions(server):
    cookie = _login(server)
    code, _, body = _get(server, cookie, "/api/files/roots")
    assert code == 200
    j = json.loads(body)
    labels = {r["label"]: r for r in j["roots"]}
    assert labels["Main"]["readonly"] is False
    assert labels["Locked"]["readonly"] is True
    assert labels["Tiny"]["quota"] == 256


def test_listing_dirs_and_navigation(server):
    cookie = _login(server)
    (server.files_dir / "sub").mkdir(exist_ok=True)
    (server.files_dir / "sub" / "nested.txt").write_bytes(b"n")
    code, _, body = _get(server, cookie, "/api/files")
    j = json.loads(body)
    assert code == 200 and j["ok"] is True
    types = {e["name"]: e["type"] for e in j["entries"]}
    assert types.get("sub") == "dir"
    assert any(e["name"] == "sub" for e in j["entries"])
    # navigate in
    code, _, body = _get(server, cookie, "/api/files?path=sub")
    j = json.loads(body)
    assert [e["name"] for e in j["entries"]] == ["nested.txt"]


def test_mkdir_rename_move_copy_delete_over_http(server):
    cookie = _login(server)
    code, _, _ = _post(server, cookie, "/api/files/mkdir", {"root": "r0", "path": "proj"})
    assert code == 200
    code, _, _ = _post(server, cookie, "/api/files/rename", {"root": "r0", "path": "proj", "name": "project"})
    assert code == 200
    (server.files_dir / "src.txt").write_bytes(b"mover")
    code, _, body = _post(server, cookie, "/api/files/move", {"root": "r0", "paths": ["src.txt"], "dest": "project"})
    assert code == 200 and json.loads(body)["results"][0]["dest"] == "project/src.txt"
    assert not (server.files_dir / "src.txt").exists()
    (server.files_dir / "src.txt").write_bytes(b"copy")
    code, _, body = _post(server, cookie, "/api/files/copy", {"root": "r0", "paths": ["src.txt"], "dest": "project"})
    assert code == 200
    # collision policy (#80/#86): the second copy auto-renamed, original untouched
    assert (server.files_dir / "project" / "src.txt").read_bytes() == b"mover"
    assert (server.files_dir / "project" / "src (2).txt").read_bytes() == b"copy"
    code, _, body = _post(server, cookie, "/api/files/delete", {"root": "r0", "paths": ["src.txt", "no-such.txt"]})
    j = json.loads(body)
    assert code == 207 and j["deleted"] == ["src.txt"] and j["failed"][0]["path"] == "no-such.txt"


def test_readonly_root_enforced_server_side(server):
    cookie = _login(server)
    for path, payload in (
        ("/api/files/mkdir", {"root": "Locked", "path": "d"}),
        ("/api/files/rename", {"root": "Locked", "path": "x", "name": "y"}),
        ("/api/files/delete", {"root": "Locked", "paths": ["x"]}),
        ("/api/files/chmod", {"root": "Locked", "path": "x", "mode": "600"}),
        ("/api/files/upload/init", {"root": "Locked", "name": "evil.txt", "size": 1, "chunk_size": 65536}),
    ):
        code, _, _ = _post(server, cookie, path, payload)
        assert code == 403, f"{path} allowed a write to a read-only root"


def test_chunked_upload_over_http(server):
    cookie = _login(server)
    data = b"0123456789abcdef"
    code, _, body = _post(
        server,
        cookie,
        "/api/files/upload/init",
        {"root": "r0", "path": "", "name": "chunked.bin", "size": len(data), "chunk_size": 65536},
    )
    j = json.loads(body)
    assert code == 200 and j["offset"] == 0
    sid = j["session"]
    # two chunks with checksums
    for off in (0, 8):
        code, _, body = server.request(
            f"/api/files/upload/chunk?session={sid}&offset={off}",
            cookie=cookie,
            raw=data[off : off + 8],
            headers={"X-Chunk-Sha256": hashlib.sha256(data[off : off + 8]).hexdigest()},
        )
        assert code == 200, body
    code, _, body = _post(server, cookie, "/api/files/upload/complete", {"session": sid})
    j = json.loads(body)
    assert code == 200 and j["size"] == len(data)
    assert (server.files_dir / "chunked.bin").read_bytes() == data


def test_chunk_checksum_mismatch_rejected(server):
    cookie = _login(server)
    code, _, body = _post(
        server, cookie, "/api/files/upload/init", {"root": "r0", "name": "bad.bin", "size": 8, "chunk_size": 65536}
    )
    sid = json.loads(body)["session"]
    code, _, body = server.request(
        f"/api/files/upload/chunk?session={sid}&offset=0",
        cookie=cookie,
        raw=b"corrupted",
        headers={"X-Chunk-Sha256": "0" * 64},
    )
    assert code == 400
    # acknowledged offset unchanged: a retry of the same chunk can succeed
    code, _, body = _get(server, cookie, f"/api/files/upload/status?session={sid}")
    assert json.loads(body)["offset"] == 0


def test_upload_resume_after_interrupt(server):
    """A 'dropped' upload resumes at the acknowledged offset (#78)."""
    cookie = _login(server)
    data = b"x" * 100
    code, _, body = _post(
        server, cookie, "/api/files/upload/init", {"root": "r0", "name": "resume.bin", "size": 100, "chunk_size": 65536}
    )
    sid = json.loads(body)["session"]
    code, _, _ = server.request(f"/api/files/upload/chunk?session={sid}&offset=0", cookie=cookie, raw=data[:60])
    assert code == 200
    # reconnect: init with resume returns the acknowledged offset
    code, _, body = _post(
        server,
        cookie,
        "/api/files/upload/init",
        {"root": "r0", "name": "resume.bin", "size": 100, "chunk_size": 65536, "session": sid},
    )
    j = json.loads(body)
    assert code == 200 and j["offset"] == 60  # resumed near where it dropped
    code, _, _ = server.request(f"/api/files/upload/chunk?session={sid}&offset=60", cookie=cookie, raw=data[60:])
    assert code == 200
    code, _, _ = _post(server, cookie, "/api/files/upload/complete", {"session": sid})
    assert code == 200
    assert (server.files_dir / "resume.bin").stat().st_size == 100


def test_transfer_queue_over_http(server):
    cookie = _login(server)
    code, _, body = _post(
        server, cookie, "/api/files/upload/init", {"root": "r0", "name": "queued.bin", "size": 10, "chunk_size": 65536}
    )
    sid = json.loads(body)["session"]
    code, _, body = _get(server, cookie, "/api/files/transfers")
    j = json.loads(body)
    t = next(t for t in j["transfers"] if t["id"] == sid)
    assert t["state"] == "running" and t["kind"] == "upload"
    assert "aggregate" in j
    code, _, _ = _post(server, cookie, "/api/files/transfers/pause", {"id": sid})
    assert code == 200
    # paused: chunk refused with 409, no bytes lost
    code, _, _ = server.request(f"/api/files/upload/chunk?session={sid}&offset=0", cookie=cookie, raw=b"0123456789")
    assert code == 409
    code, _, _ = _post(server, cookie, "/api/files/transfers/resume", {"id": sid})
    assert code == 200
    code, _, _ = server.request(f"/api/files/upload/chunk?session={sid}&offset=0", cookie=cookie, raw=b"0123456789")
    assert code == 200
    code, _, _ = _post(server, cookie, "/api/files/upload/complete", {"session": sid})
    assert code == 200
    code, _, _ = _post(server, cookie, "/api/files/transfers/cancel", {"id": sid})
    assert code == 200


def test_range_download_and_416(server):
    cookie = _login(server)
    data = b"0123456789"
    (server.files_dir / "range.bin").write_bytes(data)
    # partial content
    code, headers, body = _get(server, cookie, "/api/files/content?path=range.bin", headers={"Range": "bytes=2-5"})
    assert code == 206 and body == b"2345"
    hs = {k.lower(): v for k, v in headers.items()}
    assert hs["content-range"] == "bytes 2-5/10"
    assert hs["accept-ranges"] == "bytes"
    # suffix range
    code, _, body = _get(server, cookie, "/api/files/content?path=range.bin", headers={"Range": "bytes=-3"})
    assert code == 206 and body == b"789"
    # unsatisfiable -> 416 with the right header
    code, headers, _ = _get(server, cookie, "/api/files/content?path=range.bin", headers={"Range": "bytes=900-999"})
    assert code == 416
    hs = {k.lower(): v for k, v in headers.items()}
    assert hs["content-range"] == "bytes */10"
    # multi-range refused as invalid
    code, _, _ = _get(server, cookie, "/api/files/content?path=range.bin", headers={"Range": "bytes=0-1,3-4"})
    assert code == 416
    # full download + curl -C - style resume still works
    code, _, body = _get(server, cookie, "/api/files/content?path=range.bin")
    assert code == 200 and body == data
    code, headers, body = _get(server, cookie, "/api/files/content?path=range.bin", headers={"Range": "bytes=5-"})
    assert code == 206 and body == b"56789"
    # legacy endpoint works too
    code, _, body = _get(server, cookie, "/api/download?name=range.bin")
    assert code == 200 and body == data


def test_safe_content_disposition_unicode(server):
    cookie = _login(server)
    name = "café ☕.txt"
    (server.files_dir / name).write_bytes(b"u")
    code, headers, _ = _get(server, cookie, f"/api/files/content?path={urllib.parse.quote(name)}")
    assert code == 200
    hs = {k.lower(): v for k, v in headers.items()}
    assert "filename*=UTF-8''" in hs["content-disposition"]  # RFC 5987 for Android/iOS (#86)
    assert "\r" not in hs["content-disposition"] and "\n" not in hs["content-disposition"]


def test_quota_root_refuses(server):
    cookie = _login(server)
    code, _, body = _post(
        server,
        cookie,
        "/api/files/upload/init",
        {"root": "Tiny", "name": "huge.bin", "size": 4096, "chunk_size": 65536},
    )
    assert code == 507
    assert "quota" in json.loads(body)["error"]  # the error explains the limit (#89)


def test_search_over_http(server):
    cookie = _login(server)
    (server.files_dir / "sub").mkdir(exist_ok=True)
    (server.files_dir / "sub" / "findme.txt").write_bytes(b"here")
    code, _, body = _get(server, cookie, "/api/files/search?q=findme")
    j = json.loads(body)
    assert code == 200 and len(j["matches"]) == 1
    assert j["matches"][0]["dir"] == "sub"


def test_archive_create_extract_over_http(server):
    cookie = _login(server)
    (server.files_dir / "arch").mkdir(exist_ok=True)
    (server.files_dir / "arch" / "inner.txt").write_bytes(b"zipped")
    code, _, body = _post(
        server, cookie, "/api/files/archive", {"root": "r0", "paths": ["arch"], "format": "zip", "name": "arch.zip"}
    )
    j = json.loads(body)
    assert code == 200 and j["path"] == "arch.zip"
    with zipfile.ZipFile(server.files_dir / "arch.zip") as z:
        assert "arch/inner.txt" in z.namelist()
    # extract into a fresh dir
    (server.files_dir / "unpack").mkdir(exist_ok=True)
    code, _, body = _post(server, cookie, "/api/files/extract", {"root": "r0", "path": "arch.zip", "dest": "unpack"})
    j = json.loads(body)
    assert code == 200 and j["files"] >= 1
    assert (server.files_dir / "unpack" / "arch" / "inner.txt").read_bytes() == b"zipped"
    # unsupported format fails with a clear message (#85)
    code, _, body = _post(server, cookie, "/api/files/archive", {"root": "r0", "paths": ["arch"], "format": "rar"})
    assert code == 400 and "unsupported" in json.loads(body)["error"]


def test_tar_gz_extract_over_http(server):
    cookie = _login(server)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        raw = b"tar content"
        info = tarfile.TarInfo("d/file.txt")
        info.size = len(raw)
        t.addfile(info, io.BytesIO(raw))
    (server.files_dir / "t.tgz").write_bytes(buf.getvalue())
    (server.files_dir / "tarout").mkdir(exist_ok=True)
    code, _, body = _post(server, cookie, "/api/files/extract", {"root": "r0", "path": "t.tgz", "dest": "tarout"})
    j = json.loads(body)
    assert code == 200 and j["files"] == 1
    assert (server.files_dir / "tarout" / "d" / "file.txt").read_bytes() == b"tar content"


def test_zip_slip_over_http(server):
    cookie = _login(server)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.txt", b"evil")
    (server.files_dir / "slip.zip").write_bytes(buf.getvalue())
    (server.files_dir / "slipout").mkdir(exist_ok=True)
    code, _, _ = _post(server, cookie, "/api/files/extract", {"root": "r0", "path": "slip.zip", "dest": "slipout"})
    assert code in (400, 403)
    assert not (server.files_dir.parent / "escape.txt").exists()


def test_chmod_over_http_and_executable(server):
    cookie = _login(server)
    (server.files_dir / "script.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    code, _, body = _post(server, cookie, "/api/files/chmod", {"root": "r0", "path": "script.sh", "mode": "755"})
    assert code == 200
    assert os.access(server.files_dir / "script.sh", os.X_OK)
    # setuid is refused and explained (#90)
    code, _, body = _post(server, cookie, "/api/files/chmod", {"root": "r0", "path": "script.sh", "mode": "4755"})
    assert code == 403 and "setuid" in json.loads(body)["error"]


def test_listing_shows_mode_owner(server):
    cookie = _login(server)
    (server.files_dir / "modey.txt").write_bytes(b"m")
    code, _, body = _get(server, cookie, "/api/files")
    j = json.loads(body)
    e = next(x for x in j["entries"] if x["name"] == "modey.txt")
    assert e["mode"].startswith("-") and "owner" in e and "group" in e  # #90 display


def test_audit_over_http(server):
    cookie = _login(server)
    (server.files_dir / "audited.txt").write_bytes(b"a")
    _post(server, cookie, "/api/files/delete", {"root": "r0", "paths": ["audited.txt"]})
    code, _, body = _get(server, cookie, "/api/files/audit?limit=50")
    j = json.loads(body)
    assert code == 200
    actions = [e["action"] for e in j["events"]]
    assert "delete" in actions  # every mutating op produced one record (#92)
    ev = next(e for e in j["events"] if e["action"] == "delete")
    assert ev["user"] == "admin" and ev["ip"]
    assert "path" in ev and ev["result"] == "ok"


def test_thumbnail_over_http_small_png(server):
    cookie = _login(server)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea72d9a3c0000000049454e44ae426082"
    )
    (server.files_dir / "dot.png").write_bytes(png)
    code, headers, body = _get(server, cookie, "/api/files/thumb?path=dot.png&size=32")
    assert code == 200
    hs = {k.lower(): v for k, v in headers.items()}
    assert hs["content-type"] == "image/png"
    # SVG is refused for inline rendering (#82)
    (server.files_dir / "bad.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    code, _, _ = _get(server, cookie, "/api/files/thumb?path=bad.svg&size=32")
    assert code == 415


def test_share_target_multipart_upload(server):
    cookie = _login(server)
    b = "sharebound123"
    payload = (
        (
            f"--{b}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="shared photo.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + b"\xff\xd8\xff\xe0jpegdata"
        + f"\r\n--{b}--\r\n".encode()
    )
    code, headers, body = server.request(
        "/share",
        cookie=cookie,
        raw=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"},
    )
    assert (server.files_dir / "shared photo.jpg").read_bytes() == b"\xff\xd8\xff\xe0jpegdata"
    # urllib follows the 303; check the status code raw (no redirect)
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    conn.request(
        "POST",
        "/share",
        body=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={b}", "Cookie": cookie},
    )
    r = conn.getresponse()
    assert r.status == 303 and r.headers["Location"] == "/"  # lands back on the shell
    r.read()


def test_share_target_requires_auth_and_writable(server):
    b = "x2"
    payload = (
        f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="nope.txt"\r\n\r\ndata\r\n--{b}--\r\n'
    ).encode()
    code, _, _ = server.request("/share", raw=payload, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    assert code == 401
    cookie = _login(server)
    code, _, _ = server.request(
        "/share?root=Locked",
        cookie=cookie,
        raw=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"},
    )
    assert code == 403  # read-only root refused server-side (#84)


def test_upload_cap_enforced_on_session(server):
    cookie = _login(server)
    code, _, body = _post(
        server,
        cookie,
        "/api/files/upload/init",
        {"root": "r0", "name": "toobig.bin", "size": 5 * 1024**3, "chunk_size": 65536},
    )
    assert code == 413  # the 2 GB cap still holds (v2.0.1 behaviour kept)


def test_legacy_flat_upload_still_works(server):
    cookie = _login(server)
    code, _, body = server.request("/api/upload?name=legacy.txt", cookie=cookie, raw=b"legacy")
    assert code == 200 and json.loads(body)["name"] == "legacy.txt"
    code, _, body = _get(server, cookie, "/api/files")
    names = [e["name"] for e in json.loads(body)["entries"]]
    assert "legacy.txt" in names
