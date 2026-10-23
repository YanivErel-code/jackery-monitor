from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import forecaster
import smart_charge

BASE = int(datetime(2026, 10, 22, 22, tzinfo=timezone.utc).timestamp())


def forecast(*, soc=16, capacity=30240, hours=16, load=800, solar=None, now=BASE, efficiency=0.87):
    points = []
    first_end = now // 3600 * 3600 + 3600
    for index in range(hours):
        end = first_end + index * 3600
        points.append(
            {
                "ts": end,
                "duration_h": (end - max(now, end - 3600)) / 3600,
                "solar_w": (solar or {}).get(index, 0),
                "load_w": load,
            }
        )
    points.append(
        {"ts": first_end + hours * 3600, "duration_h": 1, "solar_w": load + 500, "load_w": load}
    )
    return {
        "ready": True,
        "charge_efficiency": efficiency,
        "forecast": forecaster.simulate_soc(soc, capacity, points, charge_efficiency=efficiency),
    }


def plan_for(
    data,
    *,
    soc=16,
    capacity=30240,
    now=BASE,
    power=1100,
    reserve=10,
    minutes=1440,
    target=30,
    **kwargs,
):
    return smart_charge.compute_plan(
        config={
            "mode": "active",
            "target_sunrise_soc_pct": target,
            "reserve_soc_pct": reserve,
            "max_charge_w": power,
            "max_on_duration_minutes": minutes,
        },
        current_soc_pct=soc,
        capacity_wh=capacity,
        forecast=data,
        baseline_forecast=data,
        cost_plan={
            "type": "tou",
            "tou_rates": [
                {"start_hour": 0, "end_hour": 15, "rate": 0.25},
                {"start_hour": 15, "end_hour": 24, "rate": 0.43},
            ],
        },
        tz_offset_seconds=-7 * 3600,
        now_ts=now,
        **kwargs,
    )


def test_clipped_zero_does_not_hide_remaining_night_energy():
    data = forecast(solar={0: 390, 1: 400, 2: 92})
    plan = plan_for(data)
    assert plan.action == "on"
    assert plan.deficit_kwh > 9.072
    assert plan.scheduled_sunrise_soc_pct >= 30
    assert plan.scheduled_min_soc_pct >= 10
    assert plan.plan_feasible


def test_reserve_requires_early_hour_even_when_later_hours_are_cheaper():
    """Starting exactly as SOC reaches the reserve is still a valid boundary."""
    data = forecast(soc=40, capacity=10000, hours=10, load=1000)
    plan = plan_for(data, soc=40, capacity=10000, power=3000, target=20)
    assert plan.window_start <= BASE + 3 * 3600
    assert plan.scheduled_min_soc_pct >= 10
    assert plan.scheduled_sunrise_soc_pct >= 20


def test_current_below_reserve_charges_now_instead_of_waiting_for_tariff():
    data = forecast(soc=5, capacity=10000, hours=10, load=200)
    plan = plan_for(data, soc=5, capacity=10000, power=1500, target=20)
    assert plan.action == "on"
    assert plan.planned_intervals[0]["start"] == BASE


def test_duration_limit_reports_unachievable_target_without_expanding_budget():
    data = forecast()
    plan = plan_for(data, minutes=60)
    seconds = sum(interval["end"] - interval["start"] for interval in plan.planned_intervals)
    assert seconds <= 3600
    assert not plan.plan_feasible
    assert plan.warnings
    assert plan.scheduled_sunrise_soc_pct < 30


def test_short_current_hour_is_not_counted_as_full_hour():
    now = BASE + 45 * 60
    data = forecast(soc=10, capacity=10000, hours=2, load=500, now=now)
    plan = plan_for(data, soc=10, capacity=10000, now=now, power=1000, target=30)
    assert not plan.plan_feasible
    assert plan.scheduled_sunrise_soc_pct < 20
    assert all(interval["start"] >= now for interval in plan.planned_intervals)


def test_half_hour_budget_uses_only_half_an_hour():
    data = forecast(soc=5, capacity=10000, hours=4, load=200)
    plan = plan_for(data, soc=5, capacity=10000, minutes=30, power=1500, target=20)
    assert plan.action == "on"
    assert sum(interval["end"] - interval["start"] for interval in plan.planned_intervals) <= 1800


