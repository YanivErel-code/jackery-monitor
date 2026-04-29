"""Location module: validate, persist, round-trip."""
from __future__ import annotations

import importlib


def _fresh_location(monkeypatch, tmp_path):
    """Reload location.py with LOCATION_PATH pointing into tmp_path."""
    monkeypatch.setenv("JACKERY_LOCATION_FILE", str(tmp_path / "location.json"))
    import location
    importlib.reload(location)
    return location


def test_get_returns_none_when_unset(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    assert loc.get() is None


def test_set_and_get_round_trip(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    saved = loc.set(37.7749, -122.4194)
    assert saved is not None
    assert abs(saved["latitude"] - 37.7749) < 1e-6
    assert abs(saved["longitude"] - (-122.4194)) < 1e-6
    got = loc.get()
    assert got is not None
    assert abs(got["latitude"] - 37.7749) < 1e-6


def test_set_rejects_out_of_range(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    assert loc.set(100, 0) is None      # lat too high
    assert loc.set(-91, 0) is None      # lat too low
    assert loc.set(0, 181) is None      # lon too high
    assert loc.set(0, -181) is None     # lon too low
    assert loc.get() is None


def test_set_rejects_null_island(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    # (0, 0) is the sentinel "no real fix" — reject so we don't try to
    # forecast weather for a buoy in the Atlantic.
    assert loc.set(0, 0) is None
    assert loc.get() is None


def test_set_rejects_non_numeric(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    assert loc.set("hello", "world") is None
    assert loc.set(None, None) is None


def test_clear_removes_stored_location(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    loc.set(37.7749, -122.4194)
    assert loc.get() is not None
    assert loc.clear() is True
    assert loc.get() is None
    # idempotent — calling clear again should still report success
    assert loc.clear() is True


def test_update_timezone_merges_offset(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    loc.set(37.7749, -122.4194)
    # Open-Meteo would return -25200 for PDT (UTC-7).
    assert loc.update_timezone(-25200, "America/Los_Angeles") is True
    got = loc.get()
    assert got["utc_offset_seconds"] == -25200
    assert got["timezone"] == "America/Los_Angeles"
    # lat/lon untouched
    assert abs(got["latitude"] - 37.7749) < 1e-6


def test_update_timezone_creates_record_without_lat_lon(tmp_path, monkeypatch):
    """The device's `uo` telemetry field can populate the offset even
    when no geographic location is set. update_timezone must accept
    standalone TZ writes."""
    loc = _fresh_location(monkeypatch, tmp_path)
    assert loc.update_timezone(-25200, "America/Los_Angeles") is True
    # get() still returns None (no lat/lon to validate)
    assert loc.get() is None
    # but get_tz_offset() returns the saved offset
    assert loc.get_tz_offset() == -25200


def test_get_tz_offset_works_with_full_record(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    loc.set(37.7749, -122.4194)
    loc.update_timezone(-25200, "America/Los_Angeles")
    assert loc.get_tz_offset() == -25200


def test_get_tz_offset_none_when_unset(tmp_path, monkeypatch):
    loc = _fresh_location(monkeypatch, tmp_path)
    assert loc.get_tz_offset() is None
    loc.set(37.7749, -122.4194)  # lat/lon only, no tz
    assert loc.get_tz_offset() is None
