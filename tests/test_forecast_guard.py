"""Measured load persistence and SOC residuals stay causal and uncertainty-bounded."""

from copy import deepcopy

import pytest

import forecaster
import smart_charge
from forecast_guard import fit_load_nowcast, reconcile_energy

NOW = 1_790_000_000 // 3600 * 3600 + 2700


def power(minutes=30, output=3000, solar=750, ac=0):
    end = NOW // 60 * 60
    return [
        {
            "bucket": end - minute * 60,
            "output_wh": output / 60,
            "solar_wh": solar / 60,
            "ac_input_wh": ac / 60,
            "last_source_ts": end - minute * 60 + 50,
            "sample_count": 30,
        }
        for minute in range(minutes, 0, -1)
    ]


def snapshot(at, soc):
    return {
        "captured_at": at,
        "system_soc_pct": soc,
        "capacity_wh": 30240,
        "main_soc_source_ts": at - 5,
        "main_soc_source": "http",
        "pack_source_ts": at - 5,
        "pack_source": "mqtt",
        "pack_sns": [f"pack-{n}" for n in range(5)],
        "pack_soc_pct": [soc] * 5,
    }


def test_cycling_load_uses_measured_average_with_explicit_planning_allowance():
    rows = power(output=3000)
    # Heater OFF for five minutes must not erase its measured cycling demand.
    for row in rows[-5:]:
        row["output_wh"] = 500 / 60
    result = fit_load_nowcast(rows, now_ts=NOW, appliance_active=True, hold_until_ts=NOW + 1440)
    expected = (10 * 3000 + 5 * 500) / 15
    assert result["observed_output_w"] == pytest.approx(expected, abs=0.1)
    assert result["planning_output_w"] == pytest.approx(expected * 1.2, abs=0.1)
    assert result["until_ts"] == NOW + 1440
    assert result["uncertainty_pct"] == 20


@pytest.mark.parametrize(
    "fault", ["gap", "stale", "future_receipt", "negative", "nan", "few_ticks", "duplicate"]
)
def test_unreliable_recent_minutes_cannot_supply_an_optimistic_nowcast(fault):
    rows = power()
    if fault == "gap":
        rows.pop()
    elif fault == "stale":
        rows[-1]["last_source_ts"] -= 600
    elif fault == "future_receipt":
        rows[-1]["last_source_ts"] = NOW + 1
    elif fault == "negative":
        rows[-1]["output_wh"] = -1
    elif fault == "nan":
        rows[-1]["output_wh"] = float("nan")
    elif fault == "few_ticks":
        rows[-1]["sample_count"] = 1
    else:
        rows.append(deepcopy(rows[-1]))
    assert fit_load_nowcast(rows, now_ts=NOW) is None


def test_load_evidence_never_persists_unbounded_or_uses_an_instantaneous_peak():
    rows = power(output=500)
    rows[-1]["output_wh"] = 6500 / 60
    result = fit_load_nowcast(rows, now_ts=NOW)
    assert result["observed_output_w"] == 1700
    assert result["until_ts"] == NOW + 900
    assert fit_load_nowcast(rows, now_ts=NOW, appliance_active=True)["until_ts"] == NOW + 1800
    assert (
        fit_load_nowcast(rows, now_ts=NOW, appliance_active=True, hold_until_ts=NOW + 86400)[
            "until_ts"
        ]
        == NOW + 7200
    )


def forecast_hours():
    end = NOW // 3600 * 3600 + 3600
    return [
        {
            "ts": end + hour * 3600,
            "duration_h": 0.25 if hour == 0 else 1,
            "solar_w": 350,
            "load_w": 867,
            "output_w": 470,
            "load_without_baseline_w": 517,
            "drain_baseline_prior_w": 350,
        }
        for hour in range(3)
    ]


def test_load_overlay_reproduces_reserve_risk_without_double_counting_appliance():
    observation = fit_load_nowcast(
        power(), now_ts=NOW, appliance_active=True, hold_until_ts=NOW + 1740
    )
    hours = forecaster._apply_load_nowcast(forecast_hours(), observation, 0.1)
    assert hours[0]["output_w"] == 3600
    assert hours[0]["load_w"] == pytest.approx(3600 * 1.1 + 350)
    trace = forecaster.simulate_soc(12.5, 30240, hours)
    import lg_thinq

    assert lg_thinq.soc_at_finish({"forecast": trace}, 12.5, now_ts=NOW, finish_ts=NOW + 1440) < 10
    # Expiry is a real interval boundary, so scheduling cannot hide the trough
    # in an hour that also contains later charging or baseline-only demand.
    assert any(row["ts"] == NOW + 1740 for row in trace)
    assert hours[-1]["output_w"] == 470
    assert "load_nowcast_applied" not in hours[-1]
    assert forecast_hours()[0]["output_w"] == 470