def test_lg_finish_never_overridden_to_recover_reserve():
    data = forecast(soc=5, capacity=10000, hours=10, load=200)
    after = BASE + 2 * 3600 + 1800
    plan = plan_for(data, soc=5, capacity=10000, power=1500, charge_not_before_ts=after)
    assert plan.action == "off"
    assert all(interval["start"] >= after for interval in plan.planned_intervals)
    assert plan.warnings


def test_manual_hold_cannot_be_overridden_by_reserve_or_target():
    plan = plan_for(forecast(), charging_unavailable=True)
    assert plan.action == "off"
    assert not plan.planned_intervals
    assert not plan.plan_feasible
    assert plan.warnings


def test_remaining_budget_is_not_reset_on_each_replan():
    data = forecast()
    plan = plan_for(data, minutes=60, elapsed_on_seconds=3500)
    assert sum(interval["end"] - interval["start"] for interval in plan.planned_intervals) <= 100


def test_config_validates_and_preserves_reserve_on_partial_lg_update(tmp_path, monkeypatch):
    monkeypatch.setattr(smart_charge, "CONFIG_PATH", str(tmp_path / "smart.json"))
    saved = smart_charge.set_config({"mode": "test", "reserve_soc_pct": 15}, device_sn="MAIN")
    assert saved["reserve_soc_pct"] == 15
    saved = smart_charge.set_config({"lg_manual_hold": True}, device_sn="MAIN")
    assert saved["reserve_soc_pct"] == 15


@pytest.mark.parametrize("invalid", [-1, 101, float("nan"), float("inf"), True, "bad"])
def test_invalid_reserve_uses_safe_default(invalid):
    assert smart_charge._validate_config({"reserve_soc_pct": invalid})["reserve_soc_pct"] == 10


def test_commanded_duration_survives_repeated_on_decisions_and_restarts():
    decisions = [
        {"decided_at": BASE - 100, "mode": "active", "executed": True, "action": "on"},
        {"decided_at": BASE + 100, "mode": "active", "executed": True, "action": "on"},
        {"decided_at": BASE + 200, "mode": "test", "executed": False, "action": "off"},
        {"decided_at": BASE + 300, "mode": "active", "executed": True, "action": "off"},
        {"decided_at": BASE + 400, "mode": "active", "executed": True, "action": "on"},
    ]
    assert smart_charge.commanded_on_seconds(decisions, since=BASE, until=BASE + 500) == 400


def test_duration_counter_ignores_failed_commands_and_future_records():
    decisions = [
        {"decided_at": BASE, "mode": "active", "executed": False, "action": "on"},
        {"decided_at": BASE + 1000, "mode": "active", "executed": True, "action": "on"},
    ]
    assert smart_charge.commanded_on_seconds(decisions, since=BASE, until=BASE + 500) == 0


def test_partial_interval_or_budget_boundary_wakes_before_five_minutes():
    interval = smart_charge.Plan(
        "on",
        "charging",
        "active",
        BASE,
        planned_intervals=[{"start": BASE, "end": BASE + 90}],
        remaining_charge_seconds=90,
    )
    assert smart_charge.next_check_delay([interval], now=BASE) == 90
    extension = smart_charge.Plan("on", "extension", "active", BASE, remaining_charge_seconds=30)
    assert smart_charge.next_check_delay([extension], now=BASE) == 30
    assert smart_charge.next_check_delay([interval], now=BASE + 100) == 0.1


