"""
User-tunable runtime settings, persisted to /data/settings.json.

Both server.py and bridge.py read this file on each loop iteration so changes
made through the dashboard /api/settings endpoint take effect immediately
without restarting either process.

Env vars still work as **defaults**: if /data/settings.json is missing or a
key is absent, the env var (or hard-coded fallback) wins. Once the user saves
a value through the UI, it pins the setting and the env-var default is no
longer consulted for that key.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("settings")

# Where the JSON lives. Same /data volume as the energy DB and creds, so it
# survives container restarts.
SETTINGS_PATH = os.environ.get("JACKERY_SETTINGS_FILE", "/data/settings.json")

# Schema: key -> (env_var, default_value, type, min, max, description)
# `type` must be `int`. (We keep it scalar-only on purpose — keeps the UI
# trivial.) min/max are inclusive bounds enforced on save.
SCHEMA: dict[str, dict[str, Any]] = {
    "poll_interval_s": {
        "env": "POLL_INTERVAL_S",
        "default": 2,
        "type": "int",
        "min": 1,
        "max": 300,
        "label": "Server poll interval (s)",
        "hint": "How often the dashboard polls the bridge. The bridge has MQTT push from the device, so 1-2s is fine for snappy UI.",
    },
    "cloud_poll_interval_s": {
        "env": "CLOUD_POLL_INTERVAL_S",
        "default": 60,
        "type": "int",
        "min": 5,
        "max": 600,
        "label": "Cloud poll interval (s)",
        "hint": "How often the bridge polls the Jackery cloud. Lower = fresher data on fields not covered by MQTT (battery %, port states, AC/car input split), but more API calls. Tokens are JWTs valid ~30 days; a single login serves many polls, so this knob is purely a freshness-vs-API-load tradeoff. MQTT pushes update ip/op/temp every 2-3s independent of this, so the live power flow stays fresh regardless.",
    },
    "session_contested_cooldown_s": {
        "env": "SESSION_CONTESTED_COOLDOWN_S",
        "default": 60,
        "type": "int",
        "min": 10,
        "max": 600,
        "label": "Session-contested cooldown (s)",
        "hint": "How long the bridge waits before reclaiming the session after the phone app logs in.",
    },
    "inverter_trip_recovery_min_w": {
        "env": "INVERTER_TRIP_RECOVERY_MIN_W",
        "default": 0,
        "type": "int",
        "min": 0,
        "max": 2000,
        "label": "Inverter trip-recovery floor (W)",
        "hint": "0 disables. Set BELOW your 24/7 base load (e.g. 100 on a rig that never idles under 450W): if AC output collapses to/below this while the port still reports ON, the watchdog treats it as a hardware trip and cycles AC off/on (max 2x per episode). Leave 0 unless your rig always has load on the inverter — on a rig where ~0W output is normal this would cause false power cycles.",
    },
    "low_battery_threshold": {
        "env": "LOW_BATTERY_THRESHOLD",
        "default": 20,
        "type": "int",
        "min": 1,
        "max": 99,
        "label": "Low-battery alert threshold (%)",
        "hint": "Battery percentage below which the dashboard fires a low-battery alert.",
    },
    "advisor_trigger_hour": {
        "env": "JACKERY_ADVISOR_HOUR",
        "default": 8,
        "type": "int",
        "min": 0,
        "max": 23,
        "label": "AI advisor daily-review hour",
        "hint": "Local hour (0-23) when the AI advisor reviews yesterday's data per device. 8 = 8 AM local. Requires an API key for the active AI provider (Settings → AI provider); runs once per device per day.",
    },
    "backup_schedule_hour": {
        "env": "JACKERY_BACKUP_HOUR",
        "default": 3,
        "type": "int",
        "min": 0,
        "max": 23,
        "label": "Backup daily-run hour",
        "hint": "Local hour (0-23) when the daily SMB backup to the remote NAS runs. 3 = 3 AM local. Configure the remote target on the Settings page → Backup & Restore section before this fires for the first time.",
    },
    "backup_keep_count": {
        "env": "JACKERY_BACKUP_KEEP",
        "default": 30,
        "type": "int",
        "min": 1,
        "max": 3650,
        "label": "Snapshots to keep",
        "hint": "How many of the most recent successful snapshots to keep on the NAS. Older ones are deleted after each successful run. Set to 365 for a year of dailies; very large values approximate 'keep forever'. Only directories matching our YYYY-MM-DD_HHMMSS naming are pruned — manual files alongside are untouched.",
    },
}


_lock = threading.Lock()
_cache: dict[str, int] = {}
_cache_mtime: float = 0.0


def _read_file() -> dict[str, Any]:
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("settings file unreadable (%s); using env/defaults", e)
        return {}


def _coerce(key: str, raw: Any) -> int | None:
    spec = SCHEMA.get(key)
    if not spec:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _default_for(key: str) -> int:
    spec = SCHEMA[key]
    env = os.environ.get(spec["env"])
    if env is not None:
        try:
            v = int(env)
            return _coerce(key, v) or v
        except ValueError:
            pass
    return int(spec["default"])


def _refresh_cache() -> None:
    """Re-read the file if it's been modified since last load."""
    global _cache, _cache_mtime
    try:
        mtime = Path(SETTINGS_PATH).stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0
    if mtime == _cache_mtime and _cache:
        return
    file_data = _read_file()
    new_cache: dict[str, int] = {}
    for k in SCHEMA:
        v = _coerce(k, file_data.get(k)) if k in file_data else None
        new_cache[k] = v if v is not None else _default_for(k)
    _cache = new_cache
    _cache_mtime = mtime


def get(key: str) -> int:
    """Return the current value for `key`. Cheap; uses an mtime-checked cache."""
    if key not in SCHEMA:
        raise KeyError(f"unknown setting: {key!r}")
    with _lock:
        _refresh_cache()
        return _cache[key]


def all_values() -> dict[str, int]:
    """Snapshot of all current values."""
    with _lock:
        _refresh_cache()
        return dict(_cache)


def schema() -> list[dict[str, Any]]:
    """Self-describing schema for the UI to render the settings form."""
    return [
        {"key": k, **{kk: vv for kk, vv in spec.items() if kk != "env"},
         "value": get(k)}
        for k, spec in SCHEMA.items()
    ]


def update(values: dict[str, Any]) -> dict[str, int]:
    """Persist a partial update. Returns the new full settings snapshot.
       Keys not in SCHEMA are silently ignored. Values out-of-range are clamped."""
    with _lock:
        current = _read_file()
        for k, v in (values or {}).items():
            if k not in SCHEMA:
                continue
            coerced = _coerce(k, v)
            if coerced is None:
                continue
            current[k] = coerced
        os.makedirs(os.path.dirname(SETTINGS_PATH) or ".", exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        # Bust the cache so the next get() reflects the change without
        # waiting for the mtime check.
        global _cache_mtime
        _cache_mtime = 0.0
        _refresh_cache()
        log.info("settings updated: %s", {k: current.get(k) for k in (values or {}) if k in SCHEMA})
        return dict(_cache)
