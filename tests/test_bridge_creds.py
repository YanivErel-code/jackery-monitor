"""Bridge credential storage — encryption + legacy-plaintext migration.

bridge.py persists Jackery cloud creds at /data/jackery-creds.json with
AES-256-GCM. Older installations may still have a plaintext file from
before encryption was added; loading must accept those AND re-encrypt
them in place so the plaintext doesn't linger on disk."""
from __future__ import annotations

import importlib
import json


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
    }


def test_missing_creds_file_returns_none(tmp_path, monkeypatch):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    assert bridge._load_creds_file() is None
