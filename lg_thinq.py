"""Read-only LG appliance monitoring and opt-in Jackery grid-charge interlocks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import crypto_util

log = logging.getLogger("lg_thinq")
POLL_SECONDS = 30
MAX_STATUS_AGE = 75
FINISH_MARGIN_SECONDS = 300
OFF_STATES = frozenset({"OFF", "POWER_OFF"})
ACTIVE_STATES = frozenset(
    {
        "ON",
        "POWER_ON",
        "RUNNING",
        "DRYING",
        "COOLING",
        "HEATING",
        "PAUSE",
        "PAUSED",
        "WRINKLE_PREVENT",
        "WRINKLE_CARE",
        "DELAYED_START",
        "RESERVATION",
        "PREHEATING",
        "COOKING",
    }
)


class InterlockBlocked(RuntimeError):
    pass


class ThinQUnavailable(RuntimeError):
    pass


def credentials_path() -> Path:
    return Path(os.environ.get("JACKERY_LG_THINQ_CREDS_FILE", "/data/lg-thinq-creds.json"))


def load_credentials() -> dict | None:
    try:
        blob = json.loads(credentials_path().read_text())
        if not isinstance(blob, dict) or blob.get("alg") != "AES-256-GCM":
            return None
        plaintext = crypto_util.decrypt(blob)
        result = json.loads(plaintext) if plaintext else None
        if isinstance(result, dict) and all(
            isinstance(result.get(key), str) and result[key]
            for key in ("token", "country_code", "client_id")
        ):
            return result
    except (OSError, ValueError, TypeError):
        pass
    return None


def prepare_credentials(token: str, country_code: str) -> dict:
    from thinqconnect.country import Country

    if not isinstance(token, str) or not re.fullmatch(r"thinqpat_[A-Za-z0-9_-]{30,256}", token):
        raise ValueError("Enter a valid LG ThinQ personal access token")
    if not isinstance(country_code, str) or country_code not in {
        country.value for country in Country
    }:
        raise ValueError("Choose a supported two-letter LG account country")
    previous = load_credentials() or {}
    return {
        "token": token,
        "country_code": country_code,
        "client_id": previous.get("client_id") or str(uuid.uuid4()),
    }


def save_credentials(token: str, country_code: str) -> dict:
    credentials = prepare_credentials(token, country_code)
    blob = crypto_util.encrypt(json.dumps(credentials).encode())
    file = credentials_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".lg-thinq-", dir=file.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(blob, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    monitor.invalidate()
    return credentials


def clear_credentials() -> None:
    credentials_path().unlink(missing_ok=True)
    monitor.invalidate()


def credential_stamp(credentials: dict | None) -> str:
    return hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()


def classify_state(payload: Any) -> dict:
    signals: list[str] = []
    timers: list[int | None] = []

    def visit(value, parent=""):
        if isinstance(value, dict):
            first_signal = len(signals)
            for key, child in value.items():
                normalized = re.sub(r"[^a-z]", "", key.lower())
                if normalized in {"runstate", "operation"}:
                    names = (
                        {re.sub(r"[^a-z]", "", name.lower()) for name in child}
                        if isinstance(child, dict)
                        else set()
                    )
                    complete = (
                        "currentstate" in names
                        if normalized == "runstate"
                        else any(name.endswith("operationmode") for name in names)
                    )
                    if not complete:
                        signals.append("UNKNOWN")
                relevant = (
                    (parent == "runstate" and normalized == "currentstate")
                    or (parent == "operation" and normalized.endswith("operationmode"))
                    or normalized in {"power", "powerstate"}
                )
                if relevant:
                    if isinstance(child, bool):
                        signals.append("ON" if child else "OFF")
                    elif isinstance(child, str):
                        signals.append(child.strip().upper()[:80])
                    else:
                        signals.append("UNKNOWN")
                else:
                    visit(child, normalized)
            owns_state = any(
                re.sub(r"[^a-z]", "", name.lower())
                in {"runstate", "operation", "power", "powerstate"}
                for name in value
            )
            if owns_state and not all(signal in OFF_STATES for signal in signals[first_signal:]):
                timer = value.get("timer")
                hours = timer.get("remainHour") if isinstance(timer, dict) else None
                minutes = timer.get("remainMinute") if isinstance(timer, dict) else None
                valid = (
                    isinstance(hours, int)
                    and not isinstance(hours, bool)
                    and 0 <= hours <= 168
                    and isinstance(minutes, int)
                    and not isinstance(minutes, bool)
                    and 0 <= minutes < 60
                )
                timers.append(hours * 60 + minutes if valid else None)
        elif isinstance(value, list):
            for child in value:
                visit(child, parent)

    visit(payload)
    if any(signal in ACTIVE_STATES for signal in signals):
        activity = "active"
    elif signals and all(signal in OFF_STATES for signal in signals):
        activity = "off"
    else:
        activity = "unknown"
    remaining = max(timers) if timers and all(timer is not None for timer in timers) else None
    return {"activity": activity, "states": signals, "remaining_minutes": remaining}


def make_api(session, credentials: dict):
    from thinqconnect.thinq_api import ThinQApi

    return ThinQApi(
        session=session,
        access_token=credentials["token"],
        country_code=credentials["country_code"],
        client_id=credentials["client_id"],
    )


def safe_error(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"\d{4}", code):
        return {
            "1103": "LG token is invalid",
            "1302": "LG token was not found",
            "1305": "LG token lacks the required read permissions",
            "1306": "LG API rate limit reached",
            "1222": "LG appliance is disconnected",
        }.get(code, "LG API unavailable (" + code + ")")
    return "LG connection unavailable"


class ThinQMonitor:
    def __init__(self):
        self.devices: dict[str, dict] = {}
        self.credential_stamp = ""
        self.error: str | None = None
        self.listed_at = 0.0
        self.lock = asyncio.Lock()

    def invalidate(self):
        self.devices = {}
        self.credential_stamp = ""
        self.listed_at = 0.0
        self.error = None

    async def refresh(self, device_ids: set[str] | None = None, *, force_list=False):
        import aiohttp

        async with self.lock:
            credentials = load_credentials()
            if not credentials:
                self.invalidate()
                return
            stamp = credential_stamp(credentials)
            devices = (
                {key: dict(value) for key, value in self.devices.items()}
                if self.credential_stamp == stamp
                else {}
            )
            async with aiohttp.ClientSession() as session:
                api = make_api(session, credentials)
                try:
                    if force_list or not devices or time.time() - self.listed_at >= 300:
                        entries = await api.async_get_device_list(timeout=8)
                        if not isinstance(entries, list) or len(entries) > 200:
                            raise ThinQUnavailable("Invalid LG device list")
                        listed = {}
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            device_id, info = entry.get("deviceId"), entry.get("deviceInfo") or {}
                            if not isinstance(device_id, str) or not re.fullmatch(
                                r"[A-Za-z0-9_-]{1,256}", device_id
                            ):
                                continue
                            if not isinstance(info, dict):
                                continue
                            listed[device_id] = {
                                **devices.get(device_id, {}),
                                "device_id": device_id,
                                "alias": str(info.get("alias") or "LG appliance")[:120],
                                "device_type": str(info.get("deviceType") or "unknown")[:80],
                                "model": str(info.get("modelName") or "")[:120],
                            }
                        devices = listed
                    else:
                        entries = None
                    for device_id, device in devices.items():
                        if device_ids is not None and device_id not in device_ids:
                            continue
                        try:
                            payload = await api.async_get_device_status(device_id, timeout=8)
                            device.update(
                                classify_state(payload),
                                observed_at=time.time(),
                                error=None,
                                disconnected=False,
                                assumed_off=False,
                            )
                        except Exception as error:
                            from thinqconnect.thinq_api import ThinQAPIException

                            disconnected = (
                                isinstance(error, ThinQAPIException) and error.code == "1222"
                            )
                            device.update(
                                activity="off" if disconnected else "unknown",
                                observed_at=time.time() if disconnected else 0,
                                error=None if disconnected else safe_error(error),
                                states=["DISCONNECTED"] if disconnected else [],
                                remaining_minutes=None,
                                disconnected=disconnected,
                                assumed_off=disconnected,
                            )
                    if credential_stamp(load_credentials()) != stamp:
                        return
                    self.devices, self.credential_stamp, self.error = devices, stamp, None
                    if entries is not None:
                        self.listed_at = time.time()
                except Exception as error:
                    if credential_stamp(load_credentials()) == stamp:
                        self.error = safe_error(error)

    def public_status(self) -> dict:
        credentials = load_credentials()
        matches = bool(credentials) and credential_stamp(credentials) == self.credential_stamp
        now = time.time()
        devices = []
        for device in self.devices.values() if matches else []:
            record = dict(device)
            age = now - float(record.get("observed_at") or 0)
            record["status_age_seconds"] = round(age, 1) if record.get("observed_at") else None
            record["fresh"] = (
                bool(record.get("observed_at")) and -5 <= age <= MAX_STATUS_AGE and not self.error
            )
            if not record["fresh"]:
                record["activity"] = "unknown"
            devices.append(record)
        return {
            "configured": bool(credentials),
            "country_code": (credentials or {}).get("country_code", "US"),
            "error": self.error,
            "devices": devices,
            "poll_seconds": POLL_SECONDS,
            "max_status_age_seconds": MAX_STATUS_AGE,
        }


monitor = ThinQMonitor()


def block_reason(config: dict) -> str | None:
    if not config.get("lg_interlock_enabled"):
        return None
    prefix = "LG 240 V interlock: "
    if config.get("lg_manual_hold"):
        return prefix + "manual reservation keeps 240 V available"
    selected = config.get("lg_device_ids") or []
    if not selected:
        return prefix + "no protected appliances selected"
    status = monitor.public_status()
    if not status["configured"] or status["error"]:
        return prefix + "LG connection unavailable; charging held OFF"
    devices = {device["device_id"]: device for device in status["devices"]}
    for device_id in selected:
        device = devices.get(device_id)
        if not device or not device.get("fresh") or device.get("activity") != "off":
            label = device.get("alias", "Selected appliance") if device else "Selected appliance"
            return prefix + label + " active, stale or not verified OFF"
    return None


def block_reason_for_host(host: str) -> str | None:
    import smart_charge

    for config in smart_charge.get_all_configs().values():
        if config.get("kasa_device_host") == host:
            reason = block_reason(config)
            if reason:
                return reason
    return None


def planning_context(config: dict, *, now_ts: float) -> dict | None:
    if not config.get("lg_interlock_enabled"):
        return None
    reason = block_reason(config)
    context = {
        "blocked_now": bool(reason),
        "reason": reason,
        "expected_finish_ts": None,
        "charge_not_before_ts": None,
        "timing_uncertain": bool(reason),
        "finish_margin_seconds": FINISH_MARGIN_SECONDS,
        "soc_at_finish_pct": None,
        "appliances": [],
    }
    status = monitor.public_status()
    selected = set(config.get("lg_device_ids") or [])
    devices = {device["device_id"]: device for device in status["devices"]}
    context["appliances"] = [
        {
            key: devices[device_id].get(key)
            for key in (
                "device_id",
                "activity",
                "fresh",
                "observed_at",
                "remaining_minutes",
                "states",
                "disconnected",
                "assumed_off",
            )
        }
        for device_id in sorted(selected)
        if device_id in devices
    ]
    if not reason:
        return context
    if config.get("lg_manual_hold") or not selected or not status["configured"] or status["error"]:
        return context
    finishes = []
    timed_states = {"RUNNING", "DRYING", "COOLING", "HEATING", "PREHEATING", "COOKING"}
    permitted_states = timed_states | OFF_STATES | {"ON", "POWER_ON"}
    for device_id in selected:
        device = devices.get(device_id)
        if not device or not device.get("fresh"):
            return context
        if device.get("activity") == "off":
            continue
        remaining = device.get("remaining_minutes")
        states = set(device.get("states") or [])
        if (
            device.get("activity") != "active"
            or not isinstance(remaining, int)
            or isinstance(remaining, bool)
            or remaining <= 0
            or not states.intersection(timed_states)
            or not states.issubset(permitted_states)
        ):
            return context
        finish = math.ceil(device["observed_at"] + remaining * 60)
        if finish <= now_ts:
            return context
        finishes.append(finish)
    if finishes:
        context.update(
            expected_finish_ts=max(finishes),
            charge_not_before_ts=max(finishes) + FINISH_MARGIN_SECONDS,
            timing_uncertain=False,
        )
    return context


def soc_at_finish(
    forecast: dict, current_soc: float, *, now_ts: float, finish_ts: float
) -> float | None:
    previous_ts, previous_soc = now_ts, current_soc
    if finish_ts < now_ts:
        return None
    for hour in forecast.get("forecast") or []:
        end = hour["ts"] + (0 if "duration_h" in hour else 3600)
        soc = hour.get("predicted_soc")
        if not isinstance(soc, (int, float)) or not math.isfinite(soc):
            return None
        if end <= previous_ts:
            continue
        if finish_ts <= end:
            fraction = (finish_ts - previous_ts) / (end - previous_ts)
            return round(previous_soc + fraction * (soc - previous_soc), 1)
        previous_ts, previous_soc = end, soc
    return None


async def refresh_for_host(host: str):
    import smart_charge

    configs = [
        config
        for config in smart_charge.get_all_configs().values()
        if config.get("lg_interlock_enabled") and config.get("kasa_device_host") == host
    ]
    if not configs:
        return
    selected = {device_id for config in configs for device_id in config.get("lg_device_ids", [])}
    try:
        await monitor.refresh(selected)
    except Exception:
        monitor.error = "LG pre-charge status recheck unavailable"
        raise InterlockBlocked(monitor.error) from None


async def poll_loop(on_update=None):
    import smart_charge

    previous = None
    while True:
        configs = {}
        selected = set()
        try:
            configs = smart_charge.get_all_configs()
            selected = {
                device_id
                for config in configs.values()
                for device_id in config.get("lg_device_ids", [])
            }
            await monitor.refresh(selected)
        except Exception:
            monitor.error = "LG monitoring unavailable"
        if on_update and any(config.get("lg_interlock_enabled") for config in configs.values()):
            status = monitor.public_status()
            summary = {"configured": status["configured"], "error": status["error"], "devices": []}
            for device in sorted(status["devices"], key=lambda record: record["device_id"]):
                if device["device_id"] not in selected:
                    continue
                record = {
                    key: device.get(key)
                    for key in (
                        "device_id",
                        "fresh",
                        "activity",
                        "states",
                        "remaining_minutes",
                        "disconnected",
                        "assumed_off",
                    )
                }
                remaining = device.get("remaining_minutes")
                if device.get("activity") == "active" and remaining:
                    record["finish_minute"] = int((device["observed_at"] + remaining * 60) // 60)
                summary["devices"].append(record)
            signature = json.dumps(summary, sort_keys=True)
            if signature != previous:
                previous = signature
                on_update()
        await asyncio.sleep(POLL_SECONDS)


async def enforce_once(can_control, last_off: dict[str, float]):
    import kasa_client
    import smart_charge

    hosts = {
        config.get("kasa_device_host")
        for serial, config in smart_charge.get_all_configs().items()
        if config.get("lg_interlock_enabled")
        and config.get("kasa_device_host")
        and can_control(serial)
    }
    for host in hosts:
        reason = block_reason_for_host(host)
        if not reason:
            last_off.pop(host, None)
        elif time.monotonic() - last_off.get(host, float("-inf")) >= 30:
            try:
                await kasa_client.set_state(host, False)
                last_off[host] = time.monotonic()
            except Exception:
                log.warning("LG interlock could not switch charging plug OFF; will retry")


async def enforce_loop(can_control):
    last_off: dict[str, float] = {}
    while True:
        try:
            await enforce_once(can_control, last_off)
        except Exception:
            log.warning("LG charging interlock check failed; will retry")
        await asyncio.sleep(5)
