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

from Crypto.Cipher import AES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s bridge: %(message)s",
)
log = logging.getLogger("bridge")

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8766"))
# CLOUD_POLL and SESSION_CONTESTED_COOLDOWN_S are now user-tunable through
# /data/settings.json; the env vars are still consulted as fallback defaults
# inside the settings module. We read them per-loop-iteration so a settings
# change applies on the next cycle without a bridge restart.
import kasa_client  # noqa: E402  for the inverter-protect fast-trip from MQTT push
import settings as user_settings  # noqa: E402  -- after env reads above
import solar_charge  # noqa: E402  shared config + overload-state file (writer-side)
from device_client import (  # noqa: E402  shares the model_code -> "portable"/"box" heuristic with the server
    device_type_for,
)

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


def _decrypt_creds(blob: dict) -> bytes | None:
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


def keychain_get(service: str, account: str) -> str | None:
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


def _load_creds_file() -> dict | None:
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
        email = str(data["email"])
        password = str(data["password"])
        region = str(data.get("region") or "US").upper()
        # Immediately re-encrypt in place so the plaintext doesn't linger.
        # The previous behavior only re-encrypted "on next save", which
        # could be never if the user didn't change credentials.
        if _save_creds_file(email, password, region):
            log.info("migrated legacy plaintext creds at %s to encrypted form", path)
        else:
            log.warning("legacy plaintext creds at %s could not be re-encrypted "
                        "(continuing to use plaintext for this session)", path)
        return {"email": email, "password": password, "region": region}
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


def load_cloud_credentials() -> dict | None:
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


# ---- event ring buffer ----
# Compact in-memory log of notable events surfaced to the dashboard's Logs
# tab. We deliberately don't capture every poll line (would be noisy) — only
# state transitions, errors, MQTT publishes, and other "what just happened"
# moments. Capped to EVENTS_MAX entries; oldest evicted on overflow.
EVENTS_MAX = 200
_events: list[dict] = []


def event(level: str, category: str, message: str, **extra) -> None:
    """Push one event to the ring buffer. Also routes to the regular logger
       at the matching level so SSH/Container Manager logs still see it."""
    e = {
        "ts": time.time(),
        "level": level,            # "info" | "warn" | "error"
        "category": category,      # "auth" | "poll" | "mqtt" | "session" | "settings" | "device"
        "message": message,
    }
    if extra:
        e["extra"] = extra
    _events.append(e)
    if len(_events) > EVENTS_MAX:
        del _events[: len(_events) - EVENTS_MAX]
    # Mirror to standard logging so the host-side container log keeps capturing.
    if level == "error":
        log.error("%s: %s%s", category, message, f" {extra}" if extra else "")
    elif level == "warn":
        log.warning("%s: %s%s", category, message, f" {extra}" if extra else "")
    else:
        log.info("%s: %s%s", category, message, f" {extra}" if extra else "")


def get_events(limit: int = 100, since: float = 0.0) -> list[dict]:
    """Return the most recent events, optionally filtered to ts > since."""
    out = _events
    if since:
        out = [e for e in out if e["ts"] > since]
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


# ---- Inverter overload protection (push-driven fast-trip) ----
# When MQTT pushes a fresh output_power_w that meets/exceeds the
# per-device threshold, fire the diversion plug OFF immediately and
# stamp the overload-state file. The server's eval loop reads that
# file and refuses to re-engage for `inverter_protect_cooldown_s`.
#
# Reaction time is bounded by the MQTT-push handler invocation
# (~hundreds of milliseconds round-trip from device → broker → us)
# plus the Kasa LAN call (~50–200ms), so end-to-end ≈ 0.3–1s.
#
# The Kasa OFF command is debounced to once per 10s per device — the
# overload-state file is still stamped on EVERY tick above threshold
# (so the server's cooldown timer keeps re-arming while the overload
# persists, not just resetting from the first sample). Stamping is a
# cheap file write; the LAN call to Kasa is the part worth throttling.
#
# Config is cached for INVERTER_PROTECT_CFG_TTL_S because this runs on
# every MQTT push — re-reading + re-validating /data/solar_charge.json
# at that rate burns lock contention with set_config writes. Stale
# threshold for a few seconds after the user changes it is fine; the
# protection trip can wait one TTL.
INVERTER_PROTECT_CFG_TTL_S = 5.0
_last_inverter_trip_fired_at: dict[str, float] = {}
_inverter_protect_cfg_cache: dict[str, tuple[float, dict]] = {}


