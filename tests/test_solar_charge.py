"""Decision-logic tests for solar_charge.compute_plan + gate_min_hold.

Pure-function tests: no DB, no Kasa, no asyncio. Tests cover the
forecast-driven gate (current design) — the controller turns ON when
baseline_sunrise_soc has headroom over the target+margin floor, OFF
when the headroom is consumed.
"""
from __future__ import annotations

import pytest

import solar_charge


def _cfg(**overrides):
    """Build a complete validated config; overrides merge on top of defaults."""
    base = dict(solar_charge.DEFAULT_CONFIG)
    base["mode"] = "active"
    base["kasa_device_host"] = "192.168.3.110"
    base.update(overrides)
    return solar_charge._validate_config(base)


def _eval(*, current_soc, predicted_sunrise=80.0, target=20.0,
          telemetry_age=10.0, cfg=None, solar_w=0.0, load_w=0.0,
          ac_input_w=0.0, now_ts=1_700_000_000.0):
    """Convenience wrapper around compute_plan with safe defaults."""
    return solar_charge.compute_plan(
        config=cfg or _cfg(),
        current_soc_pct=current_soc,
        solar_w=solar_w, load_w=load_w,
        ac_input_w=ac_input_w,
        telemetry_age_s=telemetry_age,
        target_sunrise_soc_pct=target,
        predicted_sunrise_soc_with_diversion=predicted_sunrise,
        predicted_sunrise_soc_baseline=predicted_sunrise,
        now_ts=now_ts,
    )


# ---------- Mode handling ----------
def test_mode_off_returns_skip():
    plan = _eval(current_soc=80, cfg=_cfg(mode="off"))
    assert plan.action == "skip"
    assert "mode=off" in plan.reason


def test_test_mode_computes_decisions_same_as_active():
    """Test mode is structurally identical at compute_plan level —
    the no-toggle gate is enforced server-side (only mode=='active'
    invokes kasa_client.set_state)."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, cfg=_cfg(mode="test"))
    assert plan.action == "on"
    assert plan.mode == "test"


# ---------- ON gate: forecast above target + margin + hysteresis ----------
def test_on_when_forecast_has_ample_headroom():
    """User's typical case: predicted sunrise 80% vs target 20% + 5
    margin + 3 hysteresis = 28% threshold. 80% >> 28% → ON."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=20.0)
    assert plan.action == "on"
    assert "headroom" in plan.reason


def test_on_when_forecast_just_above_threshold():
    """ON gate fires at >= target + margin + hysteresis = 28%.
    Forecast 28.5% → ON."""
    plan = _eval(current_soc=80, predicted_sunrise=28.5, target=20.0)
    assert plan.action == "on"


def test_skip_in_hysteresis_band():
    """Between OFF (25%) and ON (28%) thresholds with target=20.
    Predicted 26% → hold current state (skip)."""
    plan = _eval(current_soc=80, predicted_sunrise=26.0, target=20.0)
    assert plan.action == "skip"
    assert "hysteresis band" in plan.reason


# ---------- OFF gate: forecast at or below target + margin ----------
def test_off_when_forecast_below_floor():
    """Predicted sunrise drops to or below target+margin (= 25% with
    default config). Plug must turn OFF — would risk the overnight
    floor."""
    plan = _eval(current_soc=80, predicted_sunrise=24.0, target=20.0)
    assert plan.action == "off"
    assert "overnight floor" in plan.reason


def test_off_when_current_soc_below_hard_floor():
    """comfort_low is the hard SOC floor — overrides the forecast.
    Even if forecast says we'll recover by sunrise, if SOC is at the
    floor NOW, OFF immediately."""
    plan = _eval(current_soc=20, predicted_sunrise=80.0, target=20.0,
                 cfg=_cfg(comfort_low_pct=20))
    assert plan.action == "off"
    assert "comfort_low" in plan.reason


# ---------- Fail-closed safety paths ----------
def test_off_on_stale_telemetry():
    plan = _eval(current_soc=80, predicted_sunrise=80.0, telemetry_age=120)
    assert plan.action == "off"
    assert "stale telemetry" in plan.reason


def test_off_when_soc_missing():
    plan = _eval(current_soc=None, predicted_sunrise=80.0)
    assert plan.action == "off"
    assert "missing telemetry" in plan.reason


def test_off_when_forecast_missing():
    plan = solar_charge.compute_plan(
        config=_cfg(), current_soc_pct=80,
        solar_w=0, load_w=0, telemetry_age_s=10,
        target_sunrise_soc_pct=20,
        predicted_sunrise_soc_with_diversion=None,
        predicted_sunrise_soc_baseline=None,
    )
    assert plan.action == "off"
    assert "forecast unavailable" in plan.reason


