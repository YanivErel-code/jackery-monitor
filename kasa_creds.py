"""
Kasa cloud-account credentials (email + password).

Newer Kasa "SMART" devices (KP125M, EP25, KP405, etc.) require auth using
the user's Kasa app account, even on the local network. We persist the
credentials encrypted at /data/kasa-creds.json (AES-256-GCM via crypto_util)
and provide a simple load/save API. kasa_client.py uses load() to feed
Credentials(...) into python-kasa.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import crypto_util

log = logging.getLogger("kasa_creds")

PATH = os.environ.get("JACKERY_KASA_CREDS_FILE", "/data/kasa-creds.json")


def has_credentials() -> bool:
    """Cheap existence check — does NOT decrypt."""
    try:
        return os.path.getsize(PATH) > 0
    except OSError:
        return False


def load() -> Optional[dict]:
    """Return {email, password} or None if no creds saved / unreadable."""
    try:
        with open(PATH) as f:
            blob = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("kasa creds file %s unreadable: %s", PATH, e)
        return None
    if not isinstance(blob, dict) or "ct" not in blob:
        return None
    pt = crypto_util.decrypt(blob)
    if pt is None:
        return None
    try:
        d = json.loads(pt.decode())
    except Exception as e:
        log.error("kasa creds invalid JSON after decrypt: %s", e)
        return None
    if d.get("email") and d.get("password"):
        return {"email": str(d["email"]), "password": str(d["password"])}
    return None


def save(email: str, password: str) -> bool:
    payload = json.dumps({"email": email, "password": password}).encode()
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
        log.error("failed to save kasa creds: %s", e)
        return False


def clear() -> bool:
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        log.error("failed to delete kasa creds: %s", e)
        return False
