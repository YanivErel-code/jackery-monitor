"""
Remote-NAS backup destination credentials (SMB / CIFS).

Holds the host, share path, sub-directory, username, and password used by
backup.py to mount the remote share. Persisted encrypted at
/data/backup-creds.json (AES-256-GCM via crypto_util) — same pattern as
kasa_creds.py and anthropic_creds.py.

Why a dedicated module instead of cramming the password into
settings.json: settings.json is plain JSON (no encryption), and shipping
a SMB password in plain text would be a regression. Mirroring the
existing creds-module pattern keeps the threat model uniform.
"""

from __future__ import annotations

import json
import logging
import os

import crypto_util

log = logging.getLogger("backup_creds")

PATH = os.environ.get("JACKERY_BACKUP_CREDS_FILE", "/data/backup-creds.json")

# Fields stored in the encrypted blob. Keep this list canonical so
# load() and save() can't drift.
_FIELDS = ("host", "share", "subdir", "username", "password", "domain")


def has_credentials() -> bool:
    """Cheap existence check — does NOT decrypt."""
    try:
        return os.path.getsize(PATH) > 0
    except OSError:
        return False


def load() -> dict | None:
    """Return the saved config dict, or None if missing/unreadable.

    Shape:
        {
          "host":     "192.168.1.42",
          "share":    "/volume1/backups",
          "subdir":   "jackery-monitor",
          "username": "backupuser",
          "password": "...",
          "domain":   "WORKGROUP",   # optional, defaults to WORKGROUP
        }
    """
    try:
        with open(PATH) as f:
            blob = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("backup creds file %s unreadable: %s", PATH, e)
        return None
    if not isinstance(blob, dict) or "ct" not in blob:
        return None
    pt = crypto_util.decrypt(blob)
    if pt is None:
        return None
    try:
        d = json.loads(pt.decode())
    except Exception as e:
        log.error("backup creds invalid JSON after decrypt: %s", e)
        return None
    if not isinstance(d, dict):
        return None
    # Required fields must be present and non-empty.
    if not all(d.get(k) for k in ("host", "share", "username", "password")):
        return None
    return {
        "host": str(d["host"]),
        "share": str(d["share"]),
        "subdir": str(d.get("subdir") or "jackery-monitor"),
        "username": str(d["username"]),
        "password": str(d["password"]),
        "domain": str(d.get("domain") or "WORKGROUP"),
    }


def save(*, host: str, share: str, username: str, password: str,
         subdir: str = "jackery-monitor",
         domain: str = "WORKGROUP") -> bool:
    """Persist SMB credentials. All fields except subdir/domain are
    required. Returns True on success."""
    host = (host or "").strip()
    share = (share or "").strip()
    subdir = (subdir or "jackery-monitor").strip()
    username = (username or "").strip()
    domain = (domain or "WORKGROUP").strip()
    if not (host and share and username and password):
        return False
    payload = json.dumps({
        "host": host, "share": share, "subdir": subdir,
        "username": username, "password": password, "domain": domain,
    }).encode()
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
        log.error("failed to save backup creds: %s", e)
        return False


def public_view() -> dict | None:
    """Same as load() but with the password redacted — for UI display.
    Returns None if no creds saved."""
    d = load()
    if not d:
        return None
    out = dict(d)
    out["password"] = ""
    out["has_password"] = True
    return out


def clear() -> bool:
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        log.error("failed to delete backup creds: %s", e)
        return False
