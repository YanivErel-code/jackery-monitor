"""Decision-logic tests for solar_charge.compute_plan + gate_min_hold.

Pure-function tests: no DB, no Kasa, no asyncio. Each test stands up a
config dict, calls compute_plan with a known telemetry tuple, asserts the
action + reason match expectations.
"""
from __future__ import annotations

import pytest

import solar_charge


def _cfg(**overrides):
    """Build a complete validated config; overrides merge on top of defaults."""
    base = dict(solar_charge.DEFAULT_CONFIG)
    base["mode"] = "active"  # tests default to live mode
    base["kasa_device_host"] = "192.168.3.110"
    base.update(overrides)
    return solar_charge._validate_config(base)


def _eval(*, current_soc, solar_w, load_w, predicted_sunrise=80.0,
          target=40.0, telemetry_age=10.0, cfg=None, now_ts=1_700_000_000.0):
    """Convenience wrapper around compute_plan with safe defaults."""
    return solar_charge.compute_plan(
        config=cfg or _cfg(),
        current_soc_pct=current_soc,
        solar_w=solar_w, load_w=load_w,
        telemetry_age_s=telemetry_age,
        target_sunrise_soc_pct=target,
        predicted_sunrise_soc_with_diversion=predicted_sunrise,
        predicted_sunrise_soc_baseline=predicted_sunrise,
        now_ts=now_ts,
    )


# ---------- Mode-off / skip paths ----------
def test_mode_off_returns_skip():
    plan = _eval(current_soc=80, solar_w=3000, load_w=400,
                 cfg=_cfg(mode="off"))
    assert plan.action == "skip"
    assert "mode=off" in plan.reason


def test_test_mode_makes_decisions_but_caller_won_t_toggle():
    """Test mode is structurally identical to active at compute_plan
    level — the gate is server-side (server.py only calls
    kasa_client.set_state when mode=='active')."""
    plan = _eval(current_soc=80, solar_w=3000, load_w=400,
                 cfg=_cfg(mode="test"))
    assert plan.action == "on"  # decision computed exactly the same
    assert plan.mode == "test"


# ---------- ON gate: all conditions must hold ----------
def test_on_when_all_gates_pass():
    plan = _eval(current_soc=80, solar_w=3000, load_w=400,
                 predicted_sunrise=80.0, target=40.0)
    # surplus = 2600W >= car 1400 + buf 100; SOC 80 >= comfort_high 70;
    # predicted 80 >= target 40 + margin 5; → on.
    assert plan.action == "on"
    assert plan.surplus_w == pytest.approx(2600.0)


def test_off_when_soc_below_comfort_high():
    """High solar, low load, but SOC below comfort_high → don't divert
    (let the battery charge first)."""
    plan = _eval(current_soc=65, solar_w=3000, load_w=400)
    assert plan.action == "skip"  # In dead zone (between gates)
    assert "hysteresis dead zone" in plan.reason


def test_off_when_surplus_insufficient_for_car():
    """SOC high, but solar barely exceeds load — no real surplus."""
    plan = _eval(current_soc=80, solar_w=1500, load_w=400)
    # surplus = 1100W. car=1400, buf=100. ON needs >=1500; OFF triggers
    # when <1300. 1100 < 1300 → OFF fires (with the "net draining
    # battery" wording that applies cleanly when the plug is on; for
    # plug-off-and-no-surplus the outcome is right, the wording is
    # technically loose — acceptable for v1).
    assert plan.action == "off"


# ---------- OFF gate triggers ----------
def test_off_when_surplus_drains_battery():
    """Plug currently on, but solar drops — we're now net draining.
    OFF gate fires."""
    plan = _eval(current_soc=80, solar_w=200, load_w=1500)
    # surplus = -1300W < car 1400 - buf 100 = 1300 → off.
    assert plan.action == "off"
    assert "net draining battery" in plan.reason


def test_off_when_predicted_sunrise_below_target():
    """Even with surplus, if forecast says we won't make morning floor,
    refuse to divert."""
    plan = _eval(current_soc=80, solar_w=3000, load_w=400,
                 predicted_sunrise=42.0, target=40.0)
    # predicted 42 < target 40 + margin 5 → off.
    assert plan.action == "off"
    assert "overnight floor" in plan.reason


