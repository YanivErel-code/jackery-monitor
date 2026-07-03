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