def test_yields_to_grid_charging():
    """Critical safety: if the Jackery is being grid-charged (acip > 50W),
    excess diversion MUST yield regardless of forecast headroom. Running
    both flows simultaneously can exceed the wall circuit's amperage budget
    or trip the inverter's thermal protection. The check wins even when the
    forecast says we have huge headroom."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=20.0,
                 ac_input_w=800)  # smart_charge typical grid-charge rate
    assert plan.action == "off"
    assert "grid charging active" in plan.reason
    assert "inverter overdraw" in plan.reason


def test_acip_below_threshold_treated_as_noise():
    """50W threshold is well above sensor noise but below any real
    grid-charging rate, so a small phantom reading doesn't false-fire."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=20.0,
                 ac_input_w=30)  # below threshold
    assert plan.action == "on"  # forecast logic wins, yield doesn't fire


def test_grid_charge_yield_overrides_forecast():
    """Even if forecast headroom is huge AND SOC is high, grid charging
    forces OFF. There is no scenario where we want to run both."""
    plan = _eval(current_soc=99, predicted_sunrise=99.0, target=20.0,
                 ac_input_w=500)
    assert plan.action == "off"
    assert "grid charging" in plan.reason


def test_grid_charge_yield_uses_none_safely():
    """Telemetry without ac_input_w (older snapshots, etc.) — None
    must not crash the check. Falls through to forecast logic."""
    plan = solar_charge.compute_plan(
        config=_cfg(), current_soc_pct=80,
        solar_w=0, load_w=0, ac_input_w=None,
        telemetry_age_s=10, target_sunrise_soc_pct=20,
        predicted_sunrise_soc_with_diversion=80.0,
        predicted_sunrise_soc_baseline=80.0,
    )
    assert plan.action == "on"


def test_solar_load_ignored_for_decision():
    """The new forecast-driven controller doesn't use real-time
    surplus. solar_w=0, load_w=0 (nighttime, no surplus) still allows
    ON when forecast headroom is present — that's the whole point of
    the redesign."""
    plan = _eval(current_soc=80, predicted_sunrise=66.0, target=20.0,
                 solar_w=0, load_w=0)
    assert plan.action == "on"


# ---------- gate_min_hold (unchanged from original design) ----------
def test_min_hold_downgrades_flip_within_window():
    plan = _eval(current_soc=80, predicted_sunrise=80.0, now_ts=1000)
    assert plan.action == "on"
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=990, min_hold_s=30,
        plug_state_before="off", now_ts=1000)
    assert gated.action == "skip"
    assert "min_hold not elapsed" in gated.reason


def test_min_hold_allows_flip_after_window():
    plan = _eval(current_soc=80, predicted_sunrise=80.0, now_ts=1000)
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=960, min_hold_s=30,
        plug_state_before="off", now_ts=1000)
    assert gated.action == "on"


def test_min_hold_no_effect_when_state_matches():
    plan = _eval(current_soc=80, predicted_sunrise=80.0, now_ts=1000)
    assert plan.action == "on"
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=10, min_hold_s=30,
        plug_state_before="on", now_ts=1000)
    assert gated.action == "skip"
    assert "already on" in gated.reason


# ---------- Config validation ----------
def test_config_validates_ranges():
    cfg = solar_charge._validate_config({
        "mode": "active",
        "car_load_w": 99999,  # too high; falls back
        "on_hysteresis_pp": 5,  # in range; sticks
        "min_hold_s": 5,  # below 10; falls back
    })
    assert cfg["car_load_w"] == solar_charge.DEFAULT_CONFIG["car_load_w"]
    assert cfg["on_hysteresis_pp"] == 5
    assert cfg["min_hold_s"] == solar_charge.DEFAULT_CONFIG["min_hold_s"]


def test_config_rejects_inverted_comfort_bands():
    cfg = solar_charge._validate_config({
        "mode": "active",
        "comfort_low_pct": 80,
        "comfort_high_pct": 50,
    })
    assert cfg["comfort_low_pct"] == solar_charge.DEFAULT_CONFIG["comfort_low_pct"]
    assert cfg["comfort_high_pct"] == solar_charge.DEFAULT_CONFIG["comfort_high_pct"]


def test_config_unknown_mode_falls_back_to_off():
    cfg = solar_charge._validate_config({"mode": "nope"})
    assert cfg["mode"] == "off"
