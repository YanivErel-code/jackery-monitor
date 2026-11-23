"""Apple push for the iOS app: alerts and Live Activity updates.

Three parts:

* The signing key from the Apple developer account (a .p8 file), kept
  encrypted in /data/apns.json with its Team ID, Key ID and the app's
  bundle ID.
* The phones that have the app installed, in /data/app-devices.json.
  Each registers itself and its push tokens after sign-in.
* Senders: `send_alert()` for ordinary notifications,
  `live_start()` / `live_update()` / `live_end()` for the charging
  Live Activity on the lock screen, and `reload_widgets()` to make the
  widgets refresh.

Apple only accepts HTTP/2, and tells us when a token is dead (410, or 400
BadDeviceToken); dead tokens are dropped so they are not retried forever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any

import httpx
from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

import crypto_util

log = logging.getLogger("apns")

HOSTS = {"production": "https://api.push.apple.com",
         "development": "https://api.sandbox.push.apple.com"}
PROVIDER_TOKEN_TTL_S = 50 * 60        # Apple rejects tokens older than an hour
LIVE_ACTIVITY_TYPE = "ChargingAttributes"
LIVE_DISMISS_AFTER_S = 15 * 60
LIVE_STALE_AFTER_S = 30 * 60
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,400}$")
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "ExpiredToken", "DeviceTokenNotForTopic"}
TOKEN_FIELDS = ("push_token", "start_token", "activity_token", "widget_token")

_provider_token: tuple[str, float, str] | None = None     # (jwt, issued_at, key_id)


def _config_file() -> str:
    return os.environ.get("JACKERY_APNS_FILE", "/data/apns.json")


def _devices_file() -> str:
    return os.environ.get("JACKERY_APP_DEVICES_FILE", "/data/app-devices.json")


def _read(path: str) -> dict:
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# ---- signing key ------------------------------------------------------

def _load_key(pem: str) -> ECC.EccKey:
    try:
        key = ECC.import_key(pem)
    except (ValueError, TypeError, IndexError):
        raise ValueError("That is not a valid .p8 key file.") from None
    if not key.has_private() or key.curve not in ("NIST P-256", "p256", "P-256", "prime256v1", "secp256r1"):
        raise ValueError("The key must be the private APNs key from Apple (a P-256 .p8 file).")
    return key


def save_key(team_id: str, key_id: str, bundle_id: str, key_pem: str) -> None:
    """Validate and store the Apple push key. Raises ValueError with a
    plain message when something is off."""
    global _provider_token
    team_id, key_id, bundle_id = (str(v or "").strip() for v in (team_id, key_id, bundle_id))
    if not re.match(r"^[A-Z0-9]{10}$", team_id):
        raise ValueError("The Team ID is 10 letters and numbers, such as A1B2C3D4E5.")
    if not re.match(r"^[A-Z0-9]{10}$", key_id):
        raise ValueError("The Key ID is 10 letters and numbers, shown next to the key on Apple's site.")
    if not re.match(r"^[A-Za-z0-9.-]{3,155}$", bundle_id):
        raise ValueError("The bundle ID looks like com.example.app.")
    _load_key(str(key_pem or ""))
    _write(_config_file(), {"team_id": team_id, "key_id": key_id, "bundle_id": bundle_id,
                            "key": crypto_util.encrypt(str(key_pem).encode())})
    _provider_token = None


def remove_key() -> None:
    global _provider_token
    try:
        os.remove(_config_file())
    except FileNotFoundError:
        pass
    _provider_token = None


def _config() -> dict | None:
    stored = _read(_config_file())
    if not all(stored.get(k) for k in ("team_id", "key_id", "bundle_id", "key")):
        return None
    return stored


def is_configured() -> bool:
    return _config() is not None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def provider_token(config: dict, now: float | None = None) -> str:
    """The signed token Apple wants on every request. Reused for most of
    an hour: Apple rejects both stale tokens and ones minted too often."""
    global _provider_token
    now = time.time() if now is None else now
    if (_provider_token and _provider_token[2] == config["key_id"]
            and 0 <= now - _provider_token[1] < PROVIDER_TOKEN_TTL_S):
        return _provider_token[0]
    pem = crypto_util.decrypt(config["key"])
    if not pem:
        raise ValueError("The stored Apple push key could not be read; upload it again.")
    key = _load_key(pem.decode())
    head = _b64url(json.dumps({"alg": "ES256", "kid": config["key_id"]}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iss": config["team_id"], "iat": int(now)}, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    signature = DSS.new(key, "fips-186-3", encoding="binary").sign(SHA256.new(signing_input))
    token = f"{head}.{body}.{_b64url(signature)}"
    _provider_token = (token, now, config["key_id"])
    return token


# ---- phones -----------------------------------------------------------

def devices() -> dict[str, dict]:
    return {k: v for k, v in (_read(_devices_file()).get("devices") or {}).items() if isinstance(v, dict)}


def _save_devices(found: dict[str, dict]) -> None:
    _write(_devices_file(), {"devices": found})


def register_device(install_id: str, patch: dict[str, Any], now: float | None = None) -> dict:
    """Add a phone or update its tokens. A token given as "" clears it;
    a token not mentioned is kept."""
    install_id = str(install_id or "")
    if not ID_RE.match(install_id):
        raise ValueError("install_id must be the app's install identifier.")
    found = devices()
    device = found.get(install_id) or {"environment": "production", "name": "iPhone"}
    if "environment" in patch:
        if patch["environment"] not in HOSTS:
            raise ValueError("environment must be production or development.")
        device["environment"] = patch["environment"]
    if "name" in patch:
        device["name"] = str(patch["name"] or "iPhone")[:60]
    for field in TOKEN_FIELDS:
        if field in patch:
            token = str(patch[field] or "")
            if token and not TOKEN_RE.match(token):
                raise ValueError(f"{field} is not a valid push token.")
            device[field] = token or None
    device["updated_ts"] = time.time() if now is None else now
    if patch.get("activity_gone") == "dismissed":
        # The owner swiped the lock-screen card away: do not bring it back
        # for this charging session.
        device["activity_dismissed_ts"] = device["updated_ts"]
    found[install_id] = device
    _save_devices(found)
    return device


def issue_api_token(install_id: str, now: float | None = None) -> str:
    """A long-lived token that lets this install call /api/app/* without
    the browser sign-in, so widgets keep working after that sign-in
    expires. Only its hash is stored; issuing again replaces the old one,
    and removing the phone revokes it."""
    if not ID_RE.match(str(install_id or "")):
        raise ValueError("install_id must be the app's install identifier.")
    token = secrets.token_urlsafe(32)
    found = devices()
    device = found.get(install_id) or {"environment": "production", "name": "iPhone"}
    device["api_token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    device["updated_ts"] = time.time() if now is None else now
    found[install_id] = device
    _save_devices(found)
    return token


def verify_api_token(token: str | None) -> bool:
    if not token:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    return any(hmac.compare_digest(digest, d.get("api_token_sha256") or "") for d in devices().values())


def remove_device(install_id: str) -> bool:
    found = devices()
    if install_id not in found:
        return False
    del found[install_id]
    _save_devices(found)
    return True


def _forget_token(install_id: str, field: str, token: str) -> None:
    found = devices()
    device = found.get(install_id)
    if device and device.get(field) == token:
        device[field] = None
        _save_devices(found)


def clear_activity_tokens() -> None:
    """A new Live Activity gets new tokens; the old ones are useless."""
    found = devices()
    if any(d.get("activity_token") for d in found.values()):
        for device in found.values():
            device["activity_token"] = None
        _save_devices(found)


def public_status() -> dict[str, Any]:
    """For the Settings page. Never includes the key or any token."""
    config = _config() or {}
    return {
        "configured": bool(config),
        "team_id": config.get("team_id"), "key_id": config.get("key_id"), "bundle_id": config.get("bundle_id"),
        "devices": [{"install_id": install_id, "name": d.get("name"), "environment": d.get("environment"),
                     "updated_ts": d.get("updated_ts"), "alerts": bool(d.get("push_token")),
                     "live_activity": bool(d.get("start_token")), "widgets": bool(d.get("widget_token"))}
                    for install_id, d in sorted(devices().items(), key=lambda kv: -(kv[1].get("updated_ts") or 0))],
    }


# ---- sending ----------------------------------------------------------

def alert_payload(title: str, message: str, *, priority: str, rule: str, page: str | None = None) -> dict:
    """`page` is the dashboard page the app opens when the notification is
    tapped. A low-priority notification is delivered silently: it waits in
    Notification Centre without lighting the screen or making a sound."""
    aps: dict[str, Any] = {"alert": {"title": title, "body": message}, "thread-id": rule}
    if priority == "low":
        aps["interruption-level"] = "passive"
    else:
        aps["sound"] = "default"
        aps["interruption-level"] = "time-sensitive" if priority in ("high", "urgent") else "active"
    payload: dict[str, Any] = {"aps": aps}
    if page:
        payload["page"] = page
    return payload


def live_payload(event: str, content: dict, now: float, *, device_name: str | None = None,
                 alert: tuple[str, str] | None = None) -> dict:
    """`content` must match the app's ChargingAttributes.ContentState
    field for field, or the phone silently ignores the push."""
    aps: dict[str, Any] = {"timestamp": int(now), "event": event, "content-state": content}
    if event == "start":
        aps["attributes-type"] = LIVE_ACTIVITY_TYPE
        aps["attributes"] = {"deviceName": device_name or "Jackery"}
        title, body = alert or ("Jackery", content.get("status") or "")
        aps["alert"] = {"title": title, "body": body}
    if event == "end":
        aps["dismissal-date"] = int(now + LIVE_DISMISS_AFTER_S)
    else:
        aps["stale-date"] = int(now + LIVE_STALE_AFTER_S)
    return {"aps": aps}


async def _post(client: httpx.AsyncClient, config: dict, environment: str, token: str,
                payload: dict, *, push_type: str, priority: int, collapse_id: str | None = None,
                now: float | None = None) -> tuple[bool, str | None]:
    """One push. Returns (delivered, reason). The reason is Apple's own
    word, such as BadDeviceToken, or a short description of a network failure."""
    # Live Activity and widget pushes each have a topic of their own.
    topic = config["bundle_id"] + (f".push-type.{push_type}" if push_type in ("liveactivity", "widgets") else "")
    headers = {"authorization": f"bearer {provider_token(config, now)}", "apns-topic": topic,
               "apns-push-type": push_type, "apns-priority": str(priority)}
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]
    try:
        response = await client.post(f"{HOSTS[environment]}/3/device/{token}", headers=headers, json=payload)
    except httpx.HTTPError as error:
        return False, f"Could not reach Apple: {type(error).__name__}"
    if response.status_code == 200:
        return True, None
    try:
        reason = response.json().get("reason") or f"HTTP {response.status_code}"
    except ValueError:
        reason = f"HTTP {response.status_code}"
    return False, reason


async def _send_to_all(field: str, payload: dict, *, push_type: str, priority: int,
                       collapse_id: str | None = None, only=None,
                       client: httpx.AsyncClient | None = None) -> str | None:
    """Push to every phone that has a token of this kind. Returns None if
    at least one phone accepted it, otherwise why nothing was delivered."""
    config = _config()
    if not config:
        return "Push is not set up yet: the Apple push key is missing."
    targets = [(install_id, d) for install_id, d in devices().items()
               if d.get(field) and (only is None or only(d))]
    if not targets:
        return "No phone is registered. Open the Jackery app and allow notifications."
    owned = client is None
    client = client or httpx.AsyncClient(http2=True, timeout=10)
    delivered, reasons = 0, []
    try:
        for install_id, device in targets:
            ok, reason = await _post(client, config, device.get("environment") or "production",
                                     device[field], payload, push_type=push_type,
                                     priority=priority, collapse_id=collapse_id)
            if ok:
                delivered += 1
                continue
            reasons.append(reason or "unknown")
            if reason in DEAD_TOKEN_REASONS:
                log.info("dropping %s for %s: %s", field, device.get("name"), reason)
                _forget_token(install_id, field, device[field])
    finally:
        if owned:
            await client.aclose()
    return None if delivered else "Apple refused it: " + ", ".join(sorted(set(reasons)))


async def send_alert(title: str, message: str, *, priority: str = "default", rule: str = "alert",
                     page: str | None = None, client: httpx.AsyncClient | None = None) -> str | None:
    return await _send_to_all(
        "push_token", alert_payload(title, message, priority=priority, rule=rule, page=page),
        push_type="alert", priority=5 if priority == "low" else 10, collapse_id=rule, client=client)


async def reload_widgets(*, client: httpx.AsyncClient | None = None) -> str | None:
    """Ask the phone to refresh its home-screen and lock-screen widgets
    now. iOS budgets these and may delay or skip one."""
    return await _send_to_all("widget_token", {"aps": {"content-changed": True}},
                              push_type="widgets", priority=5, client=client)


async def live_start(content: dict, *, device_name: str | None, alert: tuple[str, str],
                     now: float | None = None, only_missing: bool = False,
                     client: httpx.AsyncClient | None = None) -> str | None:
    """Start the card on every phone. `only_missing` restarts it just on
    phones that have lost theirs (an app update removes it), leaving a
    card that is still showing alone."""
    now = time.time() if now is None else now
    if not only_missing:
        clear_activity_tokens()
    return await _send_to_all(
        "start_token", live_payload("start", content, now, device_name=device_name, alert=alert),
        push_type="liveactivity", priority=10, client=client,
        only=(lambda d: not d.get("activity_token")) if only_missing else None)


async def live_update(content: dict, *, now: float | None = None,
                      client: httpx.AsyncClient | None = None) -> str | None:
    now = time.time() if now is None else now
    return await _send_to_all("activity_token", live_payload("update", content, now),
                              push_type="liveactivity", priority=5, client=client)


async def live_end(content: dict, *, now: float | None = None,
                   client: httpx.AsyncClient | None = None) -> str | None:
    now = time.time() if now is None else now
    error = await _send_to_all("activity_token", live_payload("end", content, now),
                               push_type="liveactivity", priority=10, client=client)
    clear_activity_tokens()
    return error
