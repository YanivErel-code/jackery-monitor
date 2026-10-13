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
