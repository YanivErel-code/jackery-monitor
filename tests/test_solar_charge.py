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
          ac_input_w=0.0, now_ts=1_700_000_000.0,
          capacity_wh=None, hours_to_sunrise=None, household_load_w=None,
          predicted_min_soc=None):
    """Convenience wrapper around compute_plan with safe defaults.

    The overnight-reserve guard inputs (capacity_wh / hours_to_sunrise /
    household_load_w) and the cloudy-tomorrow guard input
    (predicted_min_soc) default to None → guards are no-ops unless a
    test opts in."""
    return solar_charge.compute_plan(
        config=cfg or _cfg(),
        current_soc_pct=current_soc,
        solar_w=solar_w, load_w=load_w,
        ac_input_w=ac_input_w,
        telemetry_age_s=telemetry_age,
        target_sunrise_soc_pct=target,
        predicted_sunrise_soc_with_diversion=predicted_sunrise,
        predicted_sunrise_soc_baseline=predicted_sunrise,
        capacity_wh=capacity_wh,
        hours_to_sunrise=hours_to_sunrise,
        household_load_w=household_load_w,
        predicted_min_soc_pct=predicted_min_soc,
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


# ---------- ON gate: forecast headroom AND SOC above comfort_high ----------
def test_on_when_forecast_and_soc_both_pass():
    """Both gates pass: predicted sunrise 80% vs 28% threshold,
    AND SOC 80% >= comfort_high 70%. ON."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=20.0)
    assert plan.action == "on"
    assert "headroom" in plan.reason


def test_on_when_forecast_just_above_threshold():
    """Forecast 28.5% → ON, with SOC well above comfort_high."""
    plan = _eval(current_soc=80, predicted_sunrise=28.5, target=20.0)
    assert plan.action == "on"


def test_skip_when_soc_below_comfort_high_even_if_forecast_great():
    """Critical regression test: after the hard floor catches a
    runaway drain, the controller MUST NOT immediately resume just
    because the forecast looks great. Requires SOC to climb back to
    comfort_high (50% per user config / 70% default) before starting
    a new session."""
    plan = _eval(current_soc=22, predicted_sunrise=80.0, target=20.0)
    # SOC 22% > comfort_low 30% (default)? No, default is 30, so 22<=30 → off via hard floor.
    # Test with a config that allows SOC=22 above comfort_low.
    plan = _eval(current_soc=40, predicted_sunrise=80.0, target=20.0,
                 cfg=_cfg(comfort_low_pct=20, comfort_high_pct=50))
    assert plan.action == "skip"
    assert "comfort_high" in plan.reason


def test_skip_in_forecast_hysteresis_band():
    """Between OFF (25%) and ON (28%) forecast thresholds.
    Predicted 26% with SOC above comfort_high → forecast band skip."""
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


# ---------- Overnight-reserve guard (model-free, forecast-independent) ----------
def test_reserve_guard_blocks_overnight_battery_drain():
    """No solar surplus + coasting on household load alone to sunrise
    would land below target+margin → OFF, even though the (optimistic)
    forecast says we're fine. This is the 2026-06-18 81%->17% case:
    SOC 78%, ~600W house, ~9h to sunrise -> ~52pp drain -> ~26% < 35%."""
    plan = _eval(current_soc=78, predicted_sunrise=80.0, target=30.0,
                 solar_w=0.0, load_w=600.0,
                 capacity_wh=10080, hours_to_sunrise=9.0,
                 household_load_w=600.0)
    assert plan.action == "off"
    assert "overnight-reserve guard" in plan.reason


def test_reserve_guard_skipped_when_solar_surplus():
    """Daytime: solar exceeds house load → diversion is self-funding →
    guard does NOT fire; the normal forecast/SOC gate decides (ON)."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0,
                 solar_w=3000.0, load_w=500.0,
                 capacity_wh=10080, hours_to_sunrise=18.0,
                 household_load_w=500.0)
    assert plan.action == "on"


