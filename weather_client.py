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
# Open-Meteo's geocoding API — also free, also no key. Used by the
# manual-location override UI on the Forecast tab so users who got
# bad GPS coords (or whose IP geolocation lied) can pick their city
# by name.
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Reverse geocoding (lat/lon -> human place name). Open-Meteo doesn't
# offer reverse, so we use bigdatacloud's free reverse-geocode endpoint
# — no API key, no registration, CORS-friendly. Used to backfill the
# `label` on locations that were saved via geolocation or coords-only
# (where the user never typed a city name).
REVERSE_GEOCODE_URL = (
    "https://api.bigdatacloud.net/data/reverse-geocode-client"
)
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
            # httpx timeout exceptions stringify to "" (e.g.
            # ConnectTimeout('')), and callers gate on
            # `if weather.get("error")` — an empty string is falsy, so the
            # failure was silently swallowed and the forecast served blank.
            # Always carry a meaningful message.
            msg = str(e) or f"{type(e).__module__}.{type(e).__name__}"
            log.warning("Open-Meteo fetch failed: %s", msg)
            # Serve the last-good cached weather (even past its TTL) so a
            # transient outage doesn't blank the forecast; flag it stale so
            # callers/UI can note it. Fall through to an error only when we
            # have no usable cache.
            with _cache_lock:
                stale = _cache.get(key)
            if stale and (stale[1].get("hourly")):
                data = dict(stale[1])
                data["stale"] = True
                data["stale_error"] = msg
                data["stale_age_s"] = round(now - float(data.get("fetched_at") or now), 1)
                log.info("weather: serving stale cache (%.0fs old) after fetch error",
                         data["stale_age_s"])
                return data
            return {"hourly": [], "fetched_at": now, "lat": lat, "lon": lon,
                    "error": msg}

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
    # Open-Meteo with timezone=auto returns the UTC offset for the lat/lon.
    # Stash it onto the location record so server-side "what's today" math
    # uses the user's local midnight, not the container's UTC midnight.
    utc_offset_seconds = j.get("utc_offset_seconds")
    timezone = j.get("timezone")
    if utc_offset_seconds is not None:
        try:
            import location as device_location
            device_location.update_timezone(int(utc_offset_seconds), timezone)
        except Exception as e:
            log.warning("could not persist timezone offset: %s", e)

    out = {"hourly": rows, "fetched_at": now, "lat": lat, "lon": lon,
           "utc_offset_seconds": utc_offset_seconds, "timezone": timezone}

    with _cache_lock:
        _cache[key] = (now + CACHE_TTL_S, out)
    log.info("weather: %d hours fetched (past %d / forecast %d days) for (%.3f,%.3f) tz=%s",
             len(rows), past_days, forecast_days, lat, lon, timezone)

    # Persist PAST observations so a future learning job has the actual
    # GHI + cloud cover paired with the actual solar_w (samples table).
    # Skip future hours — those are predictions, not observations.
    cutoff_ts = int(now)
    past_obs = [r for r in rows if r["ts"] <= cutoff_ts]
    if past_obs:
        try:
            from energy_db import EnergyDB
            EnergyDB().upsert_weather_observations(past_obs)
        except Exception as e:
            log.debug("weather observation persist failed: %s", e)

    return out


def clear_cache() -> None:
    """Drop cached forecasts. Mostly for tests."""
    with _cache_lock:
        _cache.clear()


async def geocode(query: str, *, count: int = 5) -> dict[str, Any]:
    """Free-text city / place name → list of candidate locations.

    Returns:
      {"results": [
         {"name":..., "admin1":..., "country":..., "latitude":..., "longitude":...},
         ...
      ]}

    On error returns {"results": [], "error": "..."}. We deliberately
    keep the shape stable so the UI can render an empty list rather
    than crashing on transient network failures.
    """
    q = (query or "").strip()
    if not q:
        return {"results": []}
    # Cap count: Open-Meteo allows 1-100; UI shows ~5 typeahead rows.
    # Use an explicit None check so callers can pass 0 (which we clamp
    # up to 1) without falling back to the default of 5.
    if count is None:
        count = 5
    count = max(1, min(int(count), 10))
    params = {
        "name": q,
        "count": count,
        "language": "en",
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(GEOCODE_URL, params=params)
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            log.warning("Open-Meteo geocode failed for %r: %s", q, e)
            return {"results": [], "error": str(e)}

    raw = j.get("results") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            out.append({
                "name": str(item.get("name") or ""),
                "admin1": str(item.get("admin1") or ""),  # state/province
                "country": str(item.get("country") or ""),
                "latitude": float(item["latitude"]),
                "longitude": float(item["longitude"]),
                "timezone": str(item.get("timezone") or ""),
            })
        except (KeyError, TypeError, ValueError):
            # Skip malformed rows rather than failing the whole call.
            continue
    return {"results": out}


async def reverse_geocode(latitude: float,
                          longitude: float) -> str | None:
    """Reverse geocode (lat, lon) -> short human place name like
    "Almaden Valley" or "San Jose". Uses bigdatacloud's free
    reverse-geocode-client endpoint (no key, no registration). Returns
    None on any failure — caller should treat the label as optional."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(REVERSE_GEOCODE_URL, params={
                "latitude": float(latitude),
                "longitude": float(longitude),
                "localityLanguage": "en",
            })
            r.raise_for_status()
            j = r.json()
    except Exception as e:
        log.debug("reverse geocode failed for (%.4f, %.4f): %s",
                  latitude, longitude, e)
        return None
    # Prefer locality (neighborhood) > city > principalSubdivision (state).
    # bigdatacloud occasionally returns the state name in `locality` for
    # remote coords, so we keep all three rather than picking the first.
    name = (j.get("locality") or j.get("city")
            or j.get("principalSubdivision") or None)
    return str(name).strip() if name else None
