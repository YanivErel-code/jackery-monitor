"""weather_client failure handling: non-empty error + stale-cache serve.

Regression for 2026-07-03: Open-Meteo became unreachable; httpx raised
ConnectTimeout('') whose str() is "", so callers' `if weather.get("error")`
saw a falsy error and served a silently-blank forecast.
"""
from __future__ import annotations

import time

import httpx
import pytest

import weather_client


@pytest.fixture(autouse=True)
def _clear_cache():
    weather_client._cache.clear()
    yield
    weather_client._cache.clear()


class _RaisingClient:
    """Stand-in for httpx.AsyncClient whose .get() always raises."""
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        raise self._exc


@pytest.mark.asyncio
async def test_error_never_empty_on_timeout(monkeypatch):
    monkeypatch.setattr(weather_client.httpx, "AsyncClient",
                        lambda *a, **k: _RaisingClient(httpx.ConnectTimeout("")))
    out = await weather_client.fetch_irradiance(37.0, -122.0)
    assert out["hourly"] == []
    assert out.get("error")                      # non-empty -> callers catch it
    assert "ConnectTimeout" in out["error"]


@pytest.mark.asyncio
async def test_serves_stale_cache_on_failure(monkeypatch):
    key = weather_client._cache_key(37.0, -122.0,
                                    weather_client.DEFAULT_PAST_DAYS,
                                    weather_client.DEFAULT_FORECAST_DAYS)
    good = {"hourly": [{"ts": 1, "ghi_w_m2": 500.0, "cloud_cover_pct": 0.0}],
            "fetched_at": time.time() - 9999, "lat": 37.0, "lon": -122.0}
    # Expired entry — the normal read path (expires_at > now) skips it, so
    # only the failure-path stale serve can return it.
    weather_client._cache[key] = (time.time() - 1, good)
    monkeypatch.setattr(weather_client.httpx, "AsyncClient",
                        lambda *a, **k: _RaisingClient(httpx.ConnectTimeout("")))
    out = await weather_client.fetch_irradiance(37.0, -122.0)
    assert out.get("stale") is True
    assert out["hourly"] == good["hourly"]
    assert out.get("stale_error")
    assert "error" not in out  # stale data is usable, not an error state


@pytest.mark.asyncio
async def test_synthesizes_forecast_from_observations_when_api_down(monkeypatch):
    # Live API down AND in-memory cache empty (e.g. post-restart): build a
    # fallback forecast from persisted GHI observations — real past hours +
    # a per-local-hour climatology projected forward.
    import energy_db
    import location
    now = time.time()
    base_hour = int(now) // 3600 * 3600
    obs = []
    for d in range(1, 4):                       # 3 days of hourly obs
        for h in range(24):
            ts = base_hour - d * 86400 + h * 3600
            local_h = (ts % 86400) // 3600      # tz=0, so UTC hour == local
            ghi = 800.0 if 10 <= local_h <= 14 else 0.0   # midday sun
            obs.append({"ts": ts, "ghi_w_m2": ghi, "cloud_cover_pct": 10.0})

    class _FakeDB:
        def __init__(self, *a, **k):
            pass

        def list_weather_observations(self, since_ts=0, limit=100000):
            return [o for o in obs if o["ts"] >= since_ts]

        def get_weather_forecast(self, since_ts=0):
            return [], 0        # nothing stored -> falls through to synth

    monkeypatch.setattr(energy_db, "EnergyDB", _FakeDB)
    monkeypatch.setattr(location, "get_tz_offset", lambda: 0)
    monkeypatch.setattr(weather_client.httpx, "AsyncClient",
                        lambda *a, **k: _RaisingClient(httpx.ConnectTimeout("")))

    out = await weather_client.fetch_irradiance(37.0, -122.0)
    assert out.get("synthetic") is True
    assert "error" not in out
    fut = [h for h in out["hourly"] if h["ts"] >= now]
    assert fut, "no future hours synthesized"
    midday = [h["ghi_w_m2"] for h in fut if 10 <= (h["ts"] % 86400) // 3600 <= 14]
    night = [h["ghi_w_m2"] for h in fut if (h["ts"] % 86400) // 3600 in (0, 1, 2, 3)]
    assert midday and max(midday) >= 700      # climatology carried forward
    assert night and max(night) == 0          # dark hours stay dark


@pytest.mark.asyncio
async def test_serves_last_good_db_forecast_when_api_down(monkeypatch):
    # The real last-good forecast persisted to the DB is served (as stale,
    # not synthetic) ahead of the climatology fallback.
    import energy_db
    import location
    now = time.time()
    base_hour = int(now) // 3600 * 3600
    future = [{"ts": base_hour + (i + 1) * 3600, "ghi_w_m2": 500.0,
               "cloud_cover_pct": 5.0} for i in range(120)]
    past_obs = [{"ts": base_hour - (i + 1) * 3600, "ghi_w_m2": 100.0,
                 "cloud_cover_pct": 0.0} for i in range(48)]

    class _FakeDB:
        def __init__(self, *a, **k):
            pass

        def get_weather_forecast(self, since_ts=0):
            fut = [r for r in future if r["ts"] >= since_ts]
            return (fut, int(now) - 7200) if fut else ([], 0)

        def list_weather_observations(self, since_ts=0, limit=100000):
            return [o for o in past_obs if o["ts"] >= since_ts]

    monkeypatch.setattr(energy_db, "EnergyDB", _FakeDB)
    monkeypatch.setattr(location, "get_tz_offset", lambda: 0)
    monkeypatch.setattr(weather_client.httpx, "AsyncClient",
                        lambda *a, **k: _RaisingClient(httpx.ConnectTimeout("")))

    out = await weather_client.fetch_irradiance(37.0, -122.0)
    assert out.get("stale") is True
    assert not out.get("synthetic")           # real forecast, not climatology
    assert "error" not in out
    fut = [h for h in out["hourly"] if h["ts"] >= now]
    assert len(fut) >= 100                     # served the stored 5-day curve
