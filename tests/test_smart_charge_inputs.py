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


async def test_lg_hold_overrides_charge_plan_and_records_off_decision(monitor, monkeypatch):
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kw: monitor.smart_charge.Plan(
        action="on", reason="forecast requires charging", mode="active", decided_at=NOW))
    monkeypatch.setattr(monitor.lg_thinq, "block_reason", lambda config: "LG 240 V interlock: Oven active")
    recorded = []
    monkeypatch.setattr(monitor.state.energy, "record_smart_charge_decision", lambda **kw: recorded.append(kw))
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "off"
    assert "Oven active" in plan.reason
    monitor.toggle.assert_awaited_once_with("test-plug", False)
    assert recorded[0]["plan"]["action"] == "off"
    assert recorded[0]["executed"] is True


@pytest.mark.parametrize("body", [{"lg_interlock_enabled": "false"}, {"lg_manual_hold": 1},
                                   {"lg_device_ids": "oven"}, {"lg_device_ids": ["../oven"]},
                                   {"lg_device_ids": ["oven"] * 33},
                                   {"lg_interlock_enabled": True, "lg_device_ids": ["oven"]}])
async def test_lg_configuration_rejects_invalid_input(monitor, body):
    from fastapi import HTTPException, Request
    receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()})
    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        await monitor.api_smart_charge_set(request, device_sn="MAIN")
    assert error.value.status_code == 400
    assert not monitor.smart_charge.get_config("MAIN")["lg_interlock_enabled"]


async def test_lg_protection_cannot_enable_controls_on_monitor_only_home(monitor):
    from fastapi import HTTPException, Request
    monitor.state.last_cloud_meta = {"devices": [{"device_sn": "MAIN", "api_family": "home"}]}
    body = {"mode": "off", "lg_interlock_enabled": True, "lg_device_ids": ["oven"]}
    receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()})
    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as error:
        await monitor.api_smart_charge_set(request, device_sn="MAIN")
    assert error.value.status_code == 501
    assert not monitor.smart_charge.get_config("MAIN")["lg_interlock_enabled"]


async def test_lg_finish_time_reaches_scheduler_soc_estimate_and_saved_decision(monitor, monkeypatch):
    context = {"expected_finish_ts": NOW + 1800, "charge_not_before_ts": NOW + 2100,
               "timing_uncertain": False, "blocked_now": True}
    monkeypatch.setattr(monitor.lg_thinq, "planning_context", lambda *args, **kw: dict(context))
    monkeypatch.setattr(monitor.lg_thinq, "block_reason", lambda config: "LG appliance still running")
    received = []
    compute = monitor.smart_charge.compute_plan

    def capture(**kwargs):
        received.append(kwargs)
        return compute(**kwargs)

    monkeypatch.setattr(monitor.smart_charge, "compute_plan", capture)
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert received[0]["charge_not_before_ts"] == NOW + 2100
    assert received[0]["charging_unavailable"] is False
    assert plan.lg_protection["soc_at_finish_pct"] is not None
    assert plan.action == "off"
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", plan.decided_at)
    assert snapshot["lg_protection"]["expected_finish_ts"] == NOW + 1800
    monitor.toggle.assert_awaited_once_with("test-plug", False)


async def test_lg_change_wakes_smart_charger_without_waiting_five_minutes(monitor, monkeypatch):
    import asyncio
    called = asyncio.Event()
    evaluations = []

    async def evaluate(**kwargs):
        evaluations.append(kwargs)
        called.set()

    monkeypatch.setattr(monitor, "_smart_charge_evaluate", evaluate)
    task = asyncio.create_task(monitor.smart_charge_loop())
    try:
        await asyncio.wait_for(called.wait(), timeout=1)
        called.clear()
        monitor.state.smart_charge_wake.set()
        await asyncio.wait_for(called.wait(), timeout=1)
        assert len(evaluations) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_pre_charge_recheck_rejection_records_off_and_wakes_fresh_plan(monitor, monkeypatch):
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kwargs: monitor.smart_charge.Plan(
        action="on", reason="old plan", mode="active", decided_at=NOW, planned_hours=[NOW // 3600 * 3600]))
    monitor.toggle.side_effect = monitor.lg_thinq.InterlockBlocked("LG appliance extended its cycle")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "off"
    assert plan.planned_hours == []
    assert monitor.state.smart_charge_wake.is_set()
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", plan.decided_at)
    assert snapshot["planned_hours"] == []
    decision = monitor.state.energy.smart_charge_decision("MAIN", plan.decided_at)
    assert decision["action"] == "off"
    assert not decision["executed"]


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


async def test_frozen_measurements_skip_forecast_and_charging_despite_fresh_receipts(monitor):
    monitor.state.last_status["measurement_suspect"] = True
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    assert "frozen" in plan.reason.lower()
    assert not monitor.forecast_calls
    monitor.toggle.assert_not_awaited()


async def test_frozen_measurements_do_not_produce_a_new_ready_forecast(monitor):
    monitor.state.last_status["measurement_suspect"] = True
    result = await monitor._build_and_record_forecast("MAIN")
    assert result["ready"] is False
    assert "frozen" in result["error"].lower()
    assert not monitor.forecast_calls


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