def test_recent_demand_cannot_lower_historical_load_or_invent_an_appliance_addition():
    observation = fit_load_nowcast(power(output=300), now_ts=NOW)
    rows = forecaster._apply_load_nowcast(forecast_hours(), observation, 0.1)
    assert all(row["output_w"] == 470 for row in rows)
    assert all(row["load_w"] == 867 for row in rows)


def test_sunrise_uses_actual_interval_duration_when_nowcast_splits_a_sunlit_hour():
    rows = [
        {"ts": 3600, "duration_h": 1, "solar_w": 0, "predicted_soc": 30},
        {"ts": 4500, "duration_h": 0.25, "solar_w": 100, "predicted_soc": 31},
        {"ts": 7200, "duration_h": 0.75, "solar_w": 100, "predicted_soc": 32},
    ]
    assert smart_charge._find_next_sunrise(rows) == 3600
    assert smart_charge._predicted_sunrise_soc(rows, 3600) == 30


def test_low_soc_residual_is_reported_without_treating_it_as_a_fitted_loss():
    previous, current = snapshot(NOW - 1200, 12.5), snapshot(NOW, 7.8333333333)
    rows = power(output=2819.35, solar=720.75)
    result = reconcile_energy(previous, current, rows, baseline_w=350, overhead_pct=0.1)
    assert result["status"] == "discrepancy"
    assert result["observed_soc_drop_wh"] == pytest.approx(1411.2, abs=0.1)
    assert result["unexplained_wh"] == pytest.approx(501, abs=2)
    assert result["low_soc_learning_excluded"] is True
    assert current["system_soc_pct"] == 7.8333333333


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("ac", "ac_bypass_interval"),
        ("source", "source_changed"),
        ("pack", "pack_identity_unconfirmed"),
        ("legacy", "pack_identity_unconfirmed"),
        ("gap", "incomplete_power"),
        ("stale", "stale_soc"),
    ],
)
def test_reconciliation_does_not_blame_sources_or_losses_without_comparable_evidence(fault, reason):
    previous, current = snapshot(NOW - 1200, 12.5), snapshot(NOW, 7.8333333333)
    rows = power()
    if fault == "ac":
        rows[-2]["ac_input_wh"] = 10
    elif fault == "source":
        current["pack_source"] = "http"
    elif fault == "pack":
        current["pack_sns"][0] = "replacement"
    elif fault == "legacy":
        previous.pop("pack_sns")
    elif fault == "gap":
        rows.pop()
    else:
        previous["pack_source_ts"] -= 600
    result = reconcile_energy(previous, current, rows, baseline_w=350, overhead_pct=0.1)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason


def test_soc_quantization_is_not_reported_as_a_physical_loss():
    result = reconcile_energy(
        snapshot(NOW - 1200, 12.5), snapshot(NOW, 9.5), power(), baseline_w=350, overhead_pct=0.1
    )
    assert result["status"] == "consistent"
    assert abs(result["unexplained_wh"]) < result["tolerance_wh"]


def test_public_forecast_applies_recent_demand_without_retraining_load_profile():
    history = [
        {
            "ts": NOW - day * 86400,
            "output_w": 470,
            "solar_w": 0,
            "battery_pct": 50,
            "system_soc": 50,
        }
        for day in range(14, 0, -1)
    ]
    end = NOW // 3600 * 3600 + 3600
    weather = [{"ts": end + hour * 3600, "ghi_w_m2": 0} for hour in range(3)]
    original = forecaster.build_forecast(history, weather, 12.5, 30240, now_ts=NOW, pack_count=5)
    updated = forecaster.build_forecast(
        history,
        weather,
        12.5,
        30240,
        now_ts=NOW,
        pack_count=5,
        recent_power=power(),
        appliance_active=True,
        load_hold_until_ts=NOW + 1440,
    )
    assert updated["ready"]
    assert updated["forecast"][0]["predicted_soc"] < original["forecast"][0]["predicted_soc"]
    assert updated["forecast"][0]["output_w"] == 3600
    assert updated["forecast"][-1]["output_w"] == original["forecast"][-1]["output_w"]
    assert updated["solar_coefficient"] == original["solar_coefficient"]
    assert updated["load_nowcast"]["source"] == "recent_metered_load"
