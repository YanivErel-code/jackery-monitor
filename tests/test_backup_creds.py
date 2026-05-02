"""Tests for backup_creds — encrypted SMB credentials."""
from __future__ import annotations

import importlib
import json
import os

import pytest


@pytest.fixture()
def fresh_creds(tmp_path, monkeypatch):
    """Reload backup_creds with a tmp_path-backed file."""
    creds_file = tmp_path / "backup-creds.json"
    monkeypatch.setenv("JACKERY_BACKUP_CREDS_FILE", str(creds_file))
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key"))
    # Reload after setting env vars so the module-level PATH constant
    # picks up the patched location.
    import crypto_util
    importlib.reload(crypto_util)
    import backup_creds as bc
    importlib.reload(bc)
    return bc, creds_file


def test_round_trip_save_and_load(fresh_creds):
    bc, _path = fresh_creds
    assert not bc.has_credentials()
    assert bc.load() is None

    ok = bc.save(host="nas.local", share="backups", username="alice",
                 password="hunter2", subdir="jackery", domain="WORKGROUP")
    assert ok
    assert bc.has_credentials()

    d = bc.load()
    assert d is not None
    assert d["host"] == "nas.local"
    assert d["share"] == "backups"
    assert d["username"] == "alice"
    assert d["password"] == "hunter2"
    assert d["subdir"] == "jackery"
    assert d["domain"] == "WORKGROUP"


def test_save_rejects_missing_required_fields(fresh_creds):
    bc, _ = fresh_creds
    assert bc.save(host="", share="x", username="u", password="p") is False
    assert bc.save(host="x", share="", username="u", password="p") is False
    assert bc.save(host="x", share="y", username="", password="p") is False
    assert bc.save(host="x", share="y", username="u", password="") is False


def test_password_is_encrypted_at_rest(fresh_creds):
    """Sanity: the on-disk file must NOT contain the plaintext password."""
    bc, path = fresh_creds
    bc.save(host="nas.local", share="backups",
            username="alice", password="super-secret-password",
            subdir="jackery", domain="WORKGROUP")
    raw = path.read_text()
    assert "super-secret-password" not in raw
    # Should be a JSON envelope with at least a ciphertext field.
    blob = json.loads(raw)
    assert isinstance(blob, dict)
    assert "ct" in blob


def test_public_view_redacts_password(fresh_creds):
    bc, _ = fresh_creds
    bc.save(host="nas.local", share="backups",
            username="alice", password="hunter2")
    pv = bc.public_view()
    assert pv is not None
    assert pv["host"] == "nas.local"
    assert pv["password"] == ""
    assert pv["has_password"] is True


def test_clear_removes_file(fresh_creds):
    bc, path = fresh_creds
    bc.save(host="x", share="y", username="u", password="p")
    assert path.exists()
    bc.clear()
    assert not path.exists()
    # Idempotent.
    bc.clear()


def test_load_returns_none_for_missing_file(fresh_creds):
    bc, _ = fresh_creds
    assert bc.load() is None
    assert bc.public_view() is None


def test_file_permissions_are_tight(fresh_creds):
    """The encrypted creds file must be 0600 to match the at-rest threat
    model (other users on the same NAS can't read it)."""
    bc, path = fresh_creds
    bc.save(host="x", share="y", username="u", password="p")
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600