def _inverter_protect_cfg(device_sn: str) -> dict | None:
    """Cached config read for the fast-trip path. Returns None on any
    error or if the device has no configured row yet."""
    now = time.time()
    cached = _inverter_protect_cfg_cache.get(device_sn)
    if cached and (now - cached[0]) < INVERTER_PROTECT_CFG_TTL_S:
        return cached[1]
    try:
        cfg = solar_charge.get_config(device_sn)
    except Exception:
        return None
    _inverter_protect_cfg_cache[device_sn] = (now, cfg)
    return cfg


async def _inverter_protect_check(device_sn: str, load_w: float | None) -> None:
    """Fast-trip the diversion plug if output_power_w >= threshold.

    Best-effort: any config-read, file-write, or Kasa failure logs an
    event and returns. We do NOT propagate errors back to the MQTT push
    handler — that path must stay responsive even if the inverter
    protection layer has trouble talking to its dependencies."""
    if not device_sn or load_w is None:
        return
    cfg = _inverter_protect_cfg(device_sn)
    if not cfg or cfg.get("mode") == "off":
        return
    host = cfg.get("kasa_device_host")
    if not host:
        return
    try:
        threshold = float(cfg.get("inverter_protect_load_w") or 2100)
    except (TypeError, ValueError):
        return
    if float(load_w) < threshold:
        return
    now = time.time()
    # Stamp ALWAYS — the server's cooldown starts from the LAST overload
    # sample, so as long as load stays high we keep extending the window.
    try:
        solar_charge.stamp_overload(device_sn, float(load_w), now_ts=now)
    except Exception as e:
        event("warn", "inverter_protect",
              f"failed to stamp overload state: {e}",
              device_sn=device_sn)
    # Debounce the Kasa OFF call to once per 10s per device. The plug
    # is already OFF after the first call; repeating it every push for
    # 30 min of high load is wasted LAN traffic.
    last_fired = _last_inverter_trip_fired_at.get(device_sn, 0.0)
    if now - last_fired < 10.0:
        return
    _last_inverter_trip_fired_at[device_sn] = now
    event("warn", "inverter_protect",
          f"INVERTER OVERLOAD: output {load_w:.0f}W ≥ {threshold:.0f}W "
          f"threshold — forcing diversion plug OFF (Kasa {host})",
          device_sn=device_sn, load_w=float(load_w), threshold=threshold)
    try:
        await kasa_client.set_state(host, False)
    except Exception as e:
        event("error", "inverter_protect",
              f"failed to fire Kasa OFF on overload: {e}",
              device_sn=device_sn, host=host)


# ---- shared state ----
class State:
    def __init__(self) -> None:
        self.cloud_creds: dict | None = None
        self.cloud_state: str = "needs-credentials"  # needs-credentials | logging-in | connected | error
        self.cloud_device: dict | None = None
        # ---- per-device telemetry (keyed by device_sn) ----
        # We poll EVERY Jackery device on the account so automation rules can
        # target a specific device, not just whichever the dashboard happens
        # to be viewing. The "active" device (cloud_device_id) is what the
        # live tab renders; the others poll quietly in the background.
        # cloud_props_raw / cloud_telemetry / cloud_ts below are convenience
        # mirrors of the active device for existing callers.
        self.props_raw_by_sn: dict[str, dict] = {}
        self.telemetry_by_sn: dict[str, dict] = {}
        self.ts_by_sn: dict[str, float] = {}
        # Per-expansion-battery state, keyed by parent (host) device SN.
        # Updated in real-time from MQTT SubDevicePropertyChange pushes.
        self.battery_packs_by_sn: dict[str, list[dict]] = {}
        self.packs_ts_by_sn: dict[str, float] = {}
        self.cloud_telemetry: dict | None = None
        self.cloud_props_raw: dict = {}
        self.cloud_ts: float | None = None
        self.cloud_error: str | None = None
        self.cloud_client = None               # JackeryCloudClient | None
        self.cloud_device_id: str | None = None
        # full list of devices on the account (for the UI dropdown)
        self.cloud_devices: list[dict] = []
        # set this to force a re-poll on the next loop iteration
        self.cloud_force_repoll: asyncio.Event = asyncio.Event()
        # background task handle for cloud_loop so we can cancel/restart it
        self.cloud_task: asyncio.Task | None = None
        # User-initiated polling pause (epoch seconds, 0 = not paused).
        # Set via pause_polling RPC; the cloud poller skips iterations until
        # `time.time() >= pause_until`.
        self.pause_until: float = 0.0
        # Auto-cooldown after a contested-session error. Same shape as above
        # but populated by the cloud_loop itself when it catches a 401-style
        # response from the cloud.
        self.contested_until: float = 0.0
        # Consecutive SessionContestedError counter. Exponential-backed off
        # so a persistent contender (e.g. the user's phone app left
        # running) doesn't generate a one-per-minute warning forever. Reset
        # to 0 on the first successful poll. Used by the cloud_loop to
        # compute the next cooldown, and by the event log to fire an
        # actionable alert once it's clear the contention isn't transient.
        self.contested_consecutive: int = 0
        # Set True once we've emitted the "persistent contention" error so
        # we don't spam the alert every cycle. Cleared when a poll succeeds.
        self.contested_alerted: bool = False

