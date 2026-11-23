"""
Persistent device location for the forecast feature.

Lives at /data/location.json. Set once via the browser geolocation prompt
when the user first opens the Forecast tab; read by the forecast endpoint
to call Open-Meteo. Not a "setting" — it's per-physical-install state and
the user shouldn't have to type their lat/lon by hand.

Encrypted at rest (AES-256-GCM via crypto_util) — same key file as the
other credential blobs. Lat/lon is PII (precise home coordinates), so
even though SECURITY.md scopes the threat model to image leaks and
casual filesystem access, we treat this like any other secret.

Schema (cleartext form): {"latitude": float, "longitude": float,
         "updated_at": float, "label"?: str,
         "utc_offset_seconds"?: int, "timezone"?: str}

`label` is a human-readable place name (e.g. "San Jose, California, US"),
persisted when the user picks a city from the manual-override search
results so the Forecast tab can show "Forecasting for: San Jose" instead
of bare coordinates. Older records (and coords-only saves) won't have it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import crypto_util

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
    """Internal: load and validate the on-disk dict (or None).

    Supports both encrypted ({v,alg,nonce,tag,ct}) and legacy plaintext
    ({latitude,longitude,...}). Legacy records are auto-migrated to the
    encrypted format on the next write."""
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
    # New encrypted form.
    if "ct" in data and "nonce" in data:
        pt = crypto_util.decrypt(data)
        if pt is None:
            return None
        try:
            inner = json.loads(pt.decode())
        except Exception as e:
            log.error("location payload not valid JSON after decrypt: %s", e)
            return None
        return inner if isinstance(inner, dict) else None
    # Legacy plaintext form — return as-is; the next write will encrypt.
    if "latitude" in data or "utc_offset_seconds" in data:
        log.info("loaded legacy plaintext location file; will encrypt on next write")
        return data
    return None


def _write_record(record: dict) -> None:
    """Encrypt the cleartext record and write atomically. Caller holds _lock."""
    os.makedirs(os.path.dirname(LOCATION_PATH) or ".", exist_ok=True)
    blob = crypto_util.encrypt(json.dumps(record).encode())
    tmp = LOCATION_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, LOCATION_PATH)


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
    # The offset in force now, not the number saved with the record.
    offset = _current_offset(data)
    if offset is not None:
        out["utc_offset_seconds"] = offset
    if data.get("timezone"):
        out["timezone"] = str(data["timezone"])
    if data.get("label"):
        out["label"] = str(data["label"])
    return out


def set(lat: Any, lon: Any, label: Any = None) -> dict | None:
    """Persist (lat, lon) atomically. Returns the saved record on success.

    `label` is an optional human-readable place name persisted alongside
    the coords (e.g. from the manual-override search results). Empty
    strings and non-string values are dropped so the on-disk schema
    stays clean.
    """
    pair = _validate(lat, lon)
    if pair is None:
        return None
    record: dict = {"latitude": pair[0], "longitude": pair[1],
                    "updated_at": time.time()}
    if isinstance(label, str):
        clean = label.strip()
        if clean:
            # Cap at 200 chars — defends against pathological inputs
            # without truncating any reasonable city/admin/country combo.
            record["label"] = clean[:200]
    with _lock:
        _write_record(record)
    # Log label when available; otherwise coarsen lat/lon to ~11km
    # precision so the log doesn't leak the user's precise home.
    if record.get("label"):
        log.info("location saved: label=%r", record["label"])
    else:
        log.info("location saved (~11km): lat=%.1f lon=%.1f",
                 pair[0], pair[1])
    return record


def set_label(label: str) -> bool:
    """Update only the `label` on the existing record. Used to lazily
    backfill a city name on locations saved via geolocation or coords-
    only (where the user didn't pick a search result). No-op if there
    is no record yet, or if the label is empty/non-string. Returns
    True on a real write."""
    if not isinstance(label, str):
        return False
    clean = label.strip()
    if not clean:
        return False
    with _lock:
        data = _read_raw()
        if not data or "latitude" not in data:
            return False
        data["label"] = clean[:200]
        _write_record(data)
    return True


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
        _write_record(data)
    return True


def get_timezone_name() -> str | None:
    """The IANA time-zone name (such as "America/Los_Angeles"), if a
    location has been set."""
    with _lock:
        data = _read_raw()
    name = (data or {}).get("timezone")
    return str(name) if name else None


def _current_offset(data: dict, now: float | None = None) -> int | None:
    """The UTC offset in force at `now` for a stored record.

    When the time-zone name is known the offset is worked out from it,
    so it follows daylight saving. The stored number is only a snapshot:
    it was right when it was saved and goes an hour wrong at the next
    clock change, and a device may keep reporting its own stale offset.
    Without a name, the stored offset is all there is."""
    name = data.get("timezone")
    if name:
        try:
            moment = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(str(name)))
            offset = moment.utcoffset()
            if offset is not None:
                return int(offset.total_seconds())
        except Exception as error:      # unknown name, or no time-zone data on this system
            log.debug("time zone %r unusable, using the stored offset: %s", name, error)
    raw = data.get("utc_offset_seconds")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def get_tz_offset(now: float | None = None) -> int | None:
    """The UTC offset in seconds in force right now (or at `now`), or None
    when nothing is stored. Works without lat/lon: the offset alone can
    come from the device's own `uo` field."""
    with _lock:
        data = _read_raw()
    if not data:
        return None
    return _current_offset(data, now)


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
