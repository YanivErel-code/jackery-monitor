"""
Open-Meteo wrapper for solar irradiance + cloud cover forecasts.

Open-Meteo is free, no API key required. We pull both *past* observations
(used to fit a per-device solar regression) and *future* hourly forecasts
(used to predict solar generation) in a single call via past_days +
forecast_days.

Cache lives in-process with a TTL — forecasts don't change every minute and
the regression refit is also infrequent. A single 5-day forecast call costs
~one HTTP request per hour at the default cache TTL.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("weather_client")

API_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL_S = 3600  # 1 hour
DEFAULT_PAST_DAYS = 14
DEFAULT_FORECAST_DAYS = 5

_cache: dict[str, tuple[float, dict]] = {}  # key -> (expires_at, data)
_cache_lock = threading.Lock()


def _cache_key(lat: float, lon: float, past_days: int, forecast_days: int) -> str:
    # Round lat/lon so trivially-different coords hit the same cache entry.
    return f"{round(lat, 3)}:{round(lon, 3)}:{past_days}:{forecast_days}"


async def fetch_irradiance(
    lat: float,
    lon: float,
    past_days: int = DEFAULT_PAST_DAYS,
    forecast_days: int = DEFAULT_FORECAST_DAYS,
) -> dict[str, Any]:
    """Return hourly GHI + cloud cover, past N days through forecast N days.

    Shape:
      {"hourly": [{"ts": int, "ghi_w_m2": float, "cloud_cover_pct": float}, ...],
       "fetched_at": float, "lat": float, "lon": float}
    """
    if lat == 0 and lon == 0:
        # 0,0 is our sentinel for "unconfigured" (it's in the Atlantic anyway).
        return {"hourly": [], "fetched_at": 0.0, "lat": 0.0, "lon": 0.0,
                "error": "lat/lon not configured"}

    key = _cache_key(lat, lon, past_days, forecast_days)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "shortwave_radiation,cloud_cover",
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timezone": "auto",
        "timeformat": "unixtime",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(API_URL, params=params)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            log.warning("Open-Meteo fetch failed: %s", e)
            return {"hourly": [], "fetched_at": now, "lat": lat, "lon": lon,
                    "error": str(e)}

    hourly = j.get("hourly") or {}
    times = hourly.get("time") or []
    ghi = hourly.get("shortwave_radiation") or []
    cloud = hourly.get("cloud_cover") or []
    rows = [
        {"ts": int(t),
         "ghi_w_m2": float(g) if g is not None else 0.0,
         "cloud_cover_pct": float(c) if c is not None else 0.0}
        for t, g, c in zip(times, ghi, cloud, strict=False)
    ]
    out = {"hourly": rows, "fetched_at": now, "lat": lat, "lon": lon}

    with _cache_lock:
        _cache[key] = (now + CACHE_TTL_S, out)
    log.info("weather: %d hours fetched (past %d / forecast %d days) for (%.3f,%.3f)",
             len(rows), past_days, forecast_days, lat, lon)
    return out


def clear_cache() -> None:
    """Drop cached forecasts. Mostly for tests."""
    with _cache_lock:
        _cache.clear()