state = State()


# ---- Cloud poller ----
async def cloud_loop() -> None:
    if not state.cloud_creds:
        return
    # lazy import so users without httpx/pycryptodome aren't blocked at boot
    try:
        from cloud_client import (
            JackeryCloudClient,
            SessionContestedError,
            cloud_props_to_telemetry,
        )
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

    # Realtime push handler — called from the asyncio loop whenever MQTT
    # pushes a property delta. Blends into cloud_props_raw + cloud_telemetry
    # so the UI gets ~500ms-fresh updates without our HTTP polling speeding up.
    # Keys that ride along on every MQTT push but aren't device properties —
    # broker envelope / metadata. Filtered out before merging into the
    # property dict and before logging so the Logs tab isn't noisy.
    _IGNORE_PUSH_KEYS = {"messageId", "msgId", "id"}
    # Cumulative set of every property key we've ever seen pushed by MQTT,
    # used for one-off discovery logging when a brand-new key appears (e.g.
    # if a device firmware update starts pushing per-PV solar fields).
    _seen_push_keys: set[str] = set()

    async def _on_property_push(body: dict, device_sn: str | None = None):
        if not isinstance(body, dict) or not body:
            return
        if not device_sn:
            return  # we route by device_sn now; can't apply a push without it
        props = {k: v for k, v in body.items() if k not in _IGNORE_PUSH_KEYS}
        if not props:
            return  # broker ack only, no actual property updates
        # Discovery log: emit an event the first time we see any key — handy
        # for spotting per-input solar fields (hpv/lpv/pv1/...) if/when the
        # device starts pushing them.
        new_keys = sorted(set(props.keys()) - _seen_push_keys)
        if new_keys:
            _seen_push_keys.update(new_keys)
            event("info", "mqtt", f"New MQTT key(s) discovered: {', '.join(new_keys)}",
                  new_keys=new_keys, total_seen=len(_seen_push_keys))
        # Update the per-device dicts. Active-device convenience mirrors get
        # written by the cloud_loop step that runs after this returns.
        raw = state.props_raw_by_sn.setdefault(device_sn, {})
        raw.update(props)
        state.telemetry_by_sn[device_sn] = cloud_props_to_telemetry(raw)
        state.ts_by_sn[device_sn] = time.time()
        # Inverter overload protection: as soon as a fresh output_power_w
        # comes through MQTT, check whether it crossed the per-device
        # threshold. If so, fire the diversion plug OFF immediately and
        # stamp the shared overload-state file so the server's eval loop
        # holds it off through the cooldown window. Best-effort — never
        # let this block or fail the rest of the push handler.
        try:
            telemetry = state.telemetry_by_sn[device_sn]
            await _inverter_protect_check(
                device_sn, telemetry.get("output_power_w"))
        except Exception as e:
            log.warning("inverter_protect check raised (ignored): %s", e)
        # Mirror onto the active-device fields if this push is for the
        # device the dashboard is viewing.
        active_sn = (state.cloud_device or {}).get("device_sn")
        if device_sn == active_sn:
            state.cloud_props_raw = raw
            state.cloud_telemetry = state.telemetry_by_sn[device_sn]
            state.cloud_ts = state.ts_by_sn[device_sn]
        if state.cloud_state not in ("paused", "contested"):
            state.cloud_state = "connected"
        event("info", "mqtt", f"Realtime update ({len(props)} keys) [{device_sn[-6:]}]",
              keys=sorted(props.keys())[:8], device_sn=device_sn)

    async def _on_pack_push(packs: list, parent_sn: str):
        """MQTT SubDevicePropertyChange handler. The cloud pushes the
        same shape as /v1/device/battery/pack/list in real time, so we
        can stop polling once the broker delivers an update. Updates
        both the bridge's per-device cache and the cloud_client's
        push cache so any subsequent fetch_battery_packs short-circuits
        to the realtime value."""
        if not parent_sn or not isinstance(packs, list):
            return
        cleaned = [p for p in packs
                   if isinstance(p, dict) and not p.get("isDelete")]
        cleaned.sort(key=lambda p: p.get("deviceOrder") or 0)
        state.battery_packs_by_sn[parent_sn] = cleaned
        state.packs_ts_by_sn[parent_sn] = time.time()
        if state.cloud_client:
            state.cloud_client.pack_cache_by_sn[parent_sn] = cleaned
        event("info", "mqtt",
              f"Realtime pack update ({len(cleaned)} packs) [{parent_sn[-6:]}]",
              pack_count=len(cleaned), parent_sn=parent_sn)

    realtime_subscribed = False
    while True:
        # Honor user-initiated pause and contested-session cooldown.
        # We tick every few seconds rather than sleeping the full window so a
        # resume_polling RPC takes effect quickly.
        now = time.time()
        wait_until = max(state.pause_until, state.contested_until)
        if wait_until > now:
            if state.pause_until > now:
                state.cloud_state = "paused"
            elif state.contested_until > now:
                state.cloud_state = "contested"
            await asyncio.sleep(min(5.0, wait_until - now))
            continue

        try:
            if not c.token:
                state.cloud_state = "logging-in"
                await c.login()
                event("info", "auth", "Cloud login OK", user_id=c.user_id)
                # Login dropped the MQTT client (fresh creds incoming) — force
                # the realtime subscribe block below to run again so we don't
                # lose pushes after a contested-cooldown re-login.
                realtime_subscribed = False
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
                    "device_type": device_type_for(sel.model_code),
                }
                log.info("Cloud device active: %s (model %s); %d total on account",
                         sel.name, sel.model_code, len(devs))

            # Poll EVERY device on the account. Active device first (the one
            # the dashboard is currently viewing) so it gets fresh data with
            # minimum latency; the others follow in the same iteration.
            now_ts = time.time()
            active_sn = (state.cloud_device or {}).get("device_sn")
            ordered_devs = sorted(
                state.cloud_devices,
                key=lambda d: 0 if d.get("device_sn") == active_sn else 1,
            )
            any_polled = False
            for dev in ordered_devs:
                dev_id = dev.get("device_id")
                dev_sn = dev.get("device_sn")
                if not dev_id or not dev_sn:
                    continue
                try:
                    props = await c.fetch_properties(dev_id)
                except SessionContestedError:
                    raise  # let outer handler apply the cooldown
                except Exception as e:
                    log.warning("fetch_properties(%s) failed: %s", dev_sn, e)
                    continue
                if not props:
                    continue
                raw = state.props_raw_by_sn.setdefault(dev_sn, {})
                raw.update(props)
                state.telemetry_by_sn[dev_sn] = cloud_props_to_telemetry(raw)
                state.ts_by_sn[dev_sn] = now_ts
                any_polled = True
            # Mirror the active device onto the legacy single-device fields
            # so existing merged_poll consumers see no change in shape.
            if active_sn and active_sn in state.telemetry_by_sn:
                state.cloud_props_raw = state.props_raw_by_sn[active_sn]
                state.cloud_telemetry = state.telemetry_by_sn[active_sn]
                state.cloud_ts = state.ts_by_sn[active_sn]
            if any_polled:
                state.cloud_state = "connected"
                state.cloud_error = None
                # Reset contention tracking on the first successful poll.
                # If we'd alerted about persistent contention, clear the
                # latch so a future contention episode produces a fresh
                # alert instead of being suppressed.
                if state.contested_consecutive > 0 or state.contested_alerted:
                    state.contested_consecutive = 0
                    state.contested_alerted = False
                    event("info", "session",
                          "Cloud session reclaimed; contested counter reset")
            # Subscribe to MQTT pushes once per cloud_loop lifetime, after
            # the first successful HTTP poll (so user_id is set + device
            # selected). paho-mqtt handles reconnect re-subscription via
            # on_connect.
            if not realtime_subscribed:
                try:
                    await c.subscribe_realtime(_on_property_push,
                                               on_pack_change=_on_pack_push)
                    realtime_subscribed = True
                    event("info", "mqtt", "Subscribed to realtime device topic")
                except Exception as e:
                    event("warn", "mqtt", f"Realtime subscribe failed: {e}")
            backoff = 10
        except SessionContestedError as e:
            # The phone app (or another client) just logged in and bumped us.
            # Don't fight back — cool down and let them keep the session.
            # Exponential backoff on consecutive episodes: a persistent
            # contender (e.g. phone app left running indefinitely) used to
            # generate a one-per-minute warning forever; now the cooldown
            # doubles each cycle up to a 1h cap so the log doesn't drown.
            state.contested_consecutive += 1
            base_cooldown = user_settings.get("session_contested_cooldown_s")
            # 2^0=1× for the first, 2× for the second, 4× for the third...
            # capped at 1h so a stuck contender doesn't push the next retry
            # past usefulness.
            mult = min(2 ** (state.contested_consecutive - 1), 60)
            cooldown = min(base_cooldown * mult, 3600)
            state.contested_until = time.time() + cooldown
            state.cloud_state = "contested"
            state.cloud_error = str(e)
            if state.cloud_client:
                state.cloud_client.token = None
            event("warn", "session",
                  f"Session contested by another client; cooling down {cooldown}s "
                  f"(consecutive={state.contested_consecutive})",
                  cooldown_s=cooldown,
                  consecutive=state.contested_consecutive)
            # After 3 consecutive contentions WITHOUT a successful poll
            # between them, the contender clearly isn't going away on its
            # own — emit an error-level event that downstream alerting
            # will surface to the user with an actionable message. One-
            # shot via the contested_alerted latch so the alert doesn't
            # spam every cycle thereafter.
            if state.contested_consecutive >= 3 and not state.contested_alerted:
                state.contested_alerted = True
                event(
                    "error", "session",
                    "Persistent cloud session contention. Another client is "
                    "actively contesting this account's Jackery session — "
                    "most likely the Jackery phone app is open (foreground "
                    "or background-refresh), or a second instance of this "
                    "bridge is running. Telemetry will stay stale until "
                    "you sign the contender out. The bridge will keep "
                    "retrying with backoff (up to 1h between attempts).",
                    consecutive=state.contested_consecutive,
                )
            continue
        except Exception as e:
            state.cloud_state = "error"
            state.cloud_error = str(e)
            if state.cloud_client:
                state.cloud_client.token = None
            event("error", "poll", f"Cloud poll error: {e}", backoff_s=min(backoff, 300))
            await asyncio.sleep(min(backoff, 300))
            backoff = min(backoff * 2, 300)
            continue
        # Sleep until next poll OR a device-switch nudges us awake
        sleep_started = time.time()
        sleep_target = user_settings.get("cloud_poll_interval_s")
        try:
            await asyncio.wait_for(state.cloud_force_repoll.wait(),
                                   timeout=sleep_target)
            state.cloud_force_repoll.clear()
            slept = time.time() - sleep_started
            log.info(
                "cloud_loop sleep interrupted by force_repoll after %.1fs "
                "(target=%ds) — investigating who set it",
                slept, sleep_target,
            )
        except TimeoutError:
            pass


