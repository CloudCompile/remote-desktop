"""User store, roles and migration (#111/#112) — in-process, tmp store."""

import os

import pytest

from remote_desktop import config, users


@pytest.fixture
def store(tmp_path, monkeypatch):
    udir = tmp_path / "users"
    # a realistic deployment: the legacy .passhash exists (#111 migration source)
    import crypt as _crypt

    pf = tmp_path / ".passhash"
    pf.write_text(_crypt.crypt("legacy-password-1", "$6$abcdef12") + "\n")
    monkeypatch.setattr(config, "PASS_FILE", str(pf))
    monkeypatch.setitem(config.VALUES, "users_dir", str(udir))
    users.invalidate()
    yield udir
    users.invalidate()


def test_migrate_single_user_creates_default_admin(store):
    # simulate the single-.passhash deployment: the store is absent, the
    # default admin is derived from config (RD_USER); the legacy password
    # keeps working through the store record after migration
    assert not users.store_exists()
    assert users.is_default_admin(config.AUTH_USER)
    assert users.role(config.AUTH_USER) == "admin"
    ok, err = users.create("jules", "correct horse battery", role="viewer")
    assert ok, err
    assert users.verify_password(config.AUTH_USER, "legacy-password-1")


def test_create_and_role_matrix(store):
    ok, err = users.create("jules", "correct horse battery", role="viewer")
    assert ok, err
    ok, err = users.create("kim", "operator-pass-1", role="operator")
    assert ok, err
    assert users.role("jules") == "viewer"
    assert users.is_admin("kim") is False
    assert users.is_admin(config.AUTH_USER) is True


def test_create_validates_usernames_and_roles(store):
    assert not users.create("x", "password123")[0]  # too short
    assert not users.create("1bad", "password123")[0]  # must start with a letter
    assert not users.create("bad name", "password123")[0]
    assert not users.create("jules", "short")[0]  # password too short
    assert not users.create("jules", "password123", role="root")[0]  # unknown role


def test_disable_rejects_last_admin(store):
    ok, err = users.set_status(config.AUTH_USER, "disabled")
    assert not ok and "last active admin" in err


def test_status_change_and_admin_gate(store):
    ok, _ = users.create("jules", "correct horse battery", role="admin")
    assert ok
    # now disabling the default admin is allowed (another active admin exists)
    ok, err = users.set_status(config.AUTH_USER, "disabled")
    assert ok, err
    assert not users.is_active(config.AUTH_USER)
    users.set_status(config.AUTH_USER, "active")


def test_delete_rules(store):
    ok, err = users.delete(config.AUTH_USER)
    assert not ok and "cannot be deleted" in err
    users.create("jules", "correct horse battery", role="operator")
    ok, err = users.delete("jules")
    assert ok, err
    assert not users.exists("jules")


def test_password_roundtrip_and_legacy_sync(store, monkeypatch):
    pf = store.parent / ".passhash"
    import crypt

    pf.write_text(crypt.crypt("old-password-1", "$6$abcdef12") + "\n")
    monkeypatch.setattr(config, "PASS_FILE", str(pf))
    users.create("jules", "correct horse battery", role="operator")
    assert users.verify_password("jules", "correct horse battery")
    assert not users.verify_password("jules", "wrong")
    ok, err = users.set_password("jules", "fresh-password-9")
    assert ok, err
    assert users.verify_password("jules", "fresh-password-9")
    assert not users.verify_password("jules", "correct horse battery")


def test_list_public_never_leaks_hashes(store):
    users.create("jules", "correct horse battery", role="viewer")
    blob = str(users.list_public())
    assert "correct horse battery" not in blob and "$6$" not in blob
    jules = [u for u in users.list_public() if u["username"] == "jules"][0]
    assert jules["role"] == "viewer" and jules["status"] == "active"
