from datetime import datetime, timezone

import pytest

from cost import rate_at
from smart_charge_schedule import build_schedule

NOW = int(datetime(2026, 10, 24, 4, tzinfo=timezone.utc).timestamp())
SUNRISE = NOW + 10 * 3600
TZ_OFFSET = -7 * 3600


def arguments(*, now=NOW, soc=40, load=200, target=30, power=1000, cheap_start=6, cheap_end=7):
    hours = []
    end = now // 3600 * 3600 + 3600
    while end <= SUNRISE:
        hours.append(
            {
                "ts": end,
                "duration_h": (end - max(now, end - 3600)) / 3600,
                "solar_w": 0,
                "load_w": load,
            }
        )
        end += 3600
    return {
        "forecast": {"forecast": hours, "charge_efficiency": 1},
        "current_soc_pct": soc,
        "capacity_wh": 10000,
        "now": now,
        "sunrise": SUNRISE,
        "target": target,
        "reserve": 10,
        "power_w": power,
        "budget_seconds": 12 * 3600,
        "cost_plan": {
            "type": "tou",
            "tou_rates": [
                {"start_hour": cheap_start, "end_hour": cheap_end, "rate": 0.25},
                {"start_hour": 0, "end_hour": 24, "rate": 0.41},
            ],
        },
        "tz_offset_seconds": TZ_OFFSET,
        "charge_not_before_ts": None,
        "unavailable": False,
        "margin_seconds": 3600,
    }


def test_cheaper_final_hour_beats_expensive_pre_margin_slot():
    result = build_schedule(**arguments())
    assert result["planned_intervals"] == [{"start": SUNRISE - 3600, "end": SUNRISE}]
    assert result["plan_feasible"]
    assert result["scheduled_min_soc_pct"] >= 10
    assert result["scheduled_sunrise_soc_pct"] >= 30


def test_equal_cost_now_prefers_midnight_over_the_early_finish_anchor():
    inputs = arguments()
    inputs["cost_plan"] = {"type": "flat", "rate_per_kwh": 0.25}
    result = build_schedule(**inputs)
    midnight = SUNRISE - 7 * 3600
    assert result["planned_intervals"] == [{"start": midnight, "end": midnight + 3600}]


def test_expensive_early_charging_still_protects_reserve():
    inputs = arguments(soc=15, load=1000, target=20, power=2000, cheap_start=0)
    result = build_schedule(**inputs)
    assert result["action"] == "on"
    assert any(
        rate_at(inputs["cost_plan"], interval["start"], TZ_OFFSET) > 0.25
        for interval in result["planned_intervals"]
    )
    assert result["scheduled_min_soc_pct"] >= 10
    assert result["scheduled_sunrise_soc_pct"] >= 20
    assert result["plan_feasible"]


def test_expensive_charging_remains_available_when_cheap_hours_cannot_meet_target():
    inputs = arguments(soc=30, target=98, cheap_start=0)
    result = build_schedule(**inputs)
    assert any(
        rate_at(inputs["cost_plan"], interval["start"], TZ_OFFSET) > 0.25
        for interval in result["planned_intervals"]
    )
    assert result["scheduled_sunrise_soc_pct"] >= 98
    assert result["plan_feasible"]


@pytest.mark.parametrize("constraint", ["hold", "exhausted_budget", "not_before_sunrise"])
def test_cheaper_final_hour_cannot_override_charging_constraints(constraint):
    inputs = arguments()
    if constraint == "hold":
        inputs["unavailable"] = True
    elif constraint == "exhausted_budget":
        inputs["budget_seconds"] = 0
    else:
        inputs["charge_not_before_ts"] = SUNRISE
    result = build_schedule(**inputs)
    assert result["action"] == "off"
    assert not result["planned_intervals"]
    assert result["warnings"]


def flat(**overrides):
    inputs = arguments(**overrides)
    inputs["cost_plan"] = {"type": "flat", "rate_per_kwh": 0.25}
    return inputs


def test_minimum_block_keeps_a_started_run_on_in_the_cheapest_tier():
    inputs = flat()
    assert build_schedule(**inputs)["action"] == "off"
    result = build_schedule(**inputs, min_block_until_ts=NOW + 1800)
    assert result["action"] == "on"
    assert result["planned_intervals"][0] == {"start": NOW, "end": NOW + 1800}
    assert result["retained_charge_until_ts"] == NOW + 1800
    assert result["scheduled_sunrise_soc_pct"] >= 30
    assert result["plan_feasible"]


def test_minimum_block_never_buys_at_a_dearer_rate_than_the_plan():
    inputs = arguments()
    fresh = build_schedule(**inputs)
    result = build_schedule(**inputs, min_block_until_ts=NOW + 1800)
    assert result["action"] == fresh["action"] == "off"
    assert result["planned_intervals"] == fresh["planned_intervals"]
    assert result["retained_charge_until_ts"] is None


@pytest.mark.parametrize("updates", [
    {"unavailable": True},
    {"budget_seconds": 0},
    {"charge_not_before_ts": NOW + 3600},
    {"current_soc_pct": 100},
    {"min_block_until_ts": NOW},
])
def test_minimum_block_obeys_holds_budget_coverage_and_expiry(updates):
    inputs = {**flat(), "min_block_until_ts": NOW + 1800, **updates}
    result = build_schedule(**inputs)
    assert not any(item["start"] <= NOW < item["end"] for item in result["planned_intervals"])
    assert result["retained_charge_until_ts"] is None


def test_minimum_block_does_not_shorten_a_longer_committed_interval():
    result = build_schedule(**flat(), committed_until_ts=NOW + 3000, min_block_until_ts=NOW + 600)
    assert result["planned_intervals"][0] == {"start": NOW, "end": NOW + 3000}


def test_committed_cheap_interval_ends_at_three_without_adding_peak_charging():
    now = NOW - 6 * 3600 - 15 * 60
    three_pm = now + 15 * 60
    inputs = arguments(now=now, soc=50, cheap_start=0, cheap_end=15)
    inputs["committed_until_ts"] = three_pm
    result = build_schedule(**inputs)
    assert result["planned_intervals"][0] == {"start": now, "end": three_pm}
    assert all(
        rate_at(inputs["cost_plan"], interval["start"], TZ_OFFSET) == 0.25
        for interval in result["planned_intervals"]
    )
    assert result["scheduled_min_soc_pct"] >= 10
    assert result["scheduled_sunrise_soc_pct"] >= 30
    assert result["plan_feasible"]
