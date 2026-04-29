"""Smart-charge controller: config validation + decision policy."""
from __future__ import annotations

import importlib
import time

import pytest


def _fresh(monkeypatch, tmp_path):
    """Reload smart_charge.py with a tmp config path."""
    monkeypatch.setenv("JACKERY_SMART_CHARGE_FILE", str(tmp_path / "sc.json"))
    import smart_charge
    importlib.reload(smart_charge)
    return smart_charge


def _flat_plan(rate=0.30):
    return {"type": "flat", "rate_per_kwh": rate, "currency": "USD"}


def _build_forecast(*, now_ts, sunset_h=2, night_h=10, sunrise_h=2,
                    night_predicted_soc=20.0, target_soc=25.0):
    """Synthesize a forecast that has:
        * `sunset_h` hours of remaining day (solar > 0)
        * `night_h` hours of dark (solar = 0), each carrying a
          predicted_soc that LINEARLY decays from current to
          night_predicted_soc by the last dark hour
        * `sunrise_h` hours of light (solar > 0) tomorrow
    Designed so the controller's sunrise-finder lands on the boundary
    between dark and the next light hour.
    """
    base = (int(now_ts) // 3600) * 3600
    forecast = []
    # Today's remaining sun
    for i in range(sunset_h):
        forecast.append({"ts": base + i * 3600, "solar_w": 200,
                         "load_w": 50, "predicted_soc": 50.0})
    # Night hours
    for i in range(night_h):
        ts = base + (sunset_h + i) * 3600
        soc = max(0, 50 - (50 - night_predicted_soc) * (i + 1) / night_h)
        forecast.append({"ts": ts, "solar_w": 0, "load_w": 50,
                         "predicted_soc": soc})
    # Tomorrow's sun
    for i in range(sunrise_h):
        ts = base + (sunset_h + night_h + i) * 3600
        forecast.append({"ts": ts, "solar_w": 300, "load_w": 50,
                         "predicted_soc": 50.0})
    return {"forecast": forecast}


def test_config_round_trip(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    assert sc.get_config()["mode"] == "off"
    saved = sc.set_config({"mode": "test", "target_sunrise_soc_pct": 30})
    assert saved["mode"] == "test"
    assert saved["target_sunrise_soc_pct"] == 30
    assert sc.get_config()["mode"] == "test"


def test_config_clamps_invalid(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    saved = sc.set_config({"mode": "wat", "target_sunrise_soc_pct": 999})
    assert saved["mode"] == "off"  # invalid mode → default
    assert saved["target_sunrise_soc_pct"] == 25  # invalid → default


def test_off_mode_short_circuits(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    plan = sc.compute_plan(
        config={"mode": "off"},
        current_soc_pct=20, forecast={"forecast": []},
        cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.action == "skip"
    assert "disabled" in plan.reason


def test_no_action_when_predicted_sunrise_meets_target(tmp_path, monkeypatch):
    """Forecast says we'll wake up at 30%, target is 25% — do nothing."""
    sc = _fresh(monkeypatch, tmp_path)
    now = int(time.time())
    fc = _build_forecast(now_ts=now, night_predicted_soc=30.0)
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25},
        current_soc_pct=50, forecast=fc,
        cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.action == "off"
    assert "no grid needed" in plan.reason
    assert plan.predicted_sunrise_soc_pct == 30.0


def test_charges_when_predicted_sunrise_below_target(tmp_path, monkeypatch):
    """Forecast says we'll wake up at 10%, target 25% — should plan to charge."""
    sc = _fresh(monkeypatch, tmp_path)
    now = int(time.time())
    fc = _build_forecast(now_ts=now, sunset_h=0, night_h=8,
                         night_predicted_soc=10.0)
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=20, forecast=fc,
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
    )
    assert plan.deficit_kwh > 0
    assert plan.window_start is not None
    assert plan.action in ("on", "off")  # depends on whether we're in the window
    # 15pp of 5040 Wh = 756 Wh ≈ 1 hour of charging needed
    assert plan.deficit_kwh < 1.0


def test_already_at_target_means_off(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    now = int(time.time())
    fc = _build_forecast(now_ts=now, sunset_h=0, night_h=8,
                         night_predicted_soc=10.0)
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25},
        current_soc_pct=80,  # already way above target
        forecast=fc, cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.action == "off"
    assert "already reached" in plan.reason


def test_test_mode_returns_plan_without_active_intent(tmp_path, monkeypatch):
    """In test mode the controller still computes a plan (so the UI can
    show what it WOULD do) but the server-side caller is responsible for
    NOT executing it. Plan persistence + execution control both live in
    the server layer (energy_db.record_smart_charge_decision)."""
    sc = _fresh(monkeypatch, tmp_path)
    now = int(time.time())
    fc = _build_forecast(now_ts=now, sunset_h=0, night_h=8,
                         night_predicted_soc=10.0)
    plan = sc.compute_plan(
        config={"mode": "test", "target_sunrise_soc_pct": 25},
        current_soc_pct=20, forecast=fc,
        cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.mode == "test"
    assert plan.action in ("on", "off")


def test_finds_sunrise_after_dark_run(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000
    # 2 dark hours → 1 daylight hour
    fc = [
        {"ts": base, "solar_w": 0, "predicted_soc": 30},
        {"ts": base + 3600, "solar_w": 0, "predicted_soc": 25},
        {"ts": base + 7200, "solar_w": 200, "predicted_soc": 25},
    ]
    assert sc._find_next_sunrise(fc) == base + 7200


def test_no_forecast_returns_off_with_reason(tmp_path, monkeypatch):
    sc = _fresh(monkeypatch, tmp_path)
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25},
        current_soc_pct=20, forecast={"forecast": []},
        cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.action == "off"
    assert "no forecast" in plan.reason.lower()
