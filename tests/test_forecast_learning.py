"""Forecast integration preserves conservative fallback and ignores future evidence."""

from datetime import datetime

import pytest

import forecaster
from tests.test_forecast_calibration import BASE, CAPACITY, clean_history


@pytest.mark.parametrize("old_load,new_load", [(530, 470), (470, 550)])
def test_load_profile_adapts_to_sustained_stable_shift(old_load, new_load):
    now = datetime(2026, 10, 13, 10).timestamp()
    history = [
        {"ts": now - day * 86400, "output_w": new_load if day < 3 else old_load}
        for day in range(14)
    ]
    profile = forecaster.fit_load_profile(history, now_ts=now)
    key = (10, 0)
    assert abs(profile[key] - new_load) < abs(old_load - new_load) * 0.3


def test_one_recent_load_spike_does_not_rewrite_stable_profile():
    now = datetime(2026, 10, 13, 10).timestamp()
    history = [
        {"ts": now - day * 86400, "output_w": 1500 if day == 0 else 500} for day in range(14)
    ]
    profile = forecaster.fit_load_profile(history, now_ts=now)
    assert profile[(10, 0)] == pytest.approx(500)


def test_load_profile_ignores_future_and_incomplete_hour_evidence():
    now = datetime(2026, 10, 13, 10).timestamp()
    history = [{"ts": now - day * 86400, "output_w": 500} for day in range(1, 14)]
    history.extend({"ts": now + day * 86400, "output_w": 1500} for day in range(1, 20))
    history.extend(
        {"ts": now - day * 86400, "output_w": 1500, "history_complete": False} for day in range(14)
    )
    profile = forecaster.fit_load_profile(history, now_ts=now)
    assert profile[(10, 0)] == pytest.approx(500)


def test_forecast_uses_validated_system_baseline_instead_of_pack_prior():
    history = clean_history()
    for row in history:
        row.update(
            output_w=500, output_wh=500, solar_wh=0, ac_input_wh=0, battery_pct=60, system_soc=60
        )
    now = BASE + 5 * 86400
    weather = [{"ts": now + hour * 3600, "ghi_w_m2": 0} for hour in range(1, 9)]
    result = forecaster.build_forecast(history, weather, 60, CAPACITY, now_ts=now, pack_count=5)
    assert result["ready"]
    assert result["effective_parasitic_w"] < 190
    assert result["drain_calibration"]["source"] == "validated_system_residual"
    assert result["forecast"][-1]["predicted_soc"] > 40


def test_forecast_reports_pack_prior_when_available_calibration_does_not_apply():
    history = clean_history()
    for row in history:
        row.update(
            output_w=500, output_wh=500, solar_wh=0, ac_input_wh=0, battery_pct=60, system_soc=60
        )
    now = BASE + 5 * 86400
    weather = [{"ts": now + hour * 3600, "ghi_w_m2": 0} for hour in range(1, 3)]
    result = forecaster.build_forecast(history, weather, 94, CAPACITY, now_ts=now, pack_count=5)
    assert result["drain_calibration"]["source"] == "validated_system_residual"
    assert result["pack_baseline_applied_w"] == 300
    assert result["forecast"][0]["drain_baseline_applied_w"] >= 300


def calibrated_hours(hours):
    return [
        {
            "ts": hour * 3600,
            "solar_w": 0,
            "load_w": 850,
            "load_without_baseline_w": 500,
            "drain_baseline_prior_w": 350,
        }
        for hour in range(1, hours + 1)
    ]


def test_empirical_baseline_only_applies_inside_validated_soc_band_in_dark_hours():
    band = [{"min_soc": 40, "max_soc": 60, "baseline_w": 100}]
    assert (
        forecaster.simulate_soc(50, CAPACITY, calibrated_hours(1), drain_calibration_bands=band)[0][
            "load_w"
        ]
        == 600
    )
    assert (
        forecaster.simulate_soc(35, CAPACITY, calibrated_hours(1), drain_calibration_bands=band)[0][
            "load_w"
        ]
        == 850
    )
    assert (
        forecaster.simulate_soc(41, CAPACITY, calibrated_hours(1), drain_calibration_bands=band)[0][
            "load_w"
        ]
        == 850
    )
    daylight = calibrated_hours(1)
    daylight[0]["solar_w"] = 1500
    assert (
        forecaster.simulate_soc(50, CAPACITY, daylight, drain_calibration_bands=band)[0]["load_w"]
        == 850
    )