# Watchdog thresholds. The cloud_loop is supposed to update `cloud_ts` on
# every successful poll (typically every 30-60s). If we go more than
# WATCHDOG_STALE_S without a successful update — AND the user hasn't
# explicitly paused or hit a contested cooldown — something has stalled
# (almost always a hung HTTP await inside c.login() or c.fetch_*). The
# watchdog cancels and recreates the cloud_loop task so a fresh attempt
# starts. Restart is throttled to WATCHDOG_MIN_RESTART_INTERVAL_S to
# avoid a thrash loop if the upstream is genuinely broken.
WATCHDOG_CHECK_INTERVAL_S = 120
WATCHDOG_STALE_S = 600                # 10 min stale → restart
WATCHDOG_MIN_RESTART_INTERVAL_S = 300  # don't restart more than 1x/5min


async def cloud_watchdog_loop() -> None:
    """Monitor cloud_loop liveness; cancel + recreate the task when the
    upstream HTTP layer hangs.

    Background: 2026-05-12 the cloud_loop was observed stuck in
    "logging-in" for 5 hours with no error logged and no telemetry
    updates. The task was alive but blocked inside `await c.login()`
    despite httpx's 15s timeout — likely a half-open TCP / TLS stall
    that httpx didn't surface as a TimeoutError. force_repoll alone
    can't unblock a hung HTTP await; only cancelling the task can.

    Skips when the user has explicitly paused or a contested cooldown
    is active, since those are intentional stalls."""
    last_restart_at: float = 0.0
    while True:
        try:
            await asyncio.sleep(WATCHDOG_CHECK_INTERVAL_S)
            now = time.time()
            # Skip during intentional pauses.
            if (state.pause_until or 0) > now:
                continue
            if (state.contested_until or 0) > now:
                continue
            # No telemetry yet AND no credentials → nothing to watchdog.
            if not state.cloud_creds:
                continue
            cloud_ts = state.cloud_ts or 0
            age = now - cloud_ts if cloud_ts else float("inf")
            if age < WATCHDOG_STALE_S:
                continue
            # Throttle restarts to avoid a thrash loop when upstream is
            # genuinely down.
            since_last_restart = now - last_restart_at
            if since_last_restart < WATCHDOG_MIN_RESTART_INTERVAL_S:
                continue
            # Level "error" (not just warn) so the Logs tab surfaces this
            # prominently and downstream alerting hooks fire. Telemetry
            # being stale for 10+ minutes IS a real problem the user
            # should know about, even if the auto-restart recovers it.
            event(
                "error", "watchdog",
                f"Cloud telemetry stale {age:.0f}s in state={state.cloud_state}; "
                "restarting cloud_loop",
                age_s=round(age, 1), state=state.cloud_state,
            )
            # Cancel + recreate. Drop the client too so the next login()
            # starts with a fresh httpx connection pool — important if a
            # half-open TCP socket is what hung us.
            if state.cloud_task and not state.cloud_task.done():
                state.cloud_task.cancel()
                try:
                    await asyncio.wait_for(state.cloud_task, timeout=5.0)
                except (Exception, TimeoutError):
                    pass
            if state.cloud_client:
                try:
                    await asyncio.wait_for(state.cloud_client.aclose(),
                                            timeout=5.0)
                except Exception:
                    pass
                state.cloud_client = None
            state.cloud_state = "logging-in"
            state.cloud_task = asyncio.create_task(
                cloud_loop(), name="cloud_loop"
            )
            last_restart_at = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Watchdog itself must never die — log and continue.
            log.exception("watchdog iteration error: %s", e)


