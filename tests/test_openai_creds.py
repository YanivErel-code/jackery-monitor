"""Tests for openai_creds — encrypted OpenAI API key (mirrors anthropic_creds)."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def fresh_creds(tmp_path, monkeypatch):
    creds_file = tmp_path / "openai-creds.json"
    monkeypatch.setenv("JACKERY_OPENAI_CREDS_FILE", str(creds_file))
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key"))
    import crypto_util
    importlib.reload(crypto_util)
    import openai_creds as oc
    importlib.reload(oc)
    return oc, creds_file


def test_round_trip(fresh_creds):
    oc, _ = fresh_creds
    assert not oc.has_key()
    assert oc.load() is None
    assert oc.save("sk-proj-abc123")
    assert oc.has_key()
    assert oc.load() == "sk-proj-abc123"


def test_encrypted_on_disk(fresh_creds):
    oc, path = fresh_creds
    oc.save("sk-secret-value")
    raw = path.read_text()
    assert "sk-secret-value" not in raw   # ciphertext only
    assert "ct" in raw


def test_clear(fresh_creds):
    oc, _ = fresh_creds
    oc.save("sk-x")
    assert oc.has_key()
    assert oc.clear()
    assert not oc.has_key()
    assert oc.load() is None
    # Clearing when already absent is a no-op success.
    assert oc.clear()