def test_diversion_lookahead_accounts_for_return_to_prior_below_calibrated_range():
    """Last-hour diversion is safe; earlier hours must account for band exit."""
    band = [{"min_soc": 40, "max_soc": 60, "baseline_w": 0}]
    result = forecaster.simulate_soc(
        45,
        CAPACITY,
        calibrated_hours(4),
        extra_load_w=400,
        extra_load_floor_pct=35,
        drain_calibration_bands=band,
    )
    assert all(hour["extra_load_w_applied"] == 0 for hour in result[:3])
    assert result[-1]["predicted_soc"] >= 35


def test_extra_load_without_floor_still_falls_back_when_leaving_calibrated_range():
    band = [{"min_soc": 40, "max_soc": 60, "baseline_w": 100}]
    result = forecaster.simulate_soc(
        45,
        CAPACITY,
        calibrated_hours(1),
        extra_load_w=1000,
        drain_calibration_bands=band,
    )
    assert result[0]["load_w"] == 850
    assert result[0]["extra_load_w_applied"] == 1000
    assert result[0]["predicted_soc"] < 40


def test_calibration_can_correct_net_discharging_twilight_not_solar_charging():
    band = [{"min_soc": 40, "max_soc": 60, "baseline_w": 100}]
    hours = calibrated_hours(1)
    hours[0]["solar_w"] = 200
    result = forecaster.simulate_soc(50, CAPACITY, hours, drain_calibration_bands=band)
    assert result[0]["load_w"] == 600
    assert result[0]["predicted_soc"] < 50


def test_pooled_calibration_cannot_extrapolate_or_override_specific_soc_evidence():
    pooled = {"min_soc": 45, "max_soc": 85, "baseline_w": 100, "source": "pooled_discharge"}
    specific = {"min_soc": 40, "max_soc": 60, "baseline_w": 200}
    assert (
        forecaster.simulate_soc(
            94,
            CAPACITY,
            calibrated_hours(1),
            drain_calibration_bands=[pooled],
        )[0]["load_w"]
        == 850
    )
    assert (
        forecaster.simulate_soc(
            46,
            CAPACITY,
            calibrated_hours(1),
            drain_calibration_bands=[pooled],
        )[0]["load_w"]
        == 850
    )
    assert (
        forecaster.simulate_soc(
            50,
            CAPACITY,
            calibrated_hours(1),
            drain_calibration_bands=[specific, pooled],
        )[0]["load_w"]
        == 700
    )


def test_pooled_calibration_cannot_bypass_a_specific_band_exit():
    specific = {"min_soc": 40, "max_soc": 60, "baseline_w": 200}
    pooled = {"min_soc": 30, "max_soc": 85, "baseline_w": 0, "source": "pooled_discharge"}
    result = forecaster.simulate_soc(
        41,
        CAPACITY,
        calibrated_hours(1),
        drain_calibration_bands=[specific, pooled],
    )
    assert result[0]["load_w"] == 850


@pytest.mark.parametrize("net_w", [0, 600, -100])
def test_charge_ceiling_cannot_destroy_soc_already_in_the_battery(net_w):
    hours = [{"ts": 3600, "solar_w": max(0, net_w), "load_w": max(0, -net_w)}]
    result = forecaster.simulate_soc(94, CAPACITY, hours, soc_ceiling_pct=90)
    expected = 94 + min(0, net_w) / CAPACITY * 100
    assert result[0]["predicted_soc"] == pytest.approx(round(expected, 1))


def test_charge_ceiling_does_not_stay_raised_after_real_discharge():
    hours = [
        {"ts": 3600, "solar_w": 0, "load_w": 3024},
        {"ts": 7200, "solar_w": 10000, "load_w": 0},
    ]
    result = forecaster.simulate_soc(94, CAPACITY, hours, soc_ceiling_pct=90)
    assert result[0]["predicted_soc"] == 84
    assert result[1]["predicted_soc"] == 90


def test_diversion_lookahead_cannot_spend_energy_that_a_charge_ceiling_curtails():
    hours = [
        {"ts": 3600, "solar_w": 0, "load_w": 0},
        {"ts": 7200, "solar_w": 40000, "load_w": 0},
        {"ts": 10800, "solar_w": 0, "load_w": CAPACITY * 0.7},
    ]
    result = forecaster.simulate_soc(
        90,
        CAPACITY,
        hours,
        soc_ceiling_pct=90,
        extra_load_w=3024,
        extra_load_floor_pct=25,
    )
    assert all(hour["extra_load_w_applied"] == 0 for hour in result)
