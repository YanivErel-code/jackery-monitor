"""Startup decisions must use real, fresh, device-scoped battery inputs."""
from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest

NOW = 1_790_000_000


@pytest.fixture()
def monitor(isolated_data, monkeypatch):
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(isolated_data / "energy.db"))
    for name in ("crypto_util", "location", "smart_charge", "cost", "energy_db"):
        importlib.reload(importlib.import_module(name))
    import server
    mod = importlib.reload(server)
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    mod.state.device = mod.DeviceInfo("Test", "cloud", 0, 13, "MAIN", "portable")
    mod.state.last_status = {"battery_percent": 33, "soc_source_ts": NOW - 1,
                             "soc_source": "mqtt"}
    mod.state.battery_packs_by_sn["MAIN"] = [
        {"deviceSn": "PACK-" + str(i), "rb": soc}
        for i, soc in enumerate([24, 25, 43, 24, 50])
    ]
    mod.state.battery_pack_meta_by_sn["MAIN"] = {
        "fetched_at": NOW - 1, "source": "mqtt", "stale": False,
    }
    mod.state.energy.upsert_device("MAIN", "Test", 13, "5000 Plus")
    mod.smart_charge.set_config({"mode": "active", "kasa_device_host": "test-plug",
                                 "target_sunrise_soc_pct": 32, "max_charge_w": 1400},
                                device_sn="MAIN")
    monkeypatch.setattr(mod.device_location, "get", lambda: {
        "latitude": 1, "longitude": 2, "utc_offset_seconds": 0,
    })
    weather = {"hourly": [{"ts": NOW, "ghi_w_m2": 500, "cloud_cover_pct": 0}],
               "fetched_at": NOW - 30, "utc_offset_seconds": 0}
    monkeypatch.setattr(mod.weather_client, "fetch_irradiance", AsyncMock(return_value=weather))
    monkeypatch.setattr(mod.state.energy, "history", lambda *a, **kw: [])
    mod.forecast_calls = []

    def build(**kw):
        mod.forecast_calls.append(kw)
        sunrise = NOW // 3600 * 3600 + 8 * 3600
        return {"ready": True, "solar_coefficient": 4.2, "charge_efficiency": 0.9,
                "forecast": [
                    {"ts": sunrise, "duration_h": 1, "solar_w": 0,
                     "load_w": 550, "predicted_soc": 30},
                    {"ts": sunrise + 3600, "duration_h": 1, "solar_w": 200,
                     "load_w": 550, "predicted_soc": 31},
                ]}

    monkeypatch.setattr(mod.forecaster, "build_forecast", build)
    monkeypatch.setattr(mod, "_update_daily_summary", AsyncMock())
    mod.toggle = AsyncMock()
    monkeypatch.setattr(mod.kasa_client, "set_state", mod.toggle)
    return mod


async def test_startup_uses_saved_device_model_not_generic_capacity(monitor):
    monitor.state.device.model_code = None
    plan = await monitor._smart_charge_evaluate(record=False, device_sn="MAIN")
    assert plan is not None
    assert monitor.forecast_calls[0]["capacity_wh"] == 30240
    assert monitor.forecast_calls[0]["starting_soc_pct"] == pytest.approx(199 / 6)


@pytest.mark.parametrize("soc", [None, True, "bad", float("nan"), -1, 101])
async def test_invalid_main_soc_skips_forecast_and_plug(monitor, soc):
    monitor.state.last_status["battery_percent"] = soc
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert "SOC" in plan.reason
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_zero_main_soc_is_preserved(monitor):
    monitor.state.last_status["battery_percent"] = 0
    await monitor._smart_charge_evaluate(record=False, device_sn="MAIN")
    assert monitor.forecast_calls[0]["starting_soc_pct"] == pytest.approx(166 / 6)


@pytest.mark.parametrize("received", [None, NOW - 121, NOW + 60, float("nan")])
async def test_stale_or_unknown_main_soc_cannot_drive_plug(monitor, received):
    monitor.state.last_status["soc_source_ts"] = received
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_stale_pack_snapshot_skips_forecast_and_plug(monitor):
    monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"] = True
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_missing_pack_soc_does_not_count_as_empty_energy(monitor):
    monitor.state.battery_packs_by_sn["MAIN"][0]["rb"] = None
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert not monitor.forecast_calls


