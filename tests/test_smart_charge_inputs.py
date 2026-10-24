"""Startup decisions must use real, fresh, device-scoped battery inputs."""
from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest

NOW = 1_790_000_000


def test_learning_ignores_legacy_manual_charge_rate(monitor):
    monitor.state.energy.set_device_param("MAIN", "max_charge_w", 4000, source="user")
    resolved = monitor.resolve_device_param("MAIN", "max_charge_w")
    assert resolved["value"] == 1000
    assert resolved["source"] == "default"


def test_advisor_cannot_reintroduce_a_manual_charge_rate():
    import claude_advisor
    assert "smart_charge.max_charge_w" not in claude_advisor.ALLOWED_TARGETS


def test_advisor_uses_the_same_effective_charge_rate_as_the_planner(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "_learned_charge_rate", lambda device_sn: {"value": 945})
    assert monitor._advisor_helpers.smart_charge_config("MAIN")["max_charge_w"] == 945


async def test_effective_learned_rate_reaches_planner_and_saved_snapshot(monitor, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "_learned_charge_rate", lambda device_sn: {
        "value": 950, "source": "fit", "n_samples": 6, "updated_at": NOW,
        "last_sample_ts": NOW - 60, "note": "learned", "samples": []})

    def compute(**kwargs):
        calls.append(kwargs)
        return monitor.smart_charge.Plan(action="off", reason="test", mode="active", decided_at=NOW)

    monkeypatch.setattr(monitor.smart_charge, "compute_plan", compute)
    await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert calls[0]["config"]["max_charge_w"] == 950
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", NOW)
    assert snapshot["config"]["max_charge_w"] == 950
    assert snapshot["charge_rate"]["source"] == "fit"


def test_charge_rate_history_uses_completed_minutes_and_is_device_scoped(monitor, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor.state.energy, "history", lambda *args, **kwargs: calls.append((args, kwargs)) or [])
    monitor._learned_charge_rate("MAIN")
    monitor._learned_charge_rate("OTHER")
    assert [args[0] for args, _ in calls] == ["MAIN", "OTHER"]
    assert all(kwargs["bucket_s"] == 60 and kwargs["until_ts"] == NOW // 60 * 60
               for _, kwargs in calls)


async def test_charge_rate_override_api_is_read_only(monitor):
    from fastapi import HTTPException, Request
    body = {"device_sn": "MAIN", "key": "max_charge_w", "value": 1800}
    receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()})
    with pytest.raises(HTTPException) as error:
        await monitor.api_devices_params_set(Request({"type": "http", "method": "POST", "headers": []}, receive))
    assert error.value.status_code == 400


async def test_saving_reserve_ignores_old_browser_charge_power_and_exposes_effective_rate(monitor):
    from fastapi import Request
    body = {"mode": "active", "reserve_soc_pct": 17, "max_charge_w": 4000}
    receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()})
    response = await monitor.api_smart_charge_set(Request({"type": "http", "method": "POST", "headers": []}, receive), device_sn="MAIN")
    assert response["config"]["max_charge_w"] == 1000
    assert response["config"]["reserve_soc_pct"] == 17
    assert monitor.api_smart_charge_get("MAIN")["config"]["max_charge_w"] == 1000
    assert monitor.api_smart_charge_status("MAIN")["charge_rate"]["source"] == "default"
    assert monitor.state.smart_charge_wake.is_set()


