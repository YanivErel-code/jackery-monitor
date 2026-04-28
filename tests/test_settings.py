"""Unit tests for the runtime-settings module."""

from __future__ import annotations

import importlib


def test_default_values_when_no_file(isolated_data):
    import settings
    importlib.reload(settings)
    assert settings.get("poll_interval_s") == 2
    assert settings.get("low_battery_threshold") == 20


def test_env_var_supplies_default(isolated_data, monkeypatch):
    import settings
    monkeypatch.setenv("POLL_INTERVAL_S", "30")
    importlib.reload(settings)
    assert settings.get("poll_interval_s") == 30


def test_update_persists_and_round_trips(isolated_data):
    import settings
    importlib.reload(settings)
    settings.update({"poll_interval_s": 7, "low_battery_threshold": 15})
    # Cache should reflect immediately
    assert settings.get("poll_interval_s") == 7
    assert settings.get("low_battery_threshold") == 15
    # And after a re-import (simulating a restart), still there.
    importlib.reload(settings)
    assert settings.get("poll_interval_s") == 7
    assert settings.get("low_battery_threshold") == 15


def test_update_clamps_to_min(isolated_data):
    import settings
    importlib.reload(settings)
    settings.update({"poll_interval_s": -100})
    assert settings.get("poll_interval_s") == settings.SCHEMA["poll_interval_s"]["min"]


def test_update_clamps_to_max(isolated_data):
    import settings
    importlib.reload(settings)
    settings.update({"low_battery_threshold": 9999})
    assert settings.get("low_battery_threshold") == settings.SCHEMA["low_battery_threshold"]["max"]


def test_update_ignores_unknown_keys(isolated_data):
    import settings
    importlib.reload(settings)
    new = settings.update({"this_key_does_not_exist": 42})
    assert "this_key_does_not_exist" not in new


def test_update_ignores_non_int_values(isolated_data):
    import settings
    importlib.reload(settings)
    before = settings.get("poll_interval_s")
    settings.update({"poll_interval_s": "not a number"})
    assert settings.get("poll_interval_s") == before


def test_get_unknown_key_raises(isolated_data):
    import pytest

    import settings
    importlib.reload(settings)
    with pytest.raises(KeyError):
        settings.get("nonexistent_setting")


def test_schema_includes_all_keys(isolated_data):
    import settings
    importlib.reload(settings)
    schema = settings.schema()
    keys = {entry["key"] for entry in schema}
    assert "poll_interval_s" in keys
    assert "cloud_poll_interval_s" in keys
    assert "session_contested_cooldown_s" in keys
    assert "low_battery_threshold" in keys
    # Each entry has the fields the UI expects.
    for entry in schema:
        for field in ("key", "label", "hint", "min", "max", "value"):
            assert field in entry, f"missing {field} in schema entry for {entry.get('key')}"