def test_reserve_guard_allows_true_excess():
    """No surplus but plenty of reserve: SOC 80%, ~500W house, only ~4h
    to sunrise -> ~20pp drain -> ~60% at sunrise >> floor. Guard passes;
    forecast/SOC gate turns ON."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0,
                 solar_w=0.0, load_w=500.0,
                 capacity_wh=10080, hours_to_sunrise=4.0,
                 household_load_w=500.0)
    assert plan.action == "on"


def test_reserve_guard_noop_without_inputs():
    """Back-compat: without the guard inputs the controller behaves
    exactly as before (no spurious OFF)."""
    plan = _eval(current_soc=78, predicted_sunrise=80.0, target=30.0,
                 solar_w=0.0, load_w=600.0)
    assert plan.action == "on"


# ---------- Cloudy-tomorrow guard (36h trough vs comfort_low) ----------
def test_cloudy_guard_blocks_when_trough_below_comfort_low():
    """Sunrise prediction is fine (38%) but the 36h trough dips to 22%
    < comfort_low 30% (default) — the 2026-07-12/13 case: evening car
    charge approved on a sunny-refill assumption, overcast next day
    drained the pack to 20%. Guard must force OFF."""
    plan = _eval(current_soc=75, predicted_sunrise=38.0, target=30.0,
                 predicted_min_soc=22.0)
    assert plan.action == "off"
    assert "cloudy-tomorrow guard" in plan.reason


def test_cloudy_guard_hysteresis_band_holds_state():
    """Trough in [comfort_low, comfort_low+hyst) → skip (hold current
    state), so an in-flight session isn't flapped by forecast wobble."""
    plan = _eval(current_soc=75, predicted_sunrise=80.0, target=30.0,
                 predicted_min_soc=31.0)
    assert plan.action == "skip"
    assert "cloudy-tomorrow hysteresis" in plan.reason


def test_cloudy_guard_passes_when_trough_clear():
    """Trough comfortably above comfort_low+hyst → normal gates decide
    (ON here: sunrise 80% ≥ threshold, SOC ≥ comfort_high)."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0,
                 predicted_min_soc=45.0)
    assert plan.action == "on"


def test_cloudy_guard_noop_without_input():
    """Back-compat: predicted_min_soc=None → guard skipped entirely."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0)
    assert plan.action == "on"


def test_cloudy_guard_boundaries():
    """Exact-equality semantics mirror the sunrise gate (strict <):
    trough == comfort_low lands in the skip band, trough ==
    comfort_low+hyst passes to the normal gates."""
    at_low = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0,
                   predicted_min_soc=30.0)   # == comfort_low (default 30)
    assert at_low.action == "skip"
    at_band_top = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0,
                        predicted_min_soc=33.0)  # == comfort_low + hyst 3
    assert at_band_top.action == "on"


def test_cloudy_guard_band_holds_inflight_on_through_min_hold():
    """Composition: a band 'skip' must pass through gate_min_hold
    untouched, so an in-flight ON session keeps running (skip = hold
    state, not a toggle)."""
    plan = _eval(current_soc=75, predicted_sunrise=80.0, target=30.0,
                 predicted_min_soc=31.5, now_ts=1_700_000_000.0)
    assert plan.action == "skip"
    held = solar_charge.gate_min_hold(
        plan, last_toggle_ts=1_700_000_000.0 - 5.0,  # toggled 5s ago
        min_hold_s=30, plug_state_before="on")
    assert held.action == "skip"          # no OFF injected — plug stays on
    assert "cloudy-tomorrow" in held.reason


# ---------- Pack-balance window ----------
def test_balance_due_blocks_diversion_despite_perfect_conditions():
    """Every 60 days the packs need a genuine full charge to balance:
    with balance_due, the car gets nothing even when every other gate
    would say ON (great forecast, high SOC, daytime surplus)."""
    plan = solar_charge.compute_plan(
        config=_cfg(), current_soc_pct=85,
        solar_w=3000.0, load_w=450.0, ac_input_w=0.0,
        telemetry_age_s=10.0, target_sunrise_soc_pct=30.0,
        predicted_sunrise_soc_with_diversion=80.0,
        predicted_sunrise_soc_baseline=80.0,
        balance_due=True, days_since_full=61.0,
        now_ts=1_700_000_000.0,
    )
    assert plan.action == "off"
    assert "pack-balance" in plan.reason
    assert "61d" in plan.reason


def test_balance_due_reports_never_full():
    plan = solar_charge.compute_plan(
        config=_cfg(), current_soc_pct=85,
        solar_w=3000.0, load_w=450.0, ac_input_w=0.0,
        telemetry_age_s=10.0, target_sunrise_soc_pct=30.0,
        predicted_sunrise_soc_with_diversion=80.0,
        predicted_sunrise_soc_baseline=80.0,
        balance_due=True, days_since_full=None,
        now_ts=1_700_000_000.0,
    )
    assert plan.action == "off"
    assert "no full charge on record" in plan.reason


def test_balance_not_due_is_noop():
    """balance_due=False (default) leaves the normal gates in charge."""
    plan = _eval(current_soc=80, predicted_sunrise=80.0, target=30.0)
    assert plan.action == "on"