async def test_weather_wait_resolves_inputs_after_telemetry_arrives(monitor, monkeypatch):
    monitor.state.last_status = None
    monitor.state.device.model_code = None

    async def arrive(*a):
        monitor.state.last_status = {"battery_percent": 33, "soc_source_ts": NOW,
                                     "soc_source": "http"}
        return {"hourly": [], "fetched_at": NOW, "utc_offset_seconds": 0}

    monkeypatch.setattr(monitor.weather_client, "fetch_irradiance", arrive)
    await monitor._smart_charge_evaluate(record=False, device_sn="MAIN")
    assert monitor.forecast_calls[0]["starting_soc_pct"] == pytest.approx(199 / 6)
    assert monitor.forecast_calls[0]["capacity_wh"] == 30240


async def test_unrecognized_model_does_not_use_another_device(monitor):
    monitor.state.device.model_code = None
    monitor.state.energy.upsert_device("MAIN", "Test", 999, "Unknown")
    monitor.state.energy.upsert_device("OTHER", "Other", 13, "5000 Plus")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert not monitor.forecast_calls


async def test_decision_keeps_exact_inputs_and_test_mode_does_not_toggle(monitor):
    monitor.smart_charge.set_config({"mode": "test"}, device_sn="MAIN")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", plan.decided_at)
    assert snapshot["main_soc_pct"] == 33
    assert snapshot["capacity_wh"] == 30240
    assert snapshot["model_code"] == 13
    assert snapshot["pack_count"] == 5
    assert snapshot["main_soc_source"] == "mqtt"
    assert snapshot["weather"]["fetched_at"] == NOW - 30
    assert snapshot["weather"]["hourly"][0]["ghi_w_m2"] == 500
    assert snapshot["model"]["solar_coefficient"] == 4.2
    assert snapshot["baseline_forecast"][0]["predicted_soc"] == 30
    serialized = json.dumps(snapshot)
    assert "test-plug" not in serialized
    assert "latitude" not in serialized
    assert "longitude" not in serialized
    monitor.state.last_status["battery_percent"] = 99
    assert snapshot["main_soc_pct"] == 33
    monitor.toggle.assert_not_awaited()


async def test_disabling_during_weather_wait_prevents_old_plan(monitor, monkeypatch):
    async def disable(*a):
        monitor.smart_charge.set_config({"mode": "off"}, device_sn="MAIN")
        return {"hourly": [], "fetched_at": NOW}

    monkeypatch.setattr(monitor.weather_client, "fetch_irradiance", disable)
    assert await monitor._smart_charge_evaluate(device_sn="MAIN") is None
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_inactive_device_uses_own_soc_model_and_packs(monitor):
    monitor.state.last_cloud_meta = {
        "devices": [{"device_sn": "OTHER", "model_code": 19}],
        "devices_telemetry": {"OTHER": {"telemetry": {
            "battery_percent": 0, "soc_source_ts": NOW - 5, "soc_source": "http"}}},
    }
    monitor.state.battery_pack_meta_by_sn["OTHER"] = {
        "fetched_at": NOW - 5, "source": "http", "stale": False,
    }
    monitor.smart_charge.set_config({"mode": "test"}, device_sn="OTHER")
    await monitor._smart_charge_evaluate(record=False, device_sn="OTHER")
    assert monitor.forecast_calls[0]["starting_soc_pct"] == 0
    assert monitor.forecast_calls[0]["pack_count"] == 0
    assert monitor.forecast_calls[0]["capacity_wh"] == monitor.forecaster.battery_capacity_wh(19)


async def test_missing_pack_state_is_not_assumed_to_be_single_unit(monitor):
    monitor.state.battery_packs_by_sn.clear()
    monitor.state.battery_pack_meta_by_sn.clear()
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_details_keep_tick_trace_when_hourly_forecast_is_overwritten(monitor):
    monitor.smart_charge.set_config({"mode": "test"}, device_sn="MAIN")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    monitor.state.energy.record_forecast("MAIN", NOW, [
        {"ts": plan.sunrise_ts, "predicted_soc": 99},
    ])
    details = monitor.api_smart_charge_decision_details(plan.decided_at, "MAIN")
    assert details["inputs_saved"] is True
    assert details["forecast_trace"][0]["predicted_soc"] == 30
    assert details["input_snapshot"]["capacity_wh"] == 30240
    assert details["weather"][0]["ghi_w_m2"] == 500
