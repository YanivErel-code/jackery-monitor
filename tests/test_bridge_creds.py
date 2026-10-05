"""Bridge credential storage — encryption + legacy-plaintext migration.

bridge.py persists Jackery cloud creds at /data/jackery-creds.json with
AES-256-GCM. Older installations may still have a plaintext file from
before encryption was added; loading must accept those AND re-encrypt
them in place so the plaintext doesn't linger on disk."""
from __future__ import annotations

import importlib
import json

import pytest


def _fresh_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("JACKERY_CREDS_FILE", str(tmp_path / "jackery-creds.json"))
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key"))
    import crypto_util
    importlib.reload(crypto_util)
    import bridge
    importlib.reload(bridge)
    return bridge


def test_legacy_plaintext_creds_migrate_to_encrypted_in_place(tmp_path, monkeypatch):
    """A plaintext creds.json must (a) still load through the API and
    (b) be rewritten on disk in encrypted form during that same load —
    not deferred to the next user-driven save."""
    creds_path = tmp_path / "jackery-creds.json"
    legacy = {"email": "user@example.com", "password": "hunter2", "region": "EU"}
    creds_path.write_text(json.dumps(legacy))

    bridge = _fresh_bridge(monkeypatch, tmp_path)
    loaded = bridge._load_creds_file()
    assert loaded == {
        "email": "user@example.com",
        "password": "hunter2",
        "region": "EU",
        "api_family": "portable",
    }

    # On-disk file must no longer be plaintext.
    raw = creds_path.read_text()
    assert "user@example.com" not in raw
    assert "hunter2" not in raw
    blob = json.loads(raw)
    assert blob["alg"] == "AES-256-GCM"
    assert all(k in blob for k in ("v", "nonce", "tag", "ct"))

    # And re-loading still returns the same creds (proves the re-encrypt was correct).
    loaded2 = bridge._load_creds_file()
    assert loaded2 == loaded


def test_encrypted_creds_round_trip(tmp_path, monkeypatch):
    """Sanity: save → load returns the same record."""
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    assert bridge._save_creds_file("u@example.com", "pw", "US") is True
    assert bridge._load_creds_file() == {
        "email": "u@example.com",
        "password": "pw",
        "region": "US",
        "api_family": "portable",
    }


def test_missing_creds_file_returns_none(tmp_path, monkeypatch):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    assert bridge._load_creds_file() is None


def test_home_creds_encrypted_round_trip(tmp_path, monkeypatch):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    assert bridge._save_creds_file("home@example.invalid", "home-pw", "EU", "home")
    assert bridge._load_creds_file() == {
        "email": "home@example.invalid", "password": "home-pw",
        "region": "EU", "api_family": "home",
    }
    raw = (tmp_path / "jackery-creds.json").read_text()
    assert "home@example.invalid" not in raw
    assert "home-pw" not in raw


def test_old_encrypted_creds_default_to_portable(tmp_path, monkeypatch):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    legacy = {"email": "old@example.invalid", "password": "pw", "region": "US"}
    (tmp_path / "jackery-creds.json").write_text(
        json.dumps(bridge._encrypt_creds(json.dumps(legacy).encode())))
    assert bridge._load_creds_file() == {**legacy, "api_family": "portable"}


@pytest.mark.parametrize("api_family", [None, "home"])
def test_environment_api_family(monkeypatch, tmp_path, api_family):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    monkeypatch.setenv("JACKERY_EMAIL", "env@example.invalid")
    monkeypatch.setenv("JACKERY_PASSWORD", "pw")
    monkeypatch.setenv("JACKERY_REGION", "EU")
    monkeypatch.delenv("JACKERY_API_FAMILY", raising=False)
    if api_family:
        monkeypatch.setenv("JACKERY_API_FAMILY", api_family)
    assert bridge.load_cloud_credentials()["api_family"] == (api_family or "portable")


def test_keychain_api_family_round_trip(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    monkeypatch.delenv("JACKERY_EMAIL", raising=False)
    monkeypatch.delenv("JACKERY_PASSWORD", raising=False)
    keychain = {}
    monkeypatch.setattr(bridge, "keychain_set", lambda service, key, value:
                        keychain.setdefault(key, value) == value)
    monkeypatch.setattr(bridge, "keychain_get", lambda service, key: keychain.get(key))
    assert bridge.save_cloud_credentials("home@example.invalid", "pw", "EU", "home")[0]
    assert keychain["cloud-api-family"] == "home"
    assert bridge.load_cloud_credentials()["api_family"] == "home"
    keychain.pop("cloud-api-family")
    assert bridge.load_cloud_credentials()["api_family"] == "portable"
