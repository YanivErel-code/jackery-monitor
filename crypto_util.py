"""
Tiny AES-256-GCM helper shared by bridge.py and server.py for at-rest
encryption of secrets stored in /data (Jackery credentials, Kasa cloud
credentials, anything else we add later).

The encryption key lives at /data/.jackery-creds.key (mode 0600, generated
on first use by whichever process touches it first). Reusing the same key
across all secrets means the operator only has to manage one — losing it
makes everything unrecoverable, but that's correct: the data they protect
becomes useless without it.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets

from Crypto.Cipher import AES

log = logging.getLogger("crypto_util")

KEY_PATH = os.environ.get("JACKERY_AT_REST_KEY_FILE", "/data/.jackery-creds.key")
ENV_TAG = "v1"


def _get_or_create_key() -> bytes:
    try:
        with open(KEY_PATH, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
        log.warning("at-rest key %s has wrong length (%d); regenerating", KEY_PATH, len(key))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("at-rest key %s unreadable (%s); regenerating", KEY_PATH, e)
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(KEY_PATH) or ".", exist_ok=True)
    tmp = KEY_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(key)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, KEY_PATH)
    log.info("generated new at-rest encryption key at %s", KEY_PATH)
    return key


def encrypt(plaintext: bytes) -> dict:
    key = _get_or_create_key()
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return {
        "v": ENV_TAG,
        "alg": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
        "tag":   base64.b64encode(tag).decode(),
        "ct":    base64.b64encode(ct).decode(),
    }


def decrypt(blob: dict) -> bytes | None:
    try:
        key = _get_or_create_key()
        nonce = base64.b64decode(blob["nonce"])
        tag   = base64.b64decode(blob["tag"])
        ct    = base64.b64decode(blob["ct"])
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    except Exception as e:
        log.error("decrypt failed: %s", e)
        return None