def test_balance_config_validates():
    cfg = solar_charge._validate_config(
        {**solar_charge.DEFAULT_CONFIG,
         "balance_every_days": 90, "balance_target_main_pct": 98})
    assert cfg["balance_every_days"] == 90
    assert cfg["balance_target_main_pct"] == 98
    # Out-of-range falls back to defaults.
    cfg = solar_charge._validate_config(
        {**solar_charge.DEFAULT_CONFIG,
         "balance_every_days": 9999, "balance_target_main_pct": 10})
    assert cfg["balance_every_days"] == solar_charge.DEFAULT_CONFIG["balance_every_days"]
    assert cfg["balance_target_main_pct"] == solar_charge.DEFAULT_CONFIG["balance_target_main_pct"]


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


# ---------- Inverter overload protection ----------
def _on_plan(now_ts=1_700_000_000.0):
    """Build a fresh ON plan via compute_plan so tests use real shapes."""
    return _eval(current_soc=80, predicted_sunrise=80.0, now_ts=now_ts)


def test_inverter_protect_passes_through_when_no_overload():
    plan = _on_plan()
    out = solar_charge.gate_inverter_protect(
        plan, last_overload_ts=0, cooldown_s=1800,
        now_ts=1_700_000_000.0)
    assert out.action == "on"


def test_inverter_protect_blocks_on_within_cooldown():
    plan = _on_plan(now_ts=1_700_000_500.0)
    # Overload stamped 100s ago; cooldown 1800s → still active.
    out = solar_charge.gate_inverter_protect(
        plan, last_overload_ts=1_700_000_400.0, cooldown_s=1800,
        now_ts=1_700_000_500.0)
    assert out.action == "skip"
    assert "inverter-protect cooldown" in out.reason


def test_inverter_protect_releases_after_cooldown():
    plan = _on_plan(now_ts=1_700_002_000.0)
    # Overload stamped 1900s ago; cooldown 1800s → expired.
    out = solar_charge.gate_inverter_protect(
        plan, last_overload_ts=1_700_000_100.0, cooldown_s=1800,
        now_ts=1_700_002_000.0)
    assert out.action == "on"


def test_inverter_protect_never_overrides_off():
    """An OFF plan must stay OFF — we never want the protect gate to
    upgrade a safety decision into a 'skip'."""
    plan = _eval(current_soc=10, predicted_sunrise=80.0)
    assert plan.action == "off"
    out = solar_charge.gate_inverter_protect(
        plan, last_overload_ts=1_700_000_400.0, cooldown_s=1800,
        now_ts=1_700_000_500.0)
    assert out.action == "off"


def test_inverter_protect_never_overrides_skip():
    """A skip plan stays a skip — no point upgrading 'already in
    desired state' into a different reason."""
    plan = _eval(current_soc=50, predicted_sunrise=80.0)  # SOC < comfort_high → skip
    assert plan.action == "skip"
    out = solar_charge.gate_inverter_protect(
        plan, last_overload_ts=1_700_000_400.0, cooldown_s=1800,
        now_ts=1_700_000_500.0)
    assert out.action == "skip"


def test_inverter_protect_config_bounds():
    """load_w accepts 500–4500W; cooldown_s accepts 60s–86400s."""
    cfg = solar_charge._validate_config({
        "mode": "active",
        "inverter_protect_load_w": 100,       # below 500; falls back
        "inverter_protect_cooldown_s": 30,    # below 60; falls back
    })
    assert cfg["inverter_protect_load_w"] == solar_charge.DEFAULT_CONFIG[
        "inverter_protect_load_w"]
    assert cfg["inverter_protect_cooldown_s"] == solar_charge.DEFAULT_CONFIG[
        "inverter_protect_cooldown_s"]
    cfg = solar_charge._validate_config({
        "mode": "active",
        "inverter_protect_load_w": 2500,
        "inverter_protect_cooldown_s": 3600,
    })
    assert cfg["inverter_protect_load_w"] == 2500
    assert cfg["inverter_protect_cooldown_s"] == 3600


