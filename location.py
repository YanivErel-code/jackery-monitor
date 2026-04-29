"""
Persistent device location for the forecast feature.

Lives at /data/location.json. Set once via the browser geolocation prompt
when the user first opens the Forecast tab; read by the forecast endpoint
to call Open-Meteo. Not a "setting" — it's per-physical-install state and
the user shouldn't have to type their lat/lon by hand.

Schema: {"latitude": float, "longitude": float, "updated_at": float}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("location")

LOCATION_PATH = os.environ.get("JACKERY_LOCATION_FILE", "/data/location.json")

_lock = threading.Lock()


def _validate(lat: Any, lon: Any) -> tuple[float, float] | None:
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        return None
    # Reject (0, 0) — it's in the Atlantic and almost certainly a sensor
    # error rather than a real user location.
    if flat == 0.0 and flon == 0.0:
        return None
    return flat, flon


def _read_raw() -> dict | None:
    """Internal: load and validate the on-disk dict (or None)."""
    try:
        with open(LOCATION_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("location file unreadable: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def get() -> dict | None:
    """Return {"latitude", "longitude", "updated_at",
       "utc_offset_seconds"?, "timezone"?} or None if unset."""
    with _lock:
        data = _read_raw()
    if not data:
        return None
    pair = _validate(data.get("latitude"), data.get("longitude"))
    if pair is None:
        return None
    out: dict = {
        "latitude": pair[0], "longitude": pair[1],
        "updated_at": float(data.get("updated_at") or 0.0),
    }
    if "utc_offset_seconds" in data:
        try:
            out["utc_offset_seconds"] = int(data["utc_offset_seconds"])
        except (TypeError, ValueError):
            pass
    if data.get("timezone"):
        out["timezone"] = str(data["timezone"])
    return out


def set(lat: Any, lon: Any) -> dict[str, float] | None:
    """Persist (lat, lon) atomically. Returns the saved record on success."""
    pair = _validate(lat, lon)
    if pair is None:
        return None
    record = {"latitude": pair[0], "longitude": pair[1],
              "updated_at": time.time()}
    with _lock:
        os.makedirs(os.path.dirname(LOCATION_PATH) or ".", exist_ok=True)
        tmp = LOCATION_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, LOCATION_PATH)
    log.info("location saved: lat=%.4f lon=%.4f", pair[0], pair[1])
    return record


def update_timezone(utc_offset_seconds: int,
                    timezone: str | None = None) -> bool:
    """Merge a UTC-offset (and optional IANA timezone name) into the
    location record. Works whether or not lat/lon are set — the offset
    can come from Open-Meteo (with lat/lon) or the device's own `uo`
    field (without lat/lon). Either way the resulting file lets
    energy_db._start_of_day bucket "today" at the user's local midnight."""
    with _lock:
        data = _read_raw() or {}
        try:
            data["utc_offset_seconds"] = int(utc_offset_seconds)
        except (TypeError, ValueError):
            return False
        if timezone:
            data["timezone"] = str(timezone)
        if "updated_at" not in data:
            data["updated_at"] = time.time()
        os.makedirs(os.path.dirname(LOCATION_PATH) or ".", exist_ok=True)
        tmp = LOCATION_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LOCATION_PATH)
    return True


def get_tz_offset() -> int | None:
    """Return the stored UTC offset in seconds, or None. Independent of
    lat/lon — works when only the device's `uo` field has populated the
    record."""
    with _lock:
        data = _read_raw()
    if not data:
        return None
    raw = data.get("utc_offset_seconds")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def clear() -> bool:
    """Remove the stored location."""
    with _lock:
        try:
            os.remove(LOCATION_PATH)
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            log.warning("location clear failed: %s", e)
            return False
