"""Tests for weather_client.geocode — the Open-Meteo geocoding wrapper
that powers the manual-location override on the Forecast tab.

httpx is mocked so these run hermetically. We use asyncio.run() instead
of pytest-asyncio because the rest of the suite doesn't have it.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import httpx
import pytest

import weather_client


def _run(coro):
    return asyncio.run(coro)


def _mock_response(json_payload):
    """Build an httpx.Response stand-in with .json() / raise_for_status()."""
    m = mock.MagicMock()
    m.json.return_value = json_payload
    m.raise_for_status.return_value = None
    return m


def _patched_client(get_side_effect=None, get_return=None):
    """Patch httpx.AsyncClient so its `async with` instance has an `await
    instance.get(...)` that returns/raises what we ask for."""
    cm = mock.patch.object(httpx, "AsyncClient")
    mclient = cm.start()
    instance = mclient.return_value.__aenter__.return_value
    if get_side_effect is not None:
        instance.get = mock.AsyncMock(side_effect=get_side_effect)
    else:
        instance.get = mock.AsyncMock(return_value=get_return)
    return cm, instance


def test_geocode_empty_query_returns_no_results():
    out = _run(weather_client.geocode(""))
    assert out == {"results": []}
    out = _run(weather_client.geocode("   "))
    assert out == {"results": []}


def test_geocode_returns_normalised_results():
    payload = {
        "results": [
            {
                "name": "San Jose",
                "admin1": "California",
                "country": "United States",
                "latitude": 37.33939,
                "longitude": -121.89496,
                "timezone": "America/Los_Angeles",
            },
            {
                "name": "San Jose",
                "admin1": "San Jose",
                "country": "Costa Rica",
                "latitude": 9.93333,
                "longitude": -84.08333,
                "timezone": "America/Costa_Rica",
            },
        ],
    }
    cm, _ = _patched_client(get_return=_mock_response(payload))
    try:
        out = _run(weather_client.geocode("San Jose", count=2))
    finally:
        cm.stop()

    assert len(out["results"]) == 2
    assert out["results"][0]["name"] == "San Jose"
    assert out["results"][0]["admin1"] == "California"
    assert out["results"][0]["latitude"] == pytest.approx(37.33939)
    assert out["results"][1]["country"] == "Costa Rica"
    assert "error" not in out


def test_geocode_network_error_returns_empty_list_with_error():
    cm, _ = _patched_client(get_side_effect=httpx.ConnectError("boom"))
    try:
        out = _run(weather_client.geocode("San Jose"))
    finally:
        cm.stop()
    assert out["results"] == []
    assert "error" in out  # caller can surface or ignore


def test_geocode_skips_malformed_rows():
    """A row missing latitude shouldn't crash the whole call — we drop
    just that row and return the rest."""
    payload = {
        "results": [
            {"name": "Good", "latitude": 1.0, "longitude": 2.0},
            {"name": "Missing lat", "longitude": 2.0},
            {"name": "Bad lat", "latitude": "not-a-number", "longitude": 2.0},
            {"name": "Good 2", "latitude": 3.0, "longitude": 4.0},
        ],
    }
    cm, _ = _patched_client(get_return=_mock_response(payload))
    try:
        out = _run(weather_client.geocode("anywhere"))
    finally:
        cm.stop()
    names = [r["name"] for r in out["results"]]
    assert names == ["Good", "Good 2"]


def test_geocode_count_is_clamped():
    """count outside [1, 10] gets clamped — we don't trust callers."""
    captured = {}

    async def fake_get(url, params=None):
        captured["params"] = params
        return _mock_response({"results": []})

    cm, _ = _patched_client(get_side_effect=fake_get)
    try:
        _run(weather_client.geocode("x", count=0))
        assert captured["params"]["count"] == 1
        _run(weather_client.geocode("x", count=999))
        assert captured["params"]["count"] == 10
    finally:
        cm.stop()
