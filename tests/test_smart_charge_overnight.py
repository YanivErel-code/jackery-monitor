from datetime import datetime

import pytest

from smart_charge_schedule import build_schedule


def arguments(
    *,
    now="2026-10-24T21:00:00-07:00",
    sunrise="2026-10-25T07:00:00-07:00",
    soc=40,
    target=30,
    load=200,
    day_rate=0.25,
    night_rate=0.25,
):
    current = datetime.fromisoformat(now)
    deadline = datetime.fromisoformat(sunrise)
    now_ts, sunrise_ts = current.timestamp(), deadline.timestamp()
    hours = []
    end = now_ts // 3600 * 3600 + 3600
    while end - 3600 < sunrise_ts:
        hours.append(
            {
                "ts": end,
                "duration_h": (end - max(now_ts, end - 3600)) / 3600,
                "solar_w": 0,
                "load_w": load,
            }
        )
        end += 3600
    return {
        "forecast": {"forecast": hours, "charge_efficiency": 1},
        "current_soc_pct": soc,
        "capacity_wh": 10000,
        "now": now_ts,
        "sunrise": sunrise_ts,
        "target": target,
        "reserve": 10,
        "power_w": 1000,
        "budget_seconds": 12 * 3600,
        "cost_plan": {
            "type": "tou",
            "tou_rates": [
                {"start_hour": 0, "end_hour": 7, "rate": night_rate},
                {"start_hour": 0, "end_hour": 24, "rate": day_rate},
            ],
        },
        "tz_offset_seconds": int(current.utcoffset().total_seconds()),
        "charge_not_before_ts": None,
        "unavailable": False,
        "margin_seconds": 3600,
    }


def midnight(inputs):
    return inputs["sunrise"] - 7 * 3600


def test_equal_overnight_rates_start_at_midnight_not_six_am():
    inputs = arguments()
    result = build_schedule(**inputs)
    assert result["planned_intervals"] == [
        {"start": midnight(inputs), "end": midnight(inputs) + 3600}
    ]
    assert result["plan_feasible"]
    assert result["scheduled_sunrise_soc_pct"] >= 30


def test_feasible_overnight_charging_beats_even_cheaper_daytime_slots():
    inputs = arguments(now="2026-10-24T08:30:00-07:00", soc=50, day_rate=0.10)
    result = build_schedule(**inputs)
    assert result["action"] == "off"
    assert result["planned_intervals"][0]["start"] == midnight(inputs)
    assert all(interval["start"] >= midnight(inputs) for interval in result["planned_intervals"])
    assert result["scheduled_min_soc_pct"] >= 10
    assert result["scheduled_sunrise_soc_pct"] >= 30
    assert result["plan_feasible"]


def test_inside_overnight_window_starts_in_current_partial_hour():
    inputs = arguments(now="2026-10-25T00:20:00-07:00")
    result = build_schedule(**inputs)
    assert result["action"] == "on"
    assert result["planned_intervals"][0]["start"] == inputs["now"]


def test_partial_duration_budget_starts_at_midnight_instead_of_later_in_the_hour():
    inputs = arguments(soc=35, load=100)
    inputs["budget_seconds"] = 1800
    result = build_schedule(**inputs)
    assert result["planned_intervals"] == [
        {"start": midnight(inputs), "end": midnight(inputs) + 1800}
    ]
    assert result["plan_feasible"]


def test_half_hour_timezone_starts_at_local_midnight_not_next_utc_hour():
    inputs = arguments(now="2026-10-24T20:00:00+05:30", sunrise="2026-10-25T07:00:00+05:30")
    result = build_schedule(**inputs)
    assert result["planned_intervals"][0]["start"] == midnight(inputs)
    assert result["plan_feasible"]


def test_daytime_is_available_when_reserve_cannot_last_until_midnight():
    inputs = arguments(now="2026-10-24T08:30:00-07:00", soc=20)
    result = build_schedule(**inputs)
    assert any(interval["start"] < midnight(inputs) for interval in result["planned_intervals"])
    assert result["scheduled_min_soc_pct"] >= 10
    assert result["scheduled_sunrise_soc_pct"] >= 30
    assert result["plan_feasible"]


def test_daytime_is_available_when_overnight_capacity_cannot_meet_target():
    inputs = arguments(soc=30, target=98)
    result = build_schedule(**inputs)
    assert any(interval["start"] < midnight(inputs) for interval in result["planned_intervals"])
    assert result["scheduled_sunrise_soc_pct"] >= 98
    assert result["plan_feasible"]


def test_feasible_recovery_can_relax_preference_when_nonlinear_drain_and_budget_require_it():
    inputs = arguments(soc=41, load=300)
    inputs["budget_seconds"] = 3600
    inputs["forecast"]["drain_calibration"] = {
        "soc_bands": [{"min_soc": 40, "max_soc": 60, "baseline_w": 100}]
    }
    for hour in inputs["forecast"]["forecast"]:
        hour.update(load_without_baseline_w=0, drain_baseline_prior_w=300)
    result = build_schedule(**inputs)
    assert result["plan_feasible"]
    assert result["scheduled_sunrise_soc_pct"] >= 30
    assert any(interval["start"] < midnight(inputs) for interval in result["planned_intervals"])
    assert "preference relaxed" in result["reason"]


@pytest.mark.parametrize("constraint", ["hold", "duration", "not_before"])
def test_overnight_preference_cannot_override_real_constraints(constraint):
    inputs = arguments()
    if constraint == "hold":
        inputs["unavailable"] = True
    elif constraint == "duration":
        inputs["budget_seconds"] = 0
    else:
        inputs["charge_not_before_ts"] = inputs["sunrise"]
    result = build_schedule(**inputs)
    assert result["action"] == "off"
    assert not result["planned_intervals"]
    assert result["warnings"]


def test_no_charge_when_solar_and_soc_already_cover_both_goals():
    inputs = arguments(soc=95, load=100)
    result = build_schedule(**inputs)
    assert result["action"] == "off"
    assert not result["planned_intervals"]


def test_overnight_commitment_stays_bounded_and_does_not_slide_forward():
    inputs = arguments(now="2026-10-25T00:20:00-07:00")
    committed_end = inputs["now"] // 3600 * 3600 + 3600
    for elapsed in (0, 300, 600):
        result = build_schedule(
            **{**inputs, "now": inputs["now"] + elapsed, "committed_until_ts": committed_end}
        )
        assert result["action"] == "on"
        assert result["retained_charge_until_ts"] == committed_end
        assert result["planned_intervals"][0]["end"] == committed_end


def test_policy_version_is_saved_with_the_plan():
    result = build_schedule(**arguments())
    assert result["schedule_policy"] == "overnight_first_v1"
