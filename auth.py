"""
Single-user app-level authentication for the Jackery Monitor dashboard.

This is layered on top of (or as an alternative to) Cloudflare Access /
Tailscale / etc. — useful when the dashboard is exposed to the internet
without an edge-auth wall.

Storage: /data/auth.json (encrypted via crypto_util). Holds {username,
password_hash, created_at}. The hash format is `pbkdf2_sha256:iter:salt:hash`
(all base64-encoded). Sessions are HMAC-signed tokens in an HttpOnly cookie;
no server-side session table to worry about.

There's at most one user — this is a personal monitor, not a multi-tenant
app. First visit hits the /setup flow if no user exists yet; from then on,
/login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import crypto_util

log = logging.getLogger("auth")

AUTH_PATH = os.environ.get("JACKERY_AUTH_FILE", "/data/auth.json")

PBKDF2_ITER = 200_000
PBKDF2_DKLEN = 32
SALT_BYTES = 16

SESSION_TTL_S = 30 * 24 * 3600  # 30 days
COOKIE_NAME = "jackery_session"


# ---------- password hashing (stdlib only) ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ITER, PBKDF2_DKLEN)
    return f"pbkdf2_sha256:{PBKDF2_ITER}:{base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, hash_b64 = stored.split(":", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iters_i = int(iters)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             iters_i, len(expected))
    # constant-time compare to avoid timing leaks
    return hmac.compare_digest(dk, expected)


# ---------- user persistence ----------
def has_user() -> bool:
    return load_user() is not None


def load_user() -> dict | None:
    try:
        with open(AUTH_PATH) as f:
            blob = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("auth file %s unreadable: %s", AUTH_PATH, e)
        return None
    if not isinstance(blob, dict) or "ct" not in blob:
        return None
    pt = crypto_util.decrypt(blob)
    if pt is None:
        return None
    try:
        d = json.loads(pt.decode())
    except Exception:
        return None
    if d.get("username") and d.get("password_hash"):
        return d
    return None


def save_user(username: str, password: str) -> bool:
    payload = json.dumps({
        "username": username,
        "password_hash": hash_password(password),
        "created_at": time.time(),
    }).encode()
    blob = crypto_util.encrypt(payload)
    try:
        os.makedirs(os.path.dirname(AUTH_PATH) or ".", exist_ok=True)
        tmp = AUTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, AUTH_PATH)
        return True
    except Exception as e:
        log.error("failed to save auth: %s", e)
        return False


def clear_user() -> bool:
    try:
        os.remove(AUTH_PATH)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


# ---------- session tokens (HMAC, stateless) ----------
def _session_secret() -> bytes:
    """Use the same /data/.jackery-creds.key everything else is keyed off.
       crypto_util manages it; we just need stable bytes for HMAC."""
    return crypto_util._get_or_create_key()


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_session(username: str, ttl: int = SESSION_TTL_S) -> str:
    """Return an HMAC-signed token: payload is {u, exp, n}; stateless."""
    payload = {
        "u": username,
        "exp": int(time.time()) + ttl,
        "n": secrets.token_hex(8),
    }
    body = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64u_encode(sig)


def verify_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        body, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest()
    try:
        got = _b64u_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except Exception:
        return None
    if not isinstance(payload, dict) or "u" not in payload or "exp" not in payload:
        return None
    if int(payload["exp"]) < int(time.time()):
        return None
    return payload
