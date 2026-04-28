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


def get() -> dict[str, float] | None:
    """Return {"latitude", "longitude", "updated_at"} or None if unset."""
    with _lock:
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
    pair = _validate(data.get("latitude"), data.get("longitude"))
    if pair is None:
        return None
    return {"latitude": pair[0], "longitude": pair[1],
            "updated_at": float(data.get("updated_at") or 0.0)}


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