def test_overload_state_roundtrip(tmp_path, monkeypatch):
    """stamp_overload → read_overload_state → clear_overload_state."""
    p = tmp_path / "overload.json"
    monkeypatch.setattr(solar_charge, "OVERLOAD_STATE_PATH", str(p))
    assert solar_charge.read_overload_state() == {}
    solar_charge.stamp_overload("SN-A", 2400.0, now_ts=1234.5)
    state = solar_charge.read_overload_state()
    assert state["SN-A"]["last_overload_ts"] == 1234.5
    assert state["SN-A"]["load_w"] == 2400.0
    # Second device adds without clobbering the first.
    solar_charge.stamp_overload("SN-B", 3000.0, now_ts=5000.0)
    state = solar_charge.read_overload_state()
    assert set(state.keys()) == {"SN-A", "SN-B"}
    # Re-stamping the same device updates in place.
    solar_charge.stamp_overload("SN-A", 2200.0, now_ts=6000.0)
    state = solar_charge.read_overload_state()
    assert state["SN-A"]["last_overload_ts"] == 6000.0
    assert state["SN-A"]["load_w"] == 2200.0
    solar_charge.clear_overload_state()
    assert solar_charge.read_overload_state() == {}


# ---------- Pre-engage load ceiling ----------
def _on_plan_with_car():
    """Build an ON plan via compute_plan with a non-trivial car_load_w
    so the gate's reason includes a real number."""
    cfg = _cfg(car_load_w=1400)
    return solar_charge.compute_plan(
        config=cfg, current_soc_pct=80, solar_w=0, load_w=0,
        telemetry_age_s=10, target_sunrise_soc_pct=20,
        predicted_sunrise_soc_with_diversion=80.0,
        predicted_sunrise_soc_baseline=80.0,
        now_ts=1_700_000_000.0,
    )


def test_load_ceiling_blocks_off_to_on_when_load_at_ceiling():
    plan = _on_plan_with_car()
    out = solar_charge.gate_load_ceiling(
        plan, load_w=800.0, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "skip"
    assert "ceiling 800W" in out.reason
    assert "~1400W" in out.reason


def test_load_ceiling_blocks_off_to_on_when_load_above():
    plan = _on_plan_with_car()
    out = solar_charge.gate_load_ceiling(
        plan, load_w=1500.0, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "skip"
    assert "1500W" in out.reason


def test_load_ceiling_passes_through_when_load_below():
    plan = _on_plan_with_car()
    out = solar_charge.gate_load_ceiling(
        plan, load_w=500.0, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "on"


def test_load_ceiling_passes_through_when_plug_already_on():
    """Once engaged, load_w includes the diversion's own draw, so
    'load too high' would be a false-positive block."""
    plan = _on_plan_with_car()
    out = solar_charge.gate_load_ceiling(
        plan, load_w=2000.0, plug_state_before="on",  # plug already on
        max_system_load_w=800.0)
    assert out.action == "on"


def test_load_ceiling_no_effect_on_off_plan():
    """The gate never touches OFF plans — never want to upgrade an
    explicit safety decision."""
    plan = _eval(current_soc=10, predicted_sunrise=80.0)  # SOC hits hard floor
    assert plan.action == "off"
    out = solar_charge.gate_load_ceiling(
        plan, load_w=2000.0, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "off"


def test_load_ceiling_no_effect_on_skip_plan():
    plan = _eval(current_soc=50, predicted_sunrise=80.0)  # SOC < comfort_high
    assert plan.action == "skip"
    out = solar_charge.gate_load_ceiling(
        plan, load_w=2000.0, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "skip"


def test_load_ceiling_passes_through_when_load_w_missing():
    """No fresh telemetry → other gates handle, this one stays out."""
    plan = _on_plan_with_car()
    out = solar_charge.gate_load_ceiling(
        plan, load_w=None, plug_state_before="off",
        max_system_load_w=800.0)
    assert out.action == "on"


def test_max_system_load_w_config_bounds():
    cfg = solar_charge._validate_config({
        "mode": "active",
        "max_system_load_w": 50,  # below 100 → falls back
    })
    assert cfg["max_system_load_w"] == solar_charge.DEFAULT_CONFIG[
        "max_system_load_w"]
    cfg = solar_charge._validate_config({
        "mode": "active",
        "max_system_load_w": 1200,
    })
    assert cfg["max_system_load_w"] == 1200


def test_overload_state_resilient_to_corrupt_file(tmp_path, monkeypatch):
    """Garbage on disk reads as empty rather than crashing the eval loop."""
    p = tmp_path / "overload.json"
    monkeypatch.setattr(solar_charge, "OVERLOAD_STATE_PATH", str(p))
    p.write_text("{ not valid json")
    assert solar_charge.read_overload_state() == {}
    # And we can still write through afterwards — corrupt file gets
    # overwritten by stamp_overload's atomic rename.
    solar_charge.stamp_overload("SN-A", 2400.0, now_ts=1234.5)
    assert solar_charge.read_overload_state() == {
        "SN-A": {"last_overload_ts": 1234.5, "load_w": 2400.0}
    }
