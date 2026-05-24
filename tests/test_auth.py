"""Unit tests for auth — password hashing, session tokens, persistence."""

from __future__ import annotations

import importlib
import time


def test_hash_and_verify_password_round_trip(isolated_data):
    import auth
    import crypto_util
    importlib.reload(auth)

    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("pbkdf2_sha256:")
    assert auth.verify_password("correct horse battery staple", h) is True
    assert auth.verify_password("wrong", h) is False


def test_password_format_invalid_returns_false(isolated_data):
    import auth
    importlib.reload(auth)
    assert auth.verify_password("anything", "garbage") is False
    assert auth.verify_password("x", "scheme:1:salt") is False  # missing hash field


def test_verify_accepts_legacy_lower_iter_hashes(isolated_data):
    """A stored hash with the old 200k iteration count must still verify.
    The encoded format embeds the iteration count, so a bump to the
    module-level constant cannot lock out an existing user."""
    import base64
    import hashlib
    import secrets

    import auth
    importlib.reload(auth)

    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", b"hunter2!", salt, 200_000, 32)
    legacy = (
        f"pbkdf2_sha256:200000:"
        f"{base64.b64encode(salt).decode()}:"
        f"{base64.b64encode(dk).decode()}"
    )
    assert auth.verify_password("hunter2!", legacy) is True
    assert auth.verify_password("wrong", legacy) is False


def test_save_and_load_user(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    assert auth.has_user() is False
    assert auth.load_user() is None

    assert auth.save_user("alice", "hunter2!") is True
    assert auth.has_user() is True

    user = auth.load_user()
    assert user is not None
    assert user["username"] == "alice"
    assert auth.verify_password("hunter2!", user["password_hash"]) is True


def test_clear_user_removes_file(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    auth.save_user("alice", "hunter2!")
    assert auth.has_user()
    auth.clear_user()
    assert not auth.has_user()
    assert auth.load_user() is None


def test_session_round_trip(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    token = auth.make_session("alice")
    payload = auth.verify_session(token)
    assert payload is not None
    assert payload["u"] == "alice"


def test_session_rejects_tampered_token(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    token = auth.make_session("alice")
    # Mutate the body part — signature won't match.
    body, sig = token.rsplit(".", 1)
    bad = body + "X." + sig
    assert auth.verify_session(bad) is None


def test_session_rejects_expired_token(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    # Negative TTL → expires "in the past" immediately.
    token = auth.make_session("alice", ttl=-1)
    assert auth.verify_session(token) is None


def test_session_rejects_missing_or_garbage(isolated_data):
    import auth
    import crypto_util
    importlib.reload(crypto_util)
    importlib.reload(auth)

    assert auth.verify_session(None) is None
    assert auth.verify_session("") is None
    assert auth.verify_session("nope") is None
    assert auth.verify_session("a.b.c") is None  # too many dots