def test_charge_rate_history_failure_uses_default_and_surfaces_explanation(monitor, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("history unavailable")
    monkeypatch.setattr(monitor.state.energy, "history", fail)
    rate = monitor._learned_charge_rate("MAIN")
    assert rate["value"] == 1000
    assert rate["source"] == "default"
    assert "unavailable" in rate["note"]


def test_charge_rate_cache_expires_and_refits_on_new_measurements(monitor, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor.state.energy, "history", lambda *args, **kwargs: calls.append(args) or [])
    monitor._learned_charge_rate("MAIN")
    monitor._learned_charge_rate("MAIN")
    assert len(calls) == 1
    monkeypatch.setattr(monitor.time, "time", lambda: NOW + 61)
    monitor._learned_charge_rate("MAIN")
    assert len(calls) == 2


async def test_legacy_unvalidated_forecast_never_authorizes_live_control(monitor, monkeypatch):
    sunrise = NOW // 3600 * 3600 + 8 * 3600
    monkeypatch.setattr(monitor.forecaster, "build_forecast", lambda **kwargs: {
        "ready": True, "forecast": [
            {"ts": sunrise - 3600, "solar_w": 0, "predicted_soc": 0},
            {"ts": sunrise, "solar_w": 300, "predicted_soc": 1}]})
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    monitor.toggle.assert_not_awaited()


def test_elapsed_charge_time_is_recovered_from_persisted_decisions(monitor):
    monitor.state.energy.record_smart_charge_decision("MAIN", {
        "decided_at": NOW - 1000, "mode": "active", "action": "on"}, executed=True)
    monitor.state.energy.record_smart_charge_decision("MAIN", {
        "decided_at": NOW - 500, "mode": "active", "action": "on"}, executed=True)
    assert monitor._smart_charge_elapsed_on_seconds("MAIN", since=NOW - 2000, until=NOW) == 1000


def committed_record(monitor, *, serial="MAIN", action="on", executed=True, updates=None):
    config = monitor.smart_charge.get_config("MAIN")
    snapshot = {"version": 1, "capacity_wh": 30240,
                "config": config, "schedule_projection": {
                    "trajectory_validated": True, "schedule_policy": "overnight_first_v1"},
                "planned_intervals": [{"start": NOW - 300, "end": NOW + 1800}]}
    snapshot.update(updates or {})
    monitor.state.energy.record_smart_charge_decision(serial, {
        "decided_at": NOW - 300, "mode": "active", "action": action,
        "sunrise_ts": NOW + 8 * 3600}, executed=executed, input_snapshot=snapshot)


def retained_until(monitor):
    return monitor._smart_charge_committed_until("MAIN", now=NOW, sunrise=NOW + 8 * 3600,
        config=monitor.smart_charge.get_config("MAIN"), capacity_wh=30240)


def test_recent_successful_on_recovers_its_committed_interval_after_restart(monitor):
    committed_record(monitor)
    assert retained_until(monitor) == NOW + 1800


@pytest.mark.parametrize("policy", [None, "price_first", "future_unsupported_policy"])
def test_commitment_from_another_policy_is_replanned_not_carried_forward(monitor, policy):
    committed_record(monitor, updates={"schedule_projection": {
        "trajectory_validated": True, "schedule_policy": policy}})
    assert retained_until(monitor) is None


@pytest.mark.parametrize("serial,action,executed", [
    ("OTHER", "on", True), ("MAIN", "off", True), ("MAIN", "on", False),
])
def test_other_device_off_and_failed_commands_do_not_authorize_retention(monitor, serial, action, executed):
    committed_record(monitor, serial=serial, action=action, executed=executed)
    assert retained_until(monitor) is None


@pytest.mark.parametrize("updates", [
    {"config": {}}, {"capacity_wh": 5040}, {"schedule_projection": {"trajectory_validated": False}},
    {"planned_intervals": [{"start": NOW + 10, "end": NOW + 1800}]},
    {"planned_intervals": [{"start": NOW - 300, "end": NOW - 1}]},
    {"planned_intervals": [{"start": NOW - 300, "end": float("inf")}]},
])
def test_unvalidated_or_inapplicable_previous_schedule_is_not_retained(monitor, updates):
    committed_record(monitor, updates=updates)
    assert retained_until(monitor) is None


def test_new_successful_off_cancels_previous_commitment(monitor):
    committed_record(monitor)
    monitor.state.energy.record_smart_charge_decision("MAIN", {
        "decided_at": NOW - 100, "mode": "active", "action": "off"}, executed=True)
    assert retained_until(monitor) is None


@pytest.mark.parametrize("change", [
    {"kasa_device_host": "different-plug"}, {"kasa_device_host": None}, {"target_sunrise_soc_pct": 25},
    {"reserve_soc_pct": 15}, {"max_on_duration_minutes": 90}, {"mode": "test"},
])
def test_user_configuration_changes_invalidate_old_commitment(monitor, change):
    committed_record(monitor)
    monitor.smart_charge.set_config({**monitor.smart_charge.get_config("MAIN"), **change}, device_sn="MAIN")
    assert retained_until(monitor) is None


def test_learned_power_refinement_does_not_discard_an_active_interval(monitor):
    committed_record(monitor)
    config = {**monitor.smart_charge.get_config("MAIN"), "max_charge_w": 977}
    assert monitor._smart_charge_committed_until("MAIN", now=NOW, sunrise=NOW + 8 * 3600,
        config=config, capacity_wh=30240) == NOW + 1800


@pytest.mark.parametrize("offset", [601, 3600])
def test_old_or_expired_decisions_do_not_hold_charging_indefinitely(monitor, offset):
    committed_record(monitor)
    assert monitor._smart_charge_committed_until("MAIN", now=NOW + offset,
        sunrise=NOW + 8 * 3600, config=monitor.smart_charge.get_config("MAIN"),
        capacity_wh=30240) is None


def test_different_sunrise_cycle_does_not_reuse_a_commitment(monitor):
    committed_record(monitor)
    assert monitor._smart_charge_committed_until("MAIN", now=NOW, sunrise=NOW + 9 * 3600,
        config=monitor.smart_charge.get_config("MAIN"), capacity_wh=30240) is None


async def test_retained_interval_reaches_live_planning_and_persisted_snapshot(monitor, monkeypatch):
    committed_record(monitor)
    monkeypatch.setattr(monitor.smart_charge, "_find_next_sunrise", lambda hours: NOW + 8 * 3600)
    calls = []

    def compute(**kwargs):
        calls.append(kwargs)
        return monitor.smart_charge.Plan(action="on", reason="continuing validated interval",
            mode="active", decided_at=NOW, sunrise_ts=NOW + 8 * 3600,
            trajectory_validated=True, planned_intervals=[{"start": NOW, "end": NOW + 1800}],
            retained_charge_until_ts=kwargs["committed_until_ts"], schedule_policy="overnight_first_v1")

    monkeypatch.setattr(monitor.smart_charge, "compute_plan", compute)
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert calls[0]["committed_until_ts"] == NOW + 1800
    assert plan.retained_charge_until_ts == NOW + 1800
    monitor.toggle.assert_awaited_once_with("test-plug", True)
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", NOW)
    assert snapshot["config"]["kasa_device_host"] == "test-plug"
    assert snapshot["schedule_projection"]["retained_charge_until_ts"] == NOW + 1800
    assert snapshot["schedule_projection"]["schedule_policy"] == "overnight_first_v1"
    assert snapshot["planned_intervals"][0]["end"] == NOW + 1800


async def test_lg_hold_still_immediately_cancels_a_retained_interval(monitor, monkeypatch):
    committed_record(monitor)
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kwargs: monitor.smart_charge.Plan(
        action="on", reason="continuing", mode="active", decided_at=NOW,
        retained_charge_until_ts=NOW + 1800))
    monkeypatch.setattr(monitor.lg_thinq, "block_reason", lambda config: "LG appliance is active")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "off"
    assert plan.retained_charge_until_ts is None
    monitor.toggle.assert_awaited_once_with("test-plug", False)


