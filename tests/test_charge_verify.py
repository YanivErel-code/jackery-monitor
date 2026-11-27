"""Commanded smart-charge ON must be verified against measured AC input."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import charge_verify
from tests.test_smart_charge_inputs import NOW, monitor

ON = 1_795_043_363  # 2026-11-18 23:09:23Z, first ON of the failed interval


def bucket(ts, ac_w, *, fresh=True):
    return {"bucket": ts // 60 * 60, "ac_input_wh": ac_w / 60, "last_ac_input_w": ac_w,
            "last_source_ts": ts // 60 * 60 + 55 if fresh else ts - 3600}


def minutes(start, count, ac_w, **kw):
    return [bucket(start + 60 * i, ac_w, **kw) for i in range(count)]


def decision(ts, action, executed=1, mode="active"):
    return {"decided_at": ts, "action": action, "executed": executed, "mode": mode}


def test_on_run_starts_at_first_executed_on_after_last_off():
    rows = [decision(ON - 900, "on"), decision(ON - 120, "off"), decision(ON, "on"),
            decision(ON + 303, "on"), decision(ON + 606, "on"), decision(ON + 650, "on", executed=0)]
    assert charge_verify.commanded_on_since(rows) == ON
    assert charge_verify.commanded_on_since([*rows, decision(ON + 901, "off")]) is None
    assert charge_verify.commanded_on_since([decision(ON, "on", mode="test")]) is None


def test_nov18_failure_is_flagged_on_the_next_tick():
    # 23:09-23:24Z: three executed ONs, 0 W AC input in every bucket, fresh telemetry.
    verdict = charge_verify.assess(on_since=ON, now=ON + 303, buckets=minutes(ON, 6, 0),
                                   soc_pct=24.5)
    assert verdict["status"] == "not_charging"
    assert verdict["minutes_without_ac"] == pytest.approx(5.0, abs=0.1)
    assert "0 W AC input" in charge_verify.warning_text(verdict, plug_is_on=True)
    assert "reports OFF" in charge_verify.warning_text(verdict, plug_is_on=False)


def test_working_interval_with_one_minute_start_latency_is_charging():
    # 17:01Z on Nov 18: first AC bucket one minute after ON at ~980 W.
    buckets = [bucket(ON, 0), *minutes(ON + 60, 5, 980)]
    assert charge_verify.assess(on_since=ON, now=ON + 303, buckets=buckets)["status"] == "charging"


def test_grace_period_stale_telemetry_and_full_battery_do_not_alarm():
    assert charge_verify.assess(on_since=ON, now=ON + 90,
                                buckets=minutes(ON, 2, 0))["status"] == "pending"
    assert charge_verify.assess(on_since=ON, now=ON + 303,
                                buckets=minutes(ON, 6, 0, fresh=False))["status"] == "telemetry_unavailable"
    assert charge_verify.assess(on_since=ON, now=ON + 303, buckets=minutes(ON, 6, 0),
                                soc_pct=100)["status"] == "full"
    assert charge_verify.assess(on_since=None, now=ON, buckets=[])["status"] == "idle"


def test_charging_that_stops_mid_interval_is_flagged():
    buckets = [*minutes(ON, 10, 950), *minutes(ON + 600, 12, 0)]
    verdict = charge_verify.assess(on_since=ON, now=ON + 1320, buckets=buckets)
    assert verdict["status"] == "not_charging"
    assert verdict["last_ac_ts"] == (ON + 540) // 60 * 60


def _seed_failed_on(monitor):
    db = monitor.state.energy
    db.record_smart_charge_decision("MAIN", {"decided_at": NOW - 600, "mode": "active",
                                             "action": "on"}, executed=True)
    with db._conn() as connection:
        for row in minutes(NOW - 600, 10, 0):
            connection.execute(
                "INSERT INTO samples(device_sn,bucket,ac_input_wh,last_ac_input_w,last_source_ts,"
                "sample_count) VALUES (?,?,?,?,?,30)",
                ("MAIN", row["bucket"], row["ac_input_wh"], row["last_ac_input_w"],
                 row["last_source_ts"]))


async def test_evaluate_flags_commanded_but_not_charging(monitor, monkeypatch):
    _seed_failed_on(monitor)
    status = AsyncMock(return_value={"is_on": True})
    monkeypatch.setattr(monitor.kasa_client, "status", status)
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kw: monitor.smart_charge.Plan(
        action="on", reason="charging now", mode="active", decided_at=NOW))
    monitor.toggle.return_value = {"is_on": True}
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    assert plan.charge_verification["status"] == "not_charging"
    assert plan.charge_verification["plug_is_on"] is True
    assert any("0 W AC input" in w for w in plan.warnings)
    status.assert_awaited_once_with("test-plug")          # read-only probe, no toggle
    monitor.toggle.assert_awaited_once_with("test-plug", True)
    snapshot = monitor.state.energy.smart_charge_decision_inputs("MAIN", NOW)
    assert snapshot["charge_verification"]["status"] == "not_charging"
    assert snapshot["actuation"]["reported_is_on"] is True


async def test_plug_read_back_contradicting_on_is_not_executed(monitor, monkeypatch):
    monkeypatch.setattr(monitor.smart_charge, "compute_plan", lambda **kw: monitor.smart_charge.Plan(
        action="on", reason="charging now", mode="active", decided_at=NOW))
    monitor.toggle.return_value = {"is_on": False}
    plan = await monitor._smart_charge_evaluate(device_sn="MAIN")
    latest = monitor.state.energy.list_smart_charge_decisions("MAIN", limit=1)[0]
    assert not latest["executed"]
    assert any("did not confirm ON" in w for w in plan.warnings)


def with_solar(rows, solar_w):
    return [{**row, "last_solar_w": solar_w} for row in rows]


def test_weak_dawn_light_waits_instead_of_alarming():
    # 2026-11-26 07:06: plug on, 20 W of solar, AC input began 17 minutes later.
    rows = with_solar(minutes(ON, 16, 0), 20)
    verdict = charge_verify.assess(on_since=ON, now=ON + 15 * 60, buckets=rows, soc_pct=52.8)
    assert (verdict["status"], verdict["waiting_on"]) == ("pending", "weak_sunlight")


@pytest.mark.parametrize("solar_w", [0, 400])
def test_darkness_or_real_sun_still_alarm_after_the_normal_grace(solar_w):
    rows = with_solar(minutes(ON, 6, 0), solar_w)
    verdict = charge_verify.assess(on_since=ON, now=ON + 303, buckets=rows, soc_pct=52.8)
    assert verdict["status"] == "not_charging"


def test_weak_light_allowance_runs_out():
    count = charge_verify.WEAK_SOLAR_GRACE_S // 60 + 2
    rows = with_solar(minutes(ON, count, 0), 20)
    verdict = charge_verify.assess(on_since=ON, now=ON + count * 60, buckets=rows, soc_pct=52.8)
    assert verdict["status"] == "not_charging"
