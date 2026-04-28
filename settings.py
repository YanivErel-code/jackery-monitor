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
import time
from pathlib import Path
from typing import Any, Optional

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
        "default": 15,
        "type": "int",
        "min": 5,
        "max": 600,
        "label": "Cloud poll interval (s)",
        "hint": "How often the bridge polls the Jackery cloud. Lower = fresher data, but more API calls.",
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
    "low_battery_threshold": {
        "env": "LOW_BATTERY_THRESHOLD",
        "default": 20,
        "type": "int",
        "min": 1,
        "max": 99,
        "label": "Low-battery alert threshold (%)",
        "hint": "Battery percentage below which the dashboard fires a low-battery alert.",
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


def _coerce(key: str, raw: Any) -> Optional[int]:
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
