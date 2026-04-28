"""
Cloud bridge — runs on the host (macOS) and exposes the Jackery cloud as a
tiny line-delimited JSON-RPC service over TCP.

Polls the Jackery cloud API on a timer. Telemetry is timestamped and stored
in process state; `poll` returns the latest snapshot. The Docker-side server
talks to this bridge instead of hitting Jackery's cloud directly so that
credentials live only on the host (in macOS Keychain).

Wire protocol (one JSON object per line, both ways):
  -> {"method": "<name>", "params": {...}}
  <- {"result": {...}}      |    {"error": "<msg>"}

Methods exposed:
  ping()                          -> {ok: true}
  status()                        -> snapshot
  connect()                       -> kick the cloud poller awake
  poll()                          -> {telemetry, source, device, cloud:{...}}
  auth_status()                   -> credential + cloud-state summary
  set_credentials(email, pw, rg)  -> validate + persist creds in keychain
  select_device(device_id)        -> switch which device the poller targets
  disconnect()                    -> tear down cloud session
  set_output(port, on)            -> NOT SUPPORTED (cloud-only build)

Cloud credentials are loaded from (in priority order):
  1. Environment variables  JACKERY_EMAIL / JACKERY_PASSWORD / JACKERY_REGION
     (used in container deployments — e.g. Synology, Linux servers)
  2. JSON file at $JACKERY_CREDS_FILE  (or /data/jackery-creds.json)
     {"email": "...", "password": "...", "region": "US"}
     This is what set_credentials writes to when there's no Keychain, so the
     web UI "sign in" flow keeps working on Synology/Linux.
  3. macOS Keychain (service "jackery-monitor", accounts "cloud-email",
     "cloud-password", "cloud-region") — the original macOS path.

Without any of those the bridge runs but the cloud poller is idle until
set_credentials is called.

Env:
  BRIDGE_HOST            (default 127.0.0.1)
  BRIDGE_PORT            (default 8766)
  CLOUD_POLL_INTERVAL_S  (default 15)
  JACKERY_EMAIL          (optional — sets cloud account email)
  JACKERY_PASSWORD       (optional — sets cloud account password)
  JACKERY_REGION         (optional — default US)
  JACKERY_CREDS_FILE     (optional — default /data/jackery-creds.json)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from typing import Optional

from Crypto.Cipher import AES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s bridge: %(message)s",
)
log = logging.getLogger("bridge")

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8766"))
CLOUD_POLL = int(os.environ.get("CLOUD_POLL_INTERVAL_S", "15"))


# ---- credential storage (multi-backend) ----
#
# Priority on read: env vars > creds file > macOS keychain.
# On write (from set_credentials): writes to whichever backend is available,
# preferring keychain on macOS, falling back to a JSON file otherwise.
# Env-var creds are read-only — we never overwrite them.

CREDS_FILE_DEFAULT = "/data/jackery-creds.json"
CREDS_KEY_DEFAULT  = "/data/.jackery-creds.key"
CREDS_ENV          = "v1"  # version tag in the encrypted blob


def _creds_file_path() -> str:
    return os.environ.get("JACKERY_CREDS_FILE", CREDS_FILE_DEFAULT)


def _creds_key_path() -> str:
    """Path of the secret key file used to encrypt the creds JSON at rest.
    Lives next to the creds file by default; never written to git/env/logs."""
    custom = os.environ.get("JACKERY_CREDS_KEY_FILE")
    if custom:
        return custom
    base = os.path.dirname(_creds_file_path()) or "."
    return os.path.join(base, ".jackery-creds.key")


def _get_or_create_creds_key() -> bytes:
    """Return a 32-byte AES-256 key from /data/.jackery-creds.key, creating it
    on first use. The key is stored mode 0600 inside the persistent docker
    volume, not in any image, env file, or git repo."""
    path = _creds_key_path()
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
        log.warning("creds key at %s has wrong length (%d); regenerating", path, len(key))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("creds key at %s unreadable (%s); regenerating", path, e)
    # Generate fresh
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(key)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    log.info("generated new at-rest credentials key at %s", path)
    return key


def _encrypt_creds(plaintext: bytes) -> dict:
    """Encrypt with AES-256-GCM. Returns a JSON-safe dict."""
    key = _get_or_create_creds_key()
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return {
        "v": CREDS_ENV,
        "alg": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
        "tag":   base64.b64encode(tag).decode(),
        "ct":    base64.b64encode(ct).decode(),
    }


def _decrypt_creds(blob: dict) -> Optional[bytes]:
    """Decrypt a dict produced by _encrypt_creds. Returns None on failure."""
    try:
        key = _get_or_create_creds_key()
        nonce = base64.b64decode(blob["nonce"])
        tag   = base64.b64decode(blob["tag"])
        ct    = base64.b64decode(blob["ct"])
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    except Exception as e:
        log.error("decrypt creds failed: %s", e)
        return None


def keychain_get(service: str, account: str) -> Optional[str]:
    """Read a password from macOS keychain. Returns None if missing or non-mac."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def keychain_set(service: str, account: str, password: str) -> bool:
    """Upsert a password in macOS keychain. Returns True on success, False on non-mac."""
    try:
        out = subprocess.run(
            ["security", "add-generic-password",
             "-U", "-s", service, "-a", account, "-w", password],
            capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _load_creds_file() -> Optional[dict]:
    """Read the on-disk credentials file. Supports both the new encrypted
    format ({v,alg,nonce,tag,ct}) and the legacy plaintext format
    ({email,password,region}); the latter is auto-migrated to encrypted on
    next save."""
    path = _creds_file_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("creds file %s unreadable: %s", path, e)
        return None

    if not isinstance(data, dict):
        return None

    # Encrypted (current) format
    if "ct" in data and "nonce" in data:
        pt = _decrypt_creds(data)
        if pt is None:
            return None
        try:
            inner = json.loads(pt.decode())
        except Exception as e:
            log.error("creds payload not valid JSON after decrypt: %s", e)
            return None
        if inner.get("email") and inner.get("password"):
            return {
                "email": str(inner["email"]),
                "password": str(inner["password"]),
                "region": str(inner.get("region") or "US").upper(),
            }
        return None

    # Legacy plaintext format
    if data.get("email") and data.get("password"):
        log.warning("loaded legacy plaintext creds file at %s; will re-encrypt on next save", path)
        return {
            "email": str(data["email"]),
            "password": str(data["password"]),
            "region": str(data.get("region") or "US").upper(),
        }
    return None


def _save_creds_file(email: str, password: str, region: str) -> bool:
    """Encrypt with AES-256-GCM and write to disk atomically."""
    path = _creds_file_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = json.dumps(
            {"email": email, "password": password, "region": region}
        ).encode()
        blob = _encrypt_creds(payload)
        # Write to a temp file first, then rename, so a crash mid-write
        # can't leave a half-written creds file behind.
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(blob, f)
        try:
            os.chmod(tmp, 0o600)  # rw for owner only
        except Exception:
            pass
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error("failed to write creds file %s: %s", path, e)
        return False


def _delete_creds_file() -> bool:
    """Remove the on-disk creds. Returns True if file is gone after the call."""
    path = _creds_file_path()
    try:
        os.remove(path)
        log.info("removed creds file at %s", path)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        log.error("failed to remove creds file %s: %s", path, e)
        return False


def load_cloud_credentials() -> Optional[dict]:
    """Return {email, password, region} from env / file / keychain, or None."""
    # 1. Environment variables (Synology / generic Linux deployment)
    env_email = os.environ.get("JACKERY_EMAIL")
    env_pw = os.environ.get("JACKERY_PASSWORD")
    if env_email and env_pw:
        log.info("Loaded cloud credentials from environment variables")
        return {
            "email": env_email.strip(),
            "password": env_pw,
            "region": (os.environ.get("JACKERY_REGION") or "US").strip().upper(),
        }

    # 2. JSON creds file (set_credentials persists here on non-mac hosts)
    file_creds = _load_creds_file()
    if file_creds:
        log.info("Loaded cloud credentials from %s", _creds_file_path())
        return file_creds

    # 3. macOS Keychain (original behaviour)
    email = keychain_get("jackery-monitor", "cloud-email")
    password = keychain_get("jackery-monitor", "cloud-password")
    region = keychain_get("jackery-monitor", "cloud-region") or "US"
    if email and password:
        log.info("Loaded cloud credentials from macOS keychain")
        return {"email": email, "password": password, "region": region}

    log.info("No cloud credentials found (env / file / keychain all empty) — "
             "cloud poller idle. Sign in via the web UI or set JACKERY_EMAIL / "
             "JACKERY_PASSWORD env vars.")
    return None


def clear_cloud_credentials() -> tuple[bool, str]:
    """Wipe persisted credentials. Refuses if env vars are pinning them.
    Removes keychain entries on macOS and the encrypted JSON on Linux/NAS."""
    if os.environ.get("JACKERY_EMAIL") and os.environ.get("JACKERY_PASSWORD"):
        return False, ("credentials are pinned via JACKERY_EMAIL/JACKERY_PASSWORD "
                       "environment variables \u2014 unset them or edit your .env to clear")
    cleared = []
    # macOS keychain best-effort delete (no-op on Linux)
    for acct in ("cloud-email", "cloud-password", "cloud-region"):
        try:
            r = subprocess.run(
                ["security", "delete-generic-password", "-s", "jackery-monitor", "-a", acct],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                cleared.append(f"keychain:{acct}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # JSON file (Linux / Synology / Docker)
    if _delete_creds_file():
        cleared.append(_creds_file_path())
    return True, ", ".join(cleared) or "nothing to clear"


def save_cloud_credentials(email: str, password: str, region: str) -> tuple[bool, str]:
    """Persist creds. Returns (ok, where) describing where they were stored.
       Won't try to overwrite env-var-supplied creds (those are managed by the
       operator, not the web UI)."""
    if os.environ.get("JACKERY_EMAIL") and os.environ.get("JACKERY_PASSWORD"):
        return False, ("credentials are pinned via JACKERY_EMAIL/JACKERY_PASSWORD "
                       "environment variables — unset them or edit your .env to change")
    # Try macOS keychain first (preserves original behaviour on Mac)
    if keychain_set("jackery-monitor", "cloud-email", email) \
       and keychain_set("jackery-monitor", "cloud-password", password) \
       and keychain_set("jackery-monitor", "cloud-region", region):
        return True, "macOS keychain"
    # Fall back to JSON file (Synology, Linux, Docker)
    if _save_creds_file(email, password, region):
        return True, _creds_file_path()
    return False, "no writable credential store available"


# ---- shared state ----
class State:
    def __init__(self) -> None:
        self.cloud_creds: Optional[dict] = None
        self.cloud_state: str = "needs-credentials"  # needs-credentials | logging-in | connected | error
        self.cloud_device: Optional[dict] = None
        self.cloud_telemetry: Optional[dict] = None
        self.cloud_ts: Optional[float] = None
        self.cloud_error: Optional[str] = None
        self.cloud_client = None               # JackeryCloudClient | None
        self.cloud_device_id: Optional[str] = None
        # full list of devices on the account (for the UI dropdown)
        self.cloud_devices: list[dict] = []
        # set this to force a re-poll on the next loop iteration
        self.cloud_force_repoll: asyncio.Event = asyncio.Event()
        # background task handle for cloud_loop so we can cancel/restart it
        self.cloud_task: Optional[asyncio.Task] = None

state = State()


# ---- Cloud poller ----
async def cloud_loop() -> None:
    if not state.cloud_creds:
        return
    # lazy import so users without httpx/pycryptodome aren't blocked at boot
    try:
        from cloud_client import JackeryCloudClient, cloud_props_to_telemetry
    except Exception as e:
        log.warning("Cloud client unavailable: %s", e)
        state.cloud_state = "error"
        state.cloud_error = f"import: {e}"
        return

    c = JackeryCloudClient(
        email=state.cloud_creds["email"],
        password=state.cloud_creds["password"],
        region=state.cloud_creds.get("region", "US"),
    )
    state.cloud_client = c
    backoff = 10
    while True:
        try:
            if not c.token:
                state.cloud_state = "logging-in"
                await c.login()
            # Refresh device list on first iteration AND whenever no device
            # is selected. Keeps the dropdown current.
            if not state.cloud_devices or not state.cloud_device_id:
                devs = await c.fetch_devices()
                if not devs:
                    raise RuntimeError("no devices on this Jackery account")
                state.cloud_devices = [
                    {
                        "device_id": d.device_id,
                        "name": d.name,
                        "model_code": d.model_code,
                        "model_name": d.model_name,
                        "device_sn": d.device_sn,
                    }
                    for d in devs
                ]
                if not state.cloud_device_id:
                    # Prefer Explorer 5000 Plus model codes 13 / 22 if multiple
                    preferred = next((d for d in devs if d.model_code in (13, 22)), devs[0])
                    state.cloud_device_id = preferred.device_id
                sel = next((d for d in devs if d.device_id == state.cloud_device_id), devs[0])
                state.cloud_device = {
                    "name": sel.name,
                    "address": "cloud",
                    "rssi": 0,
                    "model_code": sel.model_code,
                    "device_sn": sel.device_sn,
                    "device_type": "portable" if sel.model_code != 22 else "box",
                }
                log.info("Cloud device active: %s (model %s); %d total on account",
                         sel.name, sel.model_code, len(devs))

            props = await c.fetch_properties(state.cloud_device_id)
            if props:
                state.cloud_telemetry = cloud_props_to_telemetry(props)
                state.cloud_ts = time.time()
                state.cloud_state = "connected"
                state.cloud_error = None
            backoff = 10
        except Exception as e:
            state.cloud_state = "error"
            state.cloud_error = str(e)
            log.warning("Cloud poll error: %s", e)
            if state.cloud_client:
                state.cloud_client.token = None
            await asyncio.sleep(min(backoff, 300))
            backoff = min(backoff * 2, 300)
            continue
        # Sleep until next poll OR a device-switch nudges us awake
        try:
            await asyncio.wait_for(state.cloud_force_repoll.wait(), timeout=CLOUD_POLL)
            state.cloud_force_repoll.clear()
        except asyncio.TimeoutError:
            pass


# ---- merged poll output ----
def merged_poll() -> dict:
    """Return the current cloud telemetry snapshot."""
    now = time.time()
    cloud_age = (now - state.cloud_ts) if state.cloud_ts else None

    src = "cloud" if state.cloud_telemetry is not None else None
    tele = state.cloud_telemetry
    device = state.cloud_device

    return {
        "telemetry": tele,
        "source": src,
        "device": device,
        "cloud": {
            "state": state.cloud_state,
            "ts": state.cloud_ts,
            "age_s": round(cloud_age, 1) if cloud_age is not None else None,
            "error": state.cloud_error,
            "device": state.cloud_device,
            "devices": list(state.cloud_devices),
            "selected_device_id": state.cloud_device_id,
        },
    }


# ---- RPC handlers ----
async def handle(method: str, params: dict) -> dict:
    if method == "ping":
        return {"ok": True}

    if method == "connect":
        # Nudge the cloud poller awake.
        state.cloud_force_repoll.set()
        return {"ok": True}

    if method == "status":
        return merged_poll()

    if method == "poll":
        return merged_poll()

    if method == "auth_status":
        return {
            "has_credentials": bool(state.cloud_creds),
            "email": (state.cloud_creds or {}).get("email"),
            "region": (state.cloud_creds or {}).get("region", "US"),
            "cloud_state": state.cloud_state,
            "cloud_error": state.cloud_error,
        }

    if method == "set_credentials":
        email = (params.get("email") or "").strip()
        password = params.get("password") or ""
        region = (params.get("region") or "US").strip().upper() or "US"
        if not email or not password:
            return {"ok": False, "error": "email and password are required"}

        # 1) verify against the cloud BEFORE saving — avoids storing bad creds
        try:
            from cloud_client import JackeryCloudClient
        except Exception as e:
            return {"ok": False, "error": f"cloud client unavailable: {e}"}

        probe = JackeryCloudClient(email=email, password=password, region=region)
        try:
            await probe.login()
        except Exception as e:
            try:
                await probe.aclose()
            except Exception:
                pass
            return {"ok": False, "error": f"login failed: {e}"}
        try:
            await probe.aclose()
        except Exception:
            pass

        # 2) persist (keychain on macOS, JSON file otherwise)
        ok, where = save_cloud_credentials(email, password, region)
        if not ok:
            return {"ok": False, "error": f"failed to persist credentials: {where}"}
        log.info("persisted cloud credentials to %s", where)

        # 3) (re)start cloud_loop with fresh credentials
        state.cloud_creds = {"email": email, "password": password, "region": region}
        state.cloud_state = "logging-in"
        state.cloud_error = None
        state.cloud_device = None
        state.cloud_devices = []
        state.cloud_device_id = None
        state.cloud_telemetry = None
        state.cloud_ts = None
        if state.cloud_client:
            try:
                await state.cloud_client.aclose()
            except Exception:
                pass
            state.cloud_client = None
        if state.cloud_task and not state.cloud_task.done():
            state.cloud_task.cancel()
            try:
                await state.cloud_task
            except Exception:
                pass
        state.cloud_task = asyncio.create_task(cloud_loop(), name="cloud_loop")
        return {"ok": True, "email": email, "region": region}

    if method == "select_device":
        device_id = str(params.get("device_id") or "").strip()
        if not device_id:
            return {"ok": False, "error": "device_id required"}
        match = next((d for d in state.cloud_devices if d["device_id"] == device_id), None)
        if not match:
            return {"ok": False, "error": f"unknown device_id: {device_id}"}
        if device_id != state.cloud_device_id:
            log.info("Switching cloud device -> %s (%s)", match["name"], device_id)
            state.cloud_device_id = device_id
            state.cloud_device = {
                "name": match["name"],
                "address": "cloud",
                "rssi": 0,
                "model_code": match["model_code"],
                "device_sn": match["device_sn"],
                "device_type": "portable" if match["model_code"] != 22 else "box",
            }
            # Drop stale telemetry so the UI doesn't briefly show old data
            state.cloud_telemetry = None
            state.cloud_ts = None
            state.cloud_force_repoll.set()
        return {"ok": True, "device_id": device_id, "name": match["name"]}

    if method == "clear_credentials":
        ok, where = clear_cloud_credentials()
        if not ok:
            return {"ok": False, "error": where}
        log.info("cleared persisted credentials (%s)", where)
        # Stop the cloud loop and reset session state.
        if state.cloud_client:
            try:
                await state.cloud_client.aclose()
            except Exception:
                pass
            state.cloud_client = None
        if state.cloud_task and not state.cloud_task.done():
            state.cloud_task.cancel()
            try:
                await state.cloud_task
            except Exception:
                pass
            state.cloud_task = None
        state.cloud_creds = None
        state.cloud_state = "needs-credentials"
        state.cloud_error = None
        state.cloud_device = None
        state.cloud_devices = []
        state.cloud_device_id = None
        state.cloud_telemetry = None
        state.cloud_ts = None
        return {"ok": True, "cleared": where}

    if method == "set_output":
        # Cloud API does not expose port toggles; this build is read-only.
        return {"ok": False, "error": "switch toggles are not supported in cloud-only mode"}

    if method == "disconnect":
        if state.cloud_client:
            try:
                await state.cloud_client.aclose()
            except Exception:
                pass
            state.cloud_client = None
        if state.cloud_task and not state.cloud_task.done():
            state.cloud_task.cancel()
        state.cloud_state = "needs-credentials" if not state.cloud_creds else "logging-in"
        return {"ok": True}

    raise ValueError(f"unknown method: {method!r}")


async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    log.info("Client connected: %s", peer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode())
                m = req.get("method", "")
                p = req.get("params") or {}
                # Don't log credentials/payloads; just method.
                log.info("rpc: %s", m)
                result = await handle(m, p)
                resp = {"result": result}
            except Exception as e:
                log.exception("rpc error")
                resp = {"error": str(e)}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        log.info("Client disconnected: %s", peer)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    state.cloud_creds = load_cloud_credentials()
    if state.cloud_creds:
        state.cloud_state = "logging-in"
        log.info("Cloud poller enabled (region=%s, account=%s)",
                 state.cloud_creds["region"], state.cloud_creds["email"])
    else:
        state.cloud_state = "needs-credentials"

    server = await asyncio.start_server(serve, HOST, PORT)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    log.info("Jackery bridge listening on %s", sockets)

    bg = []
    if state.cloud_creds:
        state.cloud_task = asyncio.create_task(cloud_loop(), name="cloud_loop")
        bg.append(state.cloud_task)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with server:
        try:
            await stop.wait()
        finally:
            log.info("Shutting down bridge...")
            for t in bg:
                t.cancel()
            if state.cloud_client:
                try:
                    await state.cloud_client.aclose()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