def test_off_when_soc_at_or_below_comfort_low():
    """Hard floor: SOC at comfort_low → always OFF regardless of surplus."""
    plan = _eval(current_soc=30, solar_w=5000, load_w=200,
                 cfg=_cfg(comfort_low_pct=30, comfort_high_pct=70))
    assert plan.action == "off"
    assert "comfort_low" in plan.reason


# ---------- Fail-closed safety paths ----------
def test_off_on_stale_telemetry():
    plan = _eval(current_soc=80, solar_w=3000, load_w=400,
                 telemetry_age=120)  # > MAX_TELEMETRY_AGE_S (90s)
    assert plan.action == "off"
    assert "stale telemetry" in plan.reason


def test_off_on_missing_telemetry():
    plan = _eval(current_soc=None, solar_w=3000, load_w=400)
    assert plan.action == "off"
    assert "missing telemetry" in plan.reason


def test_off_on_missing_forecast():
    plan = solar_charge.compute_plan(
        config=_cfg(), current_soc_pct=80,
        solar_w=3000, load_w=400, telemetry_age_s=10,
        target_sunrise_soc_pct=40,
        predicted_sunrise_soc_with_diversion=None,  # forecast unavailable
        predicted_sunrise_soc_baseline=None,
    )
    assert plan.action == "off"
    assert "forecast unavailable" in plan.reason


# ---------- Hysteresis dead zone ----------
def test_dead_zone_holds_state():
    """Surplus in the buffer zone (between car-buf and car+buf): skip
    (caller maintains current state)."""
    plan = _eval(current_soc=80, solar_w=1750, load_w=400)
    # surplus = 1350W. car=1400, buf=100.
    # ON needs surplus >= 1500. OFF needs surplus < 1300.
    # 1350 in (1300, 1500) → dead zone.
    assert plan.action == "skip"
    assert "hysteresis" in plan.reason


# ---------- gate_min_hold ----------
def test_min_hold_downgrades_flip_within_window():
    """compute_plan said 'on', but plug just toggled 10s ago — gate
    downgrades to skip so the EVSE doesn't cycle."""
    plan = _eval(current_soc=80, solar_w=3000, load_w=400, now_ts=1000)
    assert plan.action == "on"
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=990, min_hold_s=30,
        plug_state_before="off", now_ts=1000)
    assert gated.action == "skip"
    assert "min_hold not elapsed" in gated.reason


def test_min_hold_allows_flip_after_window():
    plan = _eval(current_soc=80, solar_w=3000, load_w=400, now_ts=1000)
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=960, min_hold_s=30,
        plug_state_before="off", now_ts=1000)
    # 40s elapsed >= 30s → flip allowed.
    assert gated.action == "on"


def test_min_hold_no_effect_when_state_matches():
    """When the desired action matches the current plug state, it's not
    a flip — no min_hold gate needed. Downgrade to skip with a
    'already on/off' explanation for log clarity."""
    plan = _eval(current_soc=80, solar_w=3000, load_w=400, now_ts=1000)
    assert plan.action == "on"
    gated = solar_charge.gate_min_hold(
        plan, last_toggle_ts=10, min_hold_s=30,
        plug_state_before="on", now_ts=1000)
    # Already on, no flip needed.
    assert gated.action == "skip"
    assert "already on" in gated.reason


# ---------- Config validation ----------
def test_config_validates_ranges():
    """Out-of-range values fall back to defaults; in-range stick."""
    cfg = solar_charge._validate_config({
        "mode": "active",
        "car_load_w": 99999,  # too high; falls back
        "comfort_high_pct": 50,  # in range; sticks
        "min_hold_s": 5,  # below 10; falls back
    })
    assert cfg["car_load_w"] == solar_charge.DEFAULT_CONFIG["car_load_w"]
    assert cfg["comfort_high_pct"] == 50
    assert cfg["min_hold_s"] == solar_charge.DEFAULT_CONFIG["min_hold_s"]


def test_config_rejects_inverted_comfort_bands():
    """comfort_low >= comfort_high would paint an unreachable state;
    both fall back to defaults."""
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
