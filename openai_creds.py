"""
OpenAI API key for the optional AI advisor / narrator, when the user
selects OpenAI as the active AI provider (see anthropic_prefs.get_provider).

Same encrypted-on-disk pattern as anthropic_creds — AES-256-GCM via
crypto_util, saved to /data/openai-creds.json. The key is read fresh on
each call so a rotation in the UI takes effect on the next tick without a
server restart.

Path is overridable via JACKERY_OPENAI_CREDS_FILE for tests.
"""

from __future__ import annotations

import json
import logging
import os

import crypto_util

log = logging.getLogger("openai_creds")

PATH = os.environ.get(
    "JACKERY_OPENAI_CREDS_FILE", "/data/openai-creds.json")


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
        log.warning("openai creds file %s unreadable: %s", PATH, e)
        return None
    if not isinstance(blob, dict) or "ct" not in blob:
        return None
    pt = crypto_util.decrypt(blob)
    if pt is None:
        return None
    try:
        d = json.loads(pt.decode())
    except Exception as e:
        log.error("openai creds invalid JSON after decrypt: %s", e)
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
        log.error("failed to save openai creds: %s", e)
        return False


def clear() -> bool:
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        log.error("failed to delete openai creds: %s", e)
        return False