# ---- merged poll output ----
def merged_poll() -> dict:
    """Return the current cloud telemetry snapshot."""
    now = time.time()
    cloud_age = (now - state.cloud_ts) if state.cloud_ts else None

    src = "cloud" if state.cloud_telemetry is not None else None
    tele = state.cloud_telemetry
    device = state.cloud_device

    pause_remaining = max(0.0, state.pause_until - now) if state.pause_until else 0.0
    contested_remaining = max(0.0, state.contested_until - now) if state.contested_until else 0.0
    # Build a per-device telemetry dict so the server can evaluate automation
    # rules against any device, not just the active one.
    devices_telemetry = {
        sn: {
            "telemetry": state.telemetry_by_sn.get(sn),
            "ts": state.ts_by_sn.get(sn),
        }
        for sn in state.telemetry_by_sn
    }
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
            "devices_telemetry": devices_telemetry,
            "selected_device_id": state.cloud_device_id,
            "pause_until": state.pause_until or None,
            "pause_remaining_s": round(pause_remaining, 1) if pause_remaining else None,
            "contested_until": state.contested_until or None,
            "contested_remaining_s": round(contested_remaining, 1) if contested_remaining else None,
            # Consecutive contention counter — surfaced so the server's
            # alert path can distinguish a one-off contention (e.g. user
            # briefly opened the phone app) from a persistent contender
            # that needs the user to take action. 0 means "not currently
            # contended"; > 0 means "this many consecutive failures since
            # the last successful poll".
            "contested_consecutive": state.contested_consecutive,
        },
    }


