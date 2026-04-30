"""
Anthropic API key for the optional Claude smart-charge narrator.

Same encrypted-on-disk pattern as kasa_creds — AES-256-GCM via crypto_util,
saved to /data/anthropic-creds.json. The key is read fresh on each
narration call so a rotation in the UI takes effect on the next tick
without a server restart.

Path is overridable via JACKERY_ANTHROPIC_CREDS_FILE for tests.
"""

from __future__ import annotations

import json
import logging
import os

import crypto_util

log = logging.getLogger("anthropic_creds")

PATH = os.environ.get(
    "JACKERY_ANTHROPIC_CREDS_FILE", "/data/anthropic-creds.json")


def has_key() -> bool:
    """Cheap existence check — does NOT decrypt the key."""
    try:
        return os.path.getsize(PATH) > 0
    except OSError:
        return False


def load() -> str | None:
    """Return the decrypted API key, or None if not saved / unreadable."""
    try:
        with open(PATH) as f:
            blob = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("anthropic creds file %s unreadable: %s", PATH, e)
        return None
    if not isinstance(blob, dict) or "ct" not in blob:
        return None
    pt = crypto_util.decrypt(blob)
    if pt is None:
        return None
    try:
        d = json.loads(pt.decode())
    except Exception as e:
        log.error("anthropic creds invalid JSON after decrypt: %s", e)
        return None
    key = d.get("api_key")
    return str(key) if key else None


def save(api_key: str) -> bool:
    payload = json.dumps({"api_key": api_key}).encode()
    blob = crypto_util.encrypt(payload)
    try:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, PATH)
        return True
    except Exception as e:
        log.error("failed to save anthropic creds: %s", e)
        return False


def clear() -> bool:
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        log.error("failed to delete anthropic creds: %s", e)
        return False