async def test_stale_main_soc_cannot_be_overridden_by_a_committed_interval(monitor):
    committed_record(monitor)
    monitor.state.last_status["soc_source_ts"] = NOW - 3600
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "skip"
    monitor.toggle.assert_not_awaited()


def test_malformed_previous_snapshot_falls_back_to_fresh_planning(monitor):
    committed_record(monitor)
    with monitor.state.energy._conn() as connection:
        connection.execute("UPDATE smart_charge_decisions SET input_snapshot_json=? WHERE device_sn=?",
                           ("{broken", "MAIN"))
    assert retained_until(monitor) is None


async def test_pre_command_lg_refresh_can_reject_a_retained_on_request(monitor, monkeypatch):
    committed_record(monitor)
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kwargs: monitor.smart_charge.Plan(
        action="on", reason="continuing", mode="active", decided_at=NOW,
        retained_charge_until_ts=NOW + 1800, planned_intervals=[{"start": NOW, "end": NOW + 1800}]))
    monitor.toggle.side_effect = monitor.lg_thinq.InterlockBlocked("LG cycle resumed")
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.action == "off"
    assert plan.planned_intervals == []
    assert plan.retained_charge_until_ts is None
    latest = monitor.state.energy.list_smart_charge_decisions("MAIN", limit=1)[0]
    assert not latest["executed"]


@pytest.mark.parametrize("value", [-1, 100, True, None, float("nan"), float("inf")])
async def test_invalid_reserve_request_is_rejected(monitor, value):
    from fastapi import HTTPException, Request
    body = {"reserve_soc_pct": value}
    receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()})
    with pytest.raises(HTTPException) as error:
        await monitor.api_smart_charge_set(Request({"type": "http", "method": "POST", "headers": []}, receive), device_sn="MAIN")
    assert error.value.status_code == 400


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
