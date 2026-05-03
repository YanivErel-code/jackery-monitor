"""
Remote-NAS backup destination credentials.

Holds the connection parameters for whatever transport ships /data
snapshots to the remote — currently SMB/CIFS, rsync over SSH, or rsync
to a daemon (rsyncd). Persisted encrypted at /data/backup-creds.json
(AES-256-GCM via crypto_util) — same pattern as kasa_creds.py and
anthropic_creds.py.

Why a dedicated module instead of cramming the password into
settings.json: settings.json is plain JSON (no encryption), and shipping
a SMB password (or, worse, an SSH private key) in plain text would be a
regression. Mirroring the existing creds-module pattern keeps the
threat model uniform.

Transport field (added with rsync support):
  * "smb"         — fields: host, share, subdir, username, password, domain
  * "rsync_ssh"   — fields: host, ssh_user, ssh_key, target_dir
  * "rsyncd"      — fields: host, rsync_module, target_subpath,
                           rsyncd_user, rsyncd_password
  * "rsyncd_ssh"  — fields: host, ssh_port, ssh_user, ssh_password,
                           rsync_module, target_subpath
                   (rsyncd protocol tunnelled inside SSH — what
                   Synology Hyper Backup speaks to UniFi UNAS and
                   any other "rsync-compatible server" appliance
                   that rejects unencrypted port-873 traffic.)

Existing creds saved before the rsync work was done don't carry a
transport field — load() defaults it to "smb" so they keep working
with no migration. Saved fields for transports the caller doesn't
populate are stored as empty strings, which makes round-tripping a
single creds blob through the UI form trivial regardless of which
transport is in use.
"""

from __future__ import annotations

import json
import logging
import os

import crypto_util

log = logging.getLogger("backup_creds")

PATH = os.environ.get("JACKERY_BACKUP_CREDS_FILE", "/data/backup-creds.json")

VALID_TRANSPORTS = ("smb", "rsync_ssh", "rsyncd", "rsyncd_ssh")

# Required fields per transport. All must be non-empty for save() to
# succeed. Other fields are stored as empty strings so the encrypted
# blob shape stays uniform.
_REQUIRED = {
    "smb": ("host", "share", "username", "password"),
    "rsync_ssh": ("host", "ssh_user", "ssh_key", "target_dir"),
    "rsyncd": ("host", "rsync_module", "rsyncd_user", "rsyncd_password"),
    "rsyncd_ssh": ("host", "ssh_user", "ssh_password", "rsync_module"),
}

# Sensitive fields — redacted by public_view() before sending to the UI.
# ssh_key is a private key (treat as harder-to-reset than a password)
# but since the UI lets the user paste a new one we surface a
# "has_ssh_key" flag the same way we do for has_password. ssh_password
# is the SSH-tunnelled rsyncd transport's password.
_SECRET_FIELDS = ("password", "rsyncd_password", "ssh_key", "ssh_password")


def has_credentials() -> bool:
    """Cheap existence check — does NOT decrypt."""
    try:
        return os.path.getsize(PATH) > 0
    except OSError:
        return False


def _normalize(d: dict) -> dict:
    """Coerce a freshly-decrypted blob (or freshly-built save payload)
    into the canonical shape: every transport's fields present, with
    sensible defaults. Strings are stripped where stripping is safe;
    secrets are kept verbatim (leading/trailing whitespace in a
    private key would change the key's identity)."""
    transport = (d.get("transport") or "smb").strip() or "smb"
    if transport not in VALID_TRANSPORTS:
        # An unknown transport should NOT crash load() — it might be a
        # creds blob written by a newer version. Fall back to smb so
        # the existing UX (mostly) still works; the caller can override.
        transport = "smb"
    return {
        "transport": transport,
        "host": str(d.get("host") or "").strip(),
        # SMB
        "share": str(d.get("share") or "").strip(),
        "subdir": str(d.get("subdir") or "jackery-monitor").strip(),
        "username": str(d.get("username") or "").strip(),
        "password": str(d.get("password") or ""),
        "domain": str(d.get("domain") or "WORKGROUP").strip(),
        # rsync over SSH (filesystem-path style, key auth)
        "ssh_user": str(d.get("ssh_user") or "").strip(),
        "ssh_key": str(d.get("ssh_key") or ""),
        "target_dir": str(d.get("target_dir") or "").strip(),
        # rsyncd (port 873, plaintext)
        "rsync_module": str(d.get("rsync_module") or "").strip(),
        "target_subpath": str(d.get("target_subpath") or "").strip(),
        "rsyncd_user": str(d.get("rsyncd_user") or "").strip(),
        "rsyncd_password": str(d.get("rsyncd_password") or ""),
        # rsyncd-over-SSH (UniFi UNAS, etc.) — module-style addressing
        # but tunnelled through SSH. Reuses ssh_user + rsync_module from
        # above; ssh_port + ssh_password are unique to this transport.
        "ssh_port": int(d.get("ssh_port") or 22),
        "ssh_password": str(d.get("ssh_password") or ""),
    }


def load() -> dict | None:
    """Return the saved config dict, or None if missing/unreadable.

    Always returns the full canonical schema (every transport's fields
    present), with `transport` defaulting to "smb" for legacy blobs
    written before the rsync work shipped.
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
    norm = _normalize(d)
    # Per-transport sanity check: required fields must all be present.
    # If they aren't, treat the blob as corrupt / unfinished and refuse
    # to surface partial state.
    required = _REQUIRED.get(norm["transport"], ())
    if not all(norm.get(k) for k in required):
        return None
    return norm


def save(**fields) -> bool:
    """Persist remote-backup credentials. Per-transport required fields:

      smb:        host, share, username, password
      rsync_ssh:  host, ssh_user, ssh_key, target_dir
      rsyncd:     host, rsync_module, rsyncd_user, rsyncd_password

    `transport` defaults to "smb" if omitted, which keeps the existing
    SMB-only callers working unchanged. Returns True on success, False
    if any required field is missing or the transport is unknown.
    """
    transport = (str(fields.get("transport") or "smb")).strip() or "smb"
    if transport not in VALID_TRANSPORTS:
        log.warning("save: unknown transport %r", transport)
        return False

    # We normalise BEFORE checking required fields so that whitespace-
    # only inputs are correctly rejected as missing.
    payload = _normalize({**fields, "transport": transport})

    required = _REQUIRED[transport]
    if not all(payload.get(k) for k in required):
        return False

    blob = crypto_util.encrypt(json.dumps(payload).encode())
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
    """Same as load() but with secrets redacted — for UI display.
    Returns None if no creds saved."""
    d = load()
    if not d:
        return None
    out = dict(d)
    out["has_password"] = bool(d.get("password"))
    out["has_ssh_key"] = bool(d.get("ssh_key"))
    out["has_rsyncd_password"] = bool(d.get("rsyncd_password"))
    out["has_ssh_password"] = bool(d.get("ssh_password"))
    for k in _SECRET_FIELDS:
        if k in out:
            out[k] = ""
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