def test_oct22_saved_incident_no_longer_defers_into_depletion():
    """Full-precision plans may wait while above reserve, but not past its first breach."""
    data = json.loads((Path(__file__).parent / "fixtures" / "smart_charge_oct22.json").read_text())
    sunrise = smart_charge._find_next_sunrise(data["forecast"]["forecast"])
    elapsed = smart_charge.commanded_on_seconds(
        data["command_history"], since=sunrise - 86400, until=data["now_ts"]
    )
    kwargs = {
        key: data[key]
        for key in (
            "config",
            "current_soc_pct",
            "capacity_wh",
            "now_ts",
            "tz_offset_seconds",
            "cost_plan",
            "forecast",
        )
    }
    result = smart_charge.compute_plan(**kwargs, elapsed_on_seconds=elapsed)
    legacy = smart_charge._legacy_compute_plan(**kwargs)
    assert legacy.action == "off"
    assert legacy.window_start == data["original_window_start"]
    assert data["original_action"] == "off"
    first_reserve_breach = min(hour["ts"] for hour in data["forecast"]["forecast"]
                               if hour["predicted_soc"] < result.reserve_soc_pct)
    assert result.window_start < first_reserve_breach
    assert result.window_start < data["original_window_start"]
    assert result.deficit_kwh > 9.072
    assert not result.plan_feasible
    assert result.warnings
    used = sum(interval["end"] - interval["start"] for interval in result.planned_intervals)
    assert used + elapsed <= data["config"]["max_on_duration_minutes"] * 60


def test_daytime_reserve_dip_is_not_hidden_by_recovered_sunrise_endpoint():
    data = forecast(
        soc=15, capacity=10000, hours=6, load=600, solar={0: 100, 1: 100, 2: 3000, 3: 3000}
    )
    assert data["forecast"][-2]["predicted_soc"] >= 30
    plan = plan_for(data, soc=15, capacity=10000, power=1500)
    assert plan.planned_intervals
    assert plan.scheduled_min_soc_pct >= 10
    assert plan.scheduled_sunrise_soc_pct >= 30


@pytest.mark.parametrize(
    "power,capacity,soc,load",
    [
        (power, capacity, soc, load)
        for power in (800, 1600)
        for capacity in (5040, 30240)
        for soc in (12, 30, 60)
        for load in (200, 700)
    ],
)
def test_reported_feasible_paths_pass_independent_energy_balance(power, capacity, soc, load):
    data = forecast(soc=soc, capacity=capacity, hours=12, load=load)
    plan = plan_for(data, soc=soc, capacity=capacity, power=power, target=30)
    assert sum(item["end"] - item["start"] for item in plan.planned_intervals) <= 86400
    energy = soc * capacity / 100
    minimum = soc
    for row in data["forecast"][:-1]:
        start = row["ts"] - row["duration_h"] * 3600
        boundaries = sorted(
            {
                start,
                row["ts"],
                *(
                    value
                    for item in plan.planned_intervals
                    for value in (item["start"], item["end"])
                    if start < value < row["ts"]
                ),
            }
        )
        for index in range(len(boundaries) - 1):
            begin, end = boundaries[index : index + 2]
            grid = (
                power
                if any(item["start"] <= begin < item["end"] for item in plan.planned_intervals)
                else 0
            )
            net = row["solar_w"] + grid - row["load_w"]
            energy = min(
                capacity, max(0, energy + net * (0.87 if net > 0 else 1) * (end - begin) / 3600)
            )
            minimum = min(minimum, energy / capacity * 100)
    if plan.plan_feasible:
        assert minimum >= 9.9
        assert energy / capacity * 100 >= 29.9


@pytest.mark.parametrize(
    "mutation", ["gap", "overlap", "negative_load", "nan_solar", "nan_soc", "nan_efficiency"]
)
def test_invalid_interval_forecasts_cannot_authorize_charging(mutation):
    data = forecast()
    if mutation == "gap":
        del data["forecast"][1]
    elif mutation == "overlap":
        data["forecast"][1]["ts"] -= 30
    elif mutation == "negative_load":
        data["forecast"][0]["load_w"] = -1
    elif mutation == "nan_solar":
        data["forecast"][0]["solar_w"] = float("nan")
    elif mutation == "nan_soc":
        data["forecast"][-2]["predicted_soc"] = float("nan")
    else:
        data["charge_efficiency"] = float("nan")
    plan = plan_for(data)
    assert plan.action == "skip"
    assert not plan.plan_feasible
    assert not plan.trajectory_validated


def test_charge_ceiling_below_target_is_reported_not_assumed_reachable():
    data = forecast(soc=15, capacity=10000, hours=8, load=200)
    data["charge_ceiling_pct"] = 20
    plan = plan_for(data, soc=15, capacity=10000, power=3000)
    assert not plan.plan_feasible
    assert plan.scheduled_sunrise_soc_pct <= 20
    assert plan.warnings
