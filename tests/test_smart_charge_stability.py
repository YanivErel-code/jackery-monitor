from __future__ import annotations

from itertools import pairwise

import pytest

import forecaster
from smart_charge_schedule import build_schedule


def incident_arguments():
    now = 1792717363
    first_end = now // 3600 * 3600 + 3600
    loads = [
        809.3,
        813.7,
        825.8,
        807.1,
        813.7,
        807.1,
        713.4,
        711.2,
        705.9,
        709.2,
        799.4,
        810.4,
        713.6,
    ]
    hours = [
        {
            "ts": first_end + index * 3600,
            "duration_h": (first_end - now) / 3600 if index == 0 else 1,
            "solar_w": 0,
            "load_w": load,
        }
        for index, load in enumerate(loads)
    ]
    return {
        "forecast": {"forecast": hours, "charge_efficiency": 0.87},
        "current_soc_pct": 15.333333333333336,
        "capacity_wh": 30240,
        "now": now,
        "sunrise": hours[-1]["ts"],
        "target": 30,
        "reserve": 10,
        "power_w": 908,
        "budget_seconds": 38660,
        "cost_plan": {
            "type": "tou",
            "tou_rates": [
                {"start_hour": 0, "end_hour": 15, "rate": 0.25},
                {"start_hour": 15, "end_hour": 16, "rate": 0.41},
                {"start_hour": 16, "end_hour": 21, "rate": 0.43},
                {"start_hour": 21, "end_hour": 24, "rate": 0.41},
            ],
        },
        "tz_offset_seconds": -25200,
        "charge_not_before_ts": None,
        "unavailable": False,
        "margin_seconds": 3600,
    }


def test_oct22_observed_load_reproduction_does_not_chatter_as_clock_advances():
    arguments = incident_arguments()
    results = [
        build_schedule(**{**arguments, "now": arguments["now"] + delta})
        for delta in range(0, 1500, 300)
    ]
    assert len({result["action"] for result in results}) == 1
    assert all(not result["plan_feasible"] for result in results)


def test_soc_optimizer_can_keep_full_precision_without_changing_display_default():
    hours = [{"ts": 3600, "duration_h": 1, "solar_w": 0, "load_w": 1}]
    displayed = forecaster.simulate_soc(15, 30240, hours)
    precise = forecaster.simulate_soc(15, 30240, hours, round_soc=False)
    assert displayed[0]["predicted_soc"] == 15
    assert precise[0]["predicted_soc"] == pytest.approx(15 - 100 / 30240)


def test_active_committed_interval_survives_rolling_replans_until_original_end():
    arguments = incident_arguments()
    committed_end = arguments["forecast"]["forecast"][0]["ts"]
    for delta in range(0, 1500, 300):
        now = arguments["now"] + delta
        result = build_schedule(**{**arguments, "now": now, "committed_until_ts": committed_end})
        assert result["action"] == "on"
        assert result["retained_charge_until_ts"] == committed_end
        assert result["planned_intervals"][0] == {"start": now, "end": committed_end}
        assert (
            sum(item["end"] - item["start"] for item in result["planned_intervals"])
            <= arguments["budget_seconds"]
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"unavailable": True},
        {"charge_not_before_ts": 1792724400},
        {"budget_seconds": 0},
    ],
)
def test_commitment_never_overrides_lg_manual_hold_or_duration_budget(updates):
    arguments = incident_arguments()
    result = build_schedule(
        **{**arguments, "committed_until_ts": arguments["now"] + 1800, **updates}
    )
    assert result["action"] == "off"
    assert result["retained_charge_until_ts"] is None


def test_remaining_budget_caps_committed_interval_and_never_overlaps_new_slots():
    arguments = incident_arguments()
    result = build_schedule(
        **{**arguments, "committed_until_ts": arguments["now"] + 1800, "budget_seconds": 75}
    )
    assert result["planned_intervals"] == [
        {"start": arguments["now"], "end": arguments["now"] + 75}
    ]
    assert result["retained_charge_until_ts"] == arguments["now"] + 75


def test_commitment_is_released_when_no_grid_path_covers_reserve_and_target():
    arguments = incident_arguments()
    result = build_schedule(
        **{**arguments, "current_soc_pct": 100, "committed_until_ts": arguments["now"] + 1800}
    )
    assert result["action"] == "off"
    assert result["planned_intervals"] == []
    assert result["retained_charge_until_ts"] is None


def test_expired_commitment_cannot_force_a_new_charging_session():
    arguments = incident_arguments()
    fresh = build_schedule(**arguments)
    result = build_schedule(**{**arguments, "committed_until_ts": arguments["now"]})
    assert result["action"] == fresh["action"]
    assert result["planned_intervals"] == fresh["planned_intervals"]
    assert result["retained_charge_until_ts"] is None


def test_partial_commitment_does_not_double_count_its_remaining_hour():
    arguments = incident_arguments()
    result = build_schedule(**{**arguments, "committed_until_ts": arguments["now"] + 75})
    intervals = result["planned_intervals"]
    assert intervals[0] == {"start": arguments["now"], "end": arguments["now"] + 75}
    assert all(previous["end"] <= following["start"] for previous, following in pairwise(intervals))
    assert sum(item["end"] - item["start"] for item in intervals) <= arguments["budget_seconds"]


def test_retention_is_bounded_to_current_forecast_interval():
    arguments = incident_arguments()
    result = build_schedule(**{**arguments, "committed_until_ts": arguments["sunrise"]})
    assert result["retained_charge_until_ts"] == arguments["forecast"]["forecast"][0]["ts"]
