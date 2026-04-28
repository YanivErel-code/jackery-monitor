"""Unit tests for crypto_util — AES-256-GCM round-trip and tamper detection."""

from __future__ import annotations

import importlib


def test_encrypt_decrypt_round_trip(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)

    plaintext = b"hello, secret world"
    blob = crypto_util.encrypt(plaintext)
    assert blob["alg"] == "AES-256-GCM"
    assert blob["v"] == "v1"
    assert "ct" in blob and "nonce" in blob and "tag" in blob

    out = crypto_util.decrypt(blob)
    assert out == plaintext


def test_decrypt_unicode_round_trip(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)

    plaintext = "🔋 unicode + special chars: \\n \"quoted\" 'single'".encode()
    blob = crypto_util.encrypt(plaintext)
    assert crypto_util.decrypt(blob) == plaintext


def test_tampered_ciphertext_returns_none(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)

    blob = crypto_util.encrypt(b"sensitive data")
    # Flip a bit in the ciphertext — the GCM tag check should reject it.
    bad_ct = blob["ct"][:-2] + ("XX" if blob["ct"][-2:] != "XX" else "YY")
    bad_blob = {**blob, "ct": bad_ct}
    assert crypto_util.decrypt(bad_blob) is None


def test_tampered_tag_returns_none(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)

    blob = crypto_util.encrypt(b"sensitive data")
    bad_blob = {**blob, "tag": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
    assert crypto_util.decrypt(bad_blob) is None


def test_decrypt_with_different_key_fails(isolated_data, tmp_path, monkeypatch):
    import crypto_util
    importlib.reload(crypto_util)

    blob = crypto_util.encrypt(b"protected")
    # Point at a different key file → fresh key generated → decrypt fails.
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key2"))
    importlib.reload(crypto_util)
    assert crypto_util.decrypt(blob) is None


def test_key_file_persisted_with_correct_size(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)

    crypto_util.encrypt(b"force key generation")
    key_path = isolated_data / ".key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32  # AES-256
