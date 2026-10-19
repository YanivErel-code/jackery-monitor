from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest


def detector():
    from frozen_telemetry import FrozenTelemetryDetector

    return FrozenTelemetryDetector()


def observe_for(tracker, seconds=4200, **overrides):
    arguments = {
        "signature": (88, 0, 551, 69, 83, 73, 67, 74),
        "discharge_w": 551,
        "capacity_wh": 30240,
    }
    arguments.update(overrides)
    for elapsed in range(0, seconds + 1, 60):
        tracker.observe("main", now=10000 + elapsed, **arguments)


def test_repeated_http_receipts_cannot_hide_frozen_discharging_measurements():
    tracker = detector()
    observe_for(tracker)
    health = tracker.health("main", now=14200)
    assert health["suspect"]
    assert health["unchanged_s"] == 4200
    assert health["expected_discharge_wh"] > 604.8
    assert tracker.recovery_due(14200) == ["main"]


@pytest.mark.parametrize("discharge_w", [0, -100, 10])
def test_idle_charging_and_sub_quantization_loads_do_not_trigger(discharge_w):
    tracker = detector()
    observe_for(tracker, discharge_w=discharge_w)
    assert not tracker.health("main", now=14200)["suspect"]


def test_real_pack_progress_resets_the_evidence_and_recovery_backoff():
    tracker = detector()
    observe_for(tracker)
    tracker.mark_recovery(["main"], now=14200)
    tracker.observe("main", signature=(88, 0, 551, 68, 83, 73, 67, 74),
                    discharge_w=551, capacity_wh=30240, now=14260)
    assert not tracker.health("main", now=14260)["suspect"]
    assert tracker.health("main", now=14260)["recovery_attempts"] == 0


def test_relogin_is_rate_limited_and_does_not_claim_measurements_recovered():
    tracker = detector()
    observe_for(tracker)
    tracker.mark_recovery(["main"], now=14200)
    assert tracker.health("main", now=14200)["suspect"]
    assert tracker.recovery_due(14201) == []
    assert tracker.recovery_due(16000) == ["main"]
    tracker.mark_recovery(["main"], now=16000)
    assert tracker.recovery_due(17800) == []
    assert tracker.recovery_due(19600) == ["main"]


def test_healthy_secondary_device_cannot_mask_the_frozen_primary():
    tracker = detector()
    observe_for(tracker)
    tracker.observe("secondary", signature=(20, 0, 100), discharge_w=100,
                    capacity_wh=3024, now=14200)
    assert tracker.recovery_due(14200) == ["main"]
    assert not tracker.health("secondary", now=14200)["suspect"]


@pytest.mark.parametrize("now", [11000, 13000])
def test_clock_reversal_or_unobserved_gap_cannot_accumulate_energy(now):
    tracker = detector()
    observe_for(tracker, seconds=1800)
    tracker.observe("main", signature=(88, 0, 551, 69, 83, 73, 67, 74),
                    discharge_w=551, capacity_wh=30240, now=now)
    assert not tracker.health("main", now=now)["suspect"]


def test_failed_reconnect_gap_does_not_clear_confirmed_suspicion_or_backoff():
    tracker = detector()
    observe_for(tracker)
    tracker.mark_recovery(["main"], now=14200)
    tracker.observe("main", signature=(88, 0, 551, 69, 83, 73, 67, 74),
                    discharge_w=551, capacity_wh=30240, now=15000)
    assert tracker.health("main", now=15000)["suspect"]
    assert tracker.health("main", now=15000)["recovery_attempts"] == 1
    assert tracker.recovery_due(15000) == []


def test_invalid_reply_does_not_clear_confirmed_suspicion():
    tracker = detector()
    observe_for(tracker)
    tracker.observe("main", signature=(88, 0, 551), discharge_w=float("nan"),
                    capacity_wh=30240, now=14260)
    assert tracker.health("main", now=14260)["suspect"]


def test_real_transition_to_charging_clears_frozen_discharge_warning():
    tracker = detector()
    observe_for(tracker)
    tracker.observe("main", signature=(88, 600, 551, 69, 83, 73, 67, 74),
                    discharge_w=-49, capacity_wh=30240, now=14260)
    assert not tracker.health("main", now=14260)["suspect"]


@pytest.mark.parametrize("capacity_wh", [0, float("nan"), float("inf")])
def test_invalid_capacity_cannot_trigger_reconnect(capacity_wh):
    tracker = detector()
    observe_for(tracker, capacity_wh=capacity_wh)
    assert tracker.recovery_due(14200) == []


@pytest.fixture
def bridge(isolated_data):
    import bridge
    import crypto_util

    importlib.reload(crypto_util)
    importlib.reload(bridge)
    return bridge


def frozen_bridge(bridge, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(bridge.time, "time", lambda: clock[0])
    bridge.state.cloud_creds = {"email": "test@example.invalid", "password": "test"}
    bridge.state.cloud_devices = [{"device_sn": "main", "model_code": 22}]
    bridge.state.cloud_device = {"device_sn": "main", "model_code": 22}
    bridge.state.cloud_state = "connected"
    bridge.state.battery_packs_by_sn["main"] = [
        {"deviceSn": str(index), "rb": soc}
        for index, soc in enumerate((69, 83, 73, 67, 74))
    ]
    for elapsed in range(0, 4201, 60):
        clock[0] = 10000 + elapsed
        bridge.state.packs_ts_by_sn["main"] = clock[0]
        bridge._merge_cloud_properties("main", {"rb": 88, "ip": 0, "op": 551},
                                       source="http")
    bridge.state.cloud_telemetry = bridge.state.telemetry_by_sn["main"]
    bridge.state.cloud_ts = clock[0]
    return clock


def test_bridge_marks_cached_readings_suspect_despite_recent_http_success(bridge, monkeypatch):
    frozen_bridge(bridge, monkeypatch)
    snapshot = bridge.merged_poll()
    assert snapshot["cloud"]["state"] == "stale"
    assert snapshot["telemetry"]["measurement_suspect"]
    assert snapshot["cloud"]["devices_telemetry"]["main"]["telemetry"]["measurement_suspect"]
    assert snapshot["cloud"]["frozen_devices"] == ["main"]
    bridge._clear_device_caches("main")
    assert bridge.state.frozen_telemetry.recovery_due(14200) == []


@pytest.mark.asyncio
async def test_watchdog_reconnects_frozen_readings_without_output_commands(bridge, monkeypatch):
    frozen_bridge(bridge, monkeypatch)
    closed = []
    restarted = []

    async def close():
        closed.append(True)

    async def cloud_loop():
        restarted.append(True)

    sleeps = []

    async def sleep(_seconds):
        sleeps.append(True)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    bridge.state.cloud_client = SimpleNamespace(aclose=close)
    monkeypatch.setattr(bridge, "cloud_loop", cloud_loop)
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await bridge.cloud_watchdog_loop()
    await bridge.state.cloud_task
    assert closed == [True]
    assert restarted == [True]
    assert bridge.state.frozen_telemetry.health("main", 14200)["suspect"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", ["pause_until", "contested_until"])
async def test_frozen_recovery_honors_pause_and_session_contention(bridge, monkeypatch, deadline):
    frozen_bridge(bridge, monkeypatch)
    setattr(bridge.state, deadline, 14300)
    sleeps = []

    async def sleep(_seconds):
        sleeps.append(True)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await bridge.cloud_watchdog_loop()
    assert bridge.state.cloud_task is None
    assert bridge.state.frozen_telemetry.health("main", 14200)["recovery_attempts"] == 0