# ---- RPC handlers ----
async def handle(method: str, params: dict) -> dict:
    if method == "ping":
        return {"ok": True}

    if method == "connect":
        # If the cloud poller died, RESTART it; otherwise just nudge it
        # awake. The monitor's shutdown sends `disconnect` (which cancels
        # cloud_task), and /api/reconnect does disconnect→connect — so a
        # plain `connect` MUST be able to resurrect a cancelled loop.
        # Setting force_repoll alone is a no-op when the task is already
        # done (nothing is awaiting the event), which left the cloud
        # session dead after every monitor restart until the bridge
        # process itself was restarted.
        if state.cloud_creds and (state.cloud_task is None
                                  or state.cloud_task.done()):
            log.info("connect RPC: cloud_task is dead — restarting cloud_loop")
            state.cloud_task = asyncio.create_task(cloud_loop(), name="cloud_loop")
        else:
            log.info("force_repoll set by: connect RPC")
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
        state.cloud_props_raw = {}
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
                "device_type": device_type_for(match["model_code"]),
            }
            # Drop stale telemetry so the UI doesn't briefly show old data
            state.cloud_telemetry = None
            state.cloud_props_raw = {}
            state.cloud_ts = None
            log.info("force_repoll set by: select_device RPC")
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
        state.cloud_props_raw = {}
        state.cloud_ts = None
        return {"ok": True, "cleared": where}

    if method == "pause_polling":
        # Pause the cloud poller so the user can use the phone app without
        # the bridge stealing the session back. seconds defaults to 10 min;
        # clamped to [1, 3600] so a typo doesn't pause for a year.
        try:
            seconds = int(params.get("seconds") or 600)
        except (TypeError, ValueError):
            seconds = 600
        seconds = max(1, min(seconds, 3600))
        state.pause_until = time.time() + seconds
        state.cloud_state = "paused"
        event("info", "session", f"Polling paused by user for {seconds}s",
              seconds=seconds)
        return {"ok": True, "pause_until": state.pause_until, "seconds": seconds}

    if method == "resume_polling":
        was_paused = state.pause_until > time.time()
        state.pause_until = 0.0
        # Also clear an active auto-cooldown — the user is explicitly asking
        # to take the session back now.
        state.contested_until = 0.0
        if state.cloud_state in ("paused", "contested"):
            state.cloud_state = "logging-in"
        # Nudge the loop awake so we re-poll right away.
        log.info("force_repoll set by: resume_polling RPC")
        state.cloud_force_repoll.set()
        event("info", "session", "Polling resumed by user", was_paused=was_paused)
        return {"ok": True, "was_paused": was_paused}

    if method == "get_events":
        try:
            limit = int(params.get("limit") or 100)
            since = float(params.get("since") or 0.0)
        except (TypeError, ValueError):
            limit, since = 100, 0.0
        return {"ok": True, "events": get_events(limit=limit, since=since)}

    if method == "cloud_probe":
        # Diagnostic — try a handful of speculative endpoints to find
        # per-battery / expansion data the iOS app uses but we don't
        # currently parse.
        device_id = (params.get("device_id") or "").strip()
        if not device_id:
            device_id = state.cloud_device_id or ""
        if not device_id or not state.cloud_client:
            return {"ok": False, "error": "no device or cloud client",
                    "results": {}}
        # Resolve the device SN for SN-keyed endpoints (pack list,
        # firmware/upgrade). Prefer an explicit param; else look it up
        # from the cloud client's device list by device_id.
        device_sn = (params.get("device_sn") or "").strip()
        model_code = params.get("model_code")
        match = next((d for d in (state.cloud_client.devices or [])
                      if d.device_id == device_id), None)
        if not device_sn and match:
            device_sn = match.device_sn
        if model_code is None and match:
            model_code = match.model_code
        try:
            results = await state.cloud_client.probe_endpoints(
                device_id, device_sn=device_sn, model_code=model_code)
            return {"ok": True, "device_id": device_id,
                    "device_sn": device_sn, "results": results}
        except Exception as e:
            return {"ok": False, "error": str(e), "results": {}}

    if method == "get_raw_props":
        # Diagnostic dump of the raw cloud-property dict for a device.
        # Returns whatever keys the cloud has pushed/polled — useful for
        # identifying fields we don't yet parse (extension batteries,
        # per-PV solar, etc.).
        device_sn = (params.get("device_sn") or "").strip()
        if not device_sn:
            device_sn = (state.cloud_device or {}).get("device_sn", "")
        if not device_sn:
            return {"ok": False, "error": "no device_sn", "props": {}}
        return {"ok": True, "device_sn": device_sn,
                "props": dict(state.props_raw_by_sn.get(device_sn, {}))}

    if method == "get_battery_packs":
        # Per-expansion-battery state. The cloud pushes updates over MQTT
        # (SubDevicePropertyChange), so we serve from the in-memory cache
        # whenever possible — the HTTP endpoint is the cold-start fallback.
        device_sn = (params.get("device_sn") or "").strip()
        if not device_sn:
            device_sn = (state.cloud_device or {}).get("device_sn", "")
        if not device_sn:
            return {"ok": False, "error": "no device_sn", "packs": []}
        cached = state.battery_packs_by_sn.get(device_sn)
        if cached is not None:
            return {"ok": True, "device_sn": device_sn, "packs": cached,
                    "fetched_at": state.packs_ts_by_sn.get(device_sn, 0.0),
                    "source": "mqtt"}
        if not state.cloud_client:
            return {"ok": False, "error": "cloud client not initialised", "packs": []}
        try:
            packs = await state.cloud_client.fetch_battery_packs(device_sn)
        except Exception as e:
            return {"ok": False, "error": str(e), "packs": []}
        state.battery_packs_by_sn[device_sn] = packs
        state.packs_ts_by_sn[device_sn] = time.time()
        return {"ok": True, "device_sn": device_sn, "packs": packs,
                "fetched_at": state.packs_ts_by_sn[device_sn],
                "source": "http"}

    if method == "set_output":
        # Output toggles go over MQTT (emqx.jackeryapp.com). The cloud_client
        # publishes the command and waits for the broker PUBACK; the actual
        # property change shows up on the next /device/property poll.
        port = (params.get("port") or "").lower()
        on = bool(params.get("on"))
        if port not in ("ac", "dc", "usb", "car"):
            return {"ok": False, "error": f"unknown port: {port!r}"}
        if not state.cloud_client:
            return {"ok": False, "error": "cloud client not initialised — sign in first"}
        # Honour an explicit device_sn from the caller — that's how the
        # per-browser view tells us "toggle the device I'm looking at,"
        # which can differ from the bridge-active device. Validate it
        # against the account's known devices so we don't accept a stale
        # SN. Falls back to the bridge-active device when omitted.
        requested_sn = (params.get("device_sn") or "").strip() or None
        if requested_sn:
            known = {d.get("device_sn") for d in state.cloud_devices or []}
            if requested_sn not in known:
                return {"ok": False,
                        "error": f"device_sn {requested_sn!r} is not on this account"}
            device_sn = requested_sn
        else:
            device = state.cloud_device or {}
            device_sn = device.get("device_sn")
        if not device_sn:
            return {"ok": False, "error": "no device_sn — wait for first poll"}
        try:
            ack = await state.cloud_client.publish_command(device_sn, port, on)
        except Exception as e:
            event("error", "mqtt", f"set_output({port}, {on}) failed: {e}",
                  port=port, on=on, device_sn=device_sn)
            return {"ok": False, "error": str(e)}
        event("info", "mqtt",
              f"Output {port.upper()} -> {'ON' if on else 'OFF'} on {device_sn}",
              port=port, on=on, device_sn=device_sn,
              action_id=ack.get("action_id"))
        # Force a quick re-poll so the UI reflects the new state without
        # waiting for the next 15s poll cycle.
        log.info("force_repoll set by: set_output RPC")
        state.cloud_force_repoll.set()
        return {"ok": True, **ack}

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
    # Watchdog runs regardless of whether creds are set yet — if they're
    # added later via the set_credentials RPC, the watchdog will kick in
    # without needing a server restart.
    bg.append(asyncio.create_task(cloud_watchdog_loop(), name="cloud_watchdog"))

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
