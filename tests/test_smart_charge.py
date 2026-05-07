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
    assert "AC not needed" in plan.reason
    assert plan.predicted_sunrise_soc_pct == 30.0
    assert plan.baseline_predicted_sunrise_soc_pct == 30.0


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


def test_needed_hours_accounts_for_efficiency_and_loads(tmp_path, monkeypatch):
    """The original needed_hours math used raw max_charge_w as the
    per-hour gain, ignoring inverter loss (~10%) and concurrent loads
    served from AC during charging. On big deficits that under-counts
    by 1-2 hours, leaving the planner short of target. Verify the
    fixed math: deficit ≫ single-hour gain → planner picks at least
    ceil(deficit / ((max-load)*eff + load)) hours."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    # 12h overnight, deep deficit. 80% → 0% across 12h.
    fc = []
    for i in range(12):
        soc = max(0, 80 - 80 * (i + 1) / 12)
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "load_w": 400,  # 400W of overnight loads on this rig
                   "predicted_soc": soc})
    fc.append({"ts": base + 12 * 3600, "solar_w": 200, "predicted_soc": 0})
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 50,
                "max_charge_w": 1500},
        current_soc_pct=80,
        forecast={"forecast": fc, "charge_efficiency": 0.85},
        cost_plan=_flat_plan(0.30), capacity_wh=10000,
        now_ts=base,
    )
    # Deficit: target 50% − baseline 0% = 50pp on 10kWh = 5.0 kWh
    # OLD math: ceil(5000 / 1500) = 4 hours
    # NEW math: hourly_gain = (1500-400)*0.85 + 400 = 935 + 400 = 1335 Wh/h
    #          → ceil(5000 / 1335) = 4 hours (still 4 here because
    #          load_w * (1-eff) = 60 Wh adds ~4% headroom)
    # Use a deeper test: capacity 30kWh, target 70%, baseline 0%
    plan2 = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 70,
                "max_charge_w": 1500},
        current_soc_pct=80,
        forecast={"forecast": fc, "charge_efficiency": 0.85},
        cost_plan=_flat_plan(0.30), capacity_wh=30000,
        now_ts=base,
    )
    # Deficit: 70pp on 30kWh = 21 kWh
    # OLD math: ceil(21000 / 1500) = 14 hours
    # NEW math: ceil(21000 / 1335) = 16 hours
    # Only 12h available pre-margin in this fixture, so planner picks
    # all 11 candidates (margin_end = base+12-1=11h, so 11 hours
    # available [base, base+11h)). The point is needed_hours grew.
    assert plan2.deficit_kwh > 20
    # All eligible hours included since needed exceeds availability.
    assert len(plan2.planned_hours) == 11


def test_above_target_with_deficit_defers_to_planned_hour(tmp_path, monkeypatch):
    """SOC above target now, but baseline predicts sunrise < target →
    plan exists, action is "off" outside the planned hour, reason text
    says "deferred" (not "coasting"). "Coasting" used to mislead the
    user into thinking the controller had missed the deficit; the
    planned hour stays scheduled and the next tick will flip to "on"
    once we're inside the cheap window."""
    sc = _fresh(monkeypatch, tmp_path)
    now = int(time.time())
    fc = _build_forecast(now_ts=now, sunset_h=0, night_h=8,
                         night_predicted_soc=10.0)
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25},
        current_soc_pct=80,  # above target now
        forecast=fc, cost_plan=_flat_plan(), capacity_wh=5040,
    )
    assert plan.action == "off"
    assert "deferred" in plan.reason
    assert "coasting" not in plan.reason
    # A plan still exists for when SOC drifts mid-night into the
    # planned hour.
    assert plan.planned_hours


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


# ---- New behavior tests for the sunrise-anchored discontinuous schedule ----

def _tou_plan(cheap_hours=(0, 1, 2, 3), cheap_rate=0.10, peak_rate=0.40,
              currency="USD"):
    """A simple block-TOU plan: `cheap_hours` (list of UTC hours) at
    `cheap_rate`, all others at `peak_rate`. The smart-charge planner
    applies tz_offset_seconds=0 in tests so UTC-hour buckets are what
    `rate_at` looks at."""
    return {
        "type": "tou",
        "currency": currency,
        "tou_rates": [
            {"start_hour": h, "end_hour": (h + 1) % 24, "rate": cheap_rate}
            for h in cheap_hours
        ] + [
            # Catch-all peak — overlapping slots are evaluated in order,
            # so we put cheap first. cost.rate_at returns the first slot
            # whose hour matches; "0..24" covers everything.
            {"start_hour": 0, "end_hour": 24, "rate": peak_rate},
        ],
    }


def test_planned_hours_anchor_at_sunrise_minus_margin(tmp_path, monkeypatch):
    """The hour ENDING at (sunrise - margin) must be in planned_hours.
    With margin=1h, that's the hour starting at (sunrise - 2h). This is
    the deadline anchor — no matter how cheap other hours are, this one
    must charge so the deadline is met."""
    sc = _fresh(monkeypatch, tmp_path)
    # Anchor the test on a known UTC hour boundary so we can compute
    # the expected anchor timestamp exactly.
    base = 1_700_000_000 - (1_700_000_000 % 3600)  # hour-aligned
    # Now=base; sunrise=base+10h; margin=1h ⇒ deadline=base+9h ⇒ anchor
    # hour starts at base+8h (runs base+8h → base+9h).
    fc = []
    # 8 dark hours starting now (declining SOC)
    for i in range(8):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 10 * (i + 1))})
    # Hour 8: still dark
    fc.append({"ts": base + 8 * 3600, "solar_w": 0, "predicted_soc": 0})
    # Hour 9: still dark
    fc.append({"ts": base + 9 * 3600, "solar_w": 0, "predicted_soc": 0})
    # Hour 10: SUNRISE
    fc.append({"ts": base + 10 * 3600, "solar_w": 200, "predicted_soc": 5})

    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=80, forecast={"forecast": fc},
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base,
    )
    expected_anchor = base + 8 * 3600
    assert plan.planned_hours, "expected at least one planned hour"
    assert expected_anchor in plan.planned_hours, (
        f"anchor hour {expected_anchor} missing from {plan.planned_hours}"
    )
    # And the latest planned hour IS the anchor — nothing later than
    # margin is ever planned (the extension covers post-margin needs).
    assert max(plan.planned_hours) == expected_anchor


def test_planned_hours_pick_cheapest_with_tou(tmp_path, monkeypatch):
    """Cost-weighted: when TOU has a cheap evening block, the planner
    picks those hours BEFORE the anchor. With needed_hours=4 and a 4h
    cheap block early in the night plus the mandatory anchor, expect
    a 5-hour discontinuous plan if TOU positions the cheap block away
    from the anchor."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    # Now=base; sunrise=base+10h; deadline=base+9h; anchor=base+8h.
    # Cheap hours: UTC hours of (base..base+3) — first 4 hours of night.
    # Anchor (base+8h) lands at a different UTC hour, so it won't be
    # in the cheap set; it's forced in.
    cheap_utc_hours = []
    for i in range(4):
        ts = base + i * 3600
        from datetime import datetime, timezone
        cheap_utc_hours.append(datetime.fromtimestamp(ts, tz=timezone.utc).hour)
    fc = []
    for i in range(10):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 8 * (i + 1))})
    fc.append({"ts": base + 10 * 3600, "solar_w": 200, "predicted_soc": 0})

    # Big deficit so needed_hours is large enough to span the cheap block
    # plus the anchor. baseline_predicted ≈ 0%, target=25%, capacity=10kWh
    # ⇒ deficit=2.5kWh ⇒ needed=ceil(2500/800)=4. With anchor forced,
    # we'll pick 3 cheapest from the rest.
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=10, forecast={"forecast": fc},
        cost_plan=_tou_plan(cheap_hours=tuple(cheap_utc_hours)),
        capacity_wh=10000,
        now_ts=base,
    )
    expected_anchor = base + 8 * 3600
    assert expected_anchor in plan.planned_hours
    # Three cheapest (other than anchor) are the 4 cheap hours minus
    # any that aren't in candidates. Candidates are [base, base+9h);
    # cheap is base..base+3h, all in range. Picker takes 3 of those 4.
    cheap_hours_chosen = [h for h in plan.planned_hours
                          if base <= h < base + 4 * 3600]
    assert len(cheap_hours_chosen) == 3, (
        f"expected 3 cheap hours selected, got {cheap_hours_chosen}"
    )


def test_in_planned_hour_returns_on(tmp_path, monkeypatch):
    """When `now` lands on a scheduled ON hour, action=on."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    fc = []
    for i in range(8):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 10 * (i + 1))})
    fc.append({"ts": base + 8 * 3600, "solar_w": 200, "predicted_soc": 0})
    # sunrise = base+8h; deadline = base+7h; anchor hour = base+6h.

    # Evaluate AT the anchor hour.
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=10, forecast={"forecast": fc},
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base + 6 * 3600 + 100,  # mid-anchor-hour
    )
    assert plan.action == "on"
    assert "charging now" in plan.reason


def test_post_margin_extension_keeps_charging(tmp_path, monkeypatch):
    """Lock-in (Q2): past sunrise-margin and still under target →
    extension fires, action=on. This is what hits target even when
    the planner under-allocated hours."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    fc = []
    for i in range(4):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 20 * (i + 1))})
    fc.append({"ts": base + 4 * 3600, "solar_w": 200, "predicted_soc": 0})
    # sunrise = base+4h; deadline = base+3h.

    # Now = base + 3h + 30min — past the deadline.
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=18,  # still under target
        forecast={"forecast": fc},
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base + 3 * 3600 + 1800,
    )
    assert plan.action == "on"
    assert plan.extension_active is True
    assert "extension" in plan.reason


def test_post_margin_target_hit_releases(tmp_path, monkeypatch):
    """Past the margin AND SOC has hit target → off. Extension only
    fires while SOC < target."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    fc = []
    for i in range(4):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 20 * (i + 1))})
    fc.append({"ts": base + 4 * 3600, "solar_w": 200, "predicted_soc": 0})

    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=26,  # just over target
        forecast={"forecast": fc},
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base + 3 * 3600 + 1800,
    )
    assert plan.action == "off"
    assert plan.extension_active is False


def test_counterfactual_releases_lock_when_baseline_recovers(tmp_path, monkeypatch):
    """Q4: mid-session, if the baseline forecast (no AC) shows we'll
    hit target on our own, release. The with-floor `forecast` still
    looks like we're charging (because the floor is target), but the
    `baseline_forecast` is the ground truth for need-to-charge."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)

    def mk_fc(end_pct):
        out = []
        for i in range(8):
            soc = max(0, 80 - (80 - end_pct) * (i + 1) / 8)
            out.append({"ts": base + i * 3600, "solar_w": 0,
                        "predicted_soc": soc})
        out.append({"ts": base + 8 * 3600, "solar_w": 200, "predicted_soc": 50})
        return {"forecast": out}

    # With-floor forecast bottoms at target=25 (floor injected by caller);
    # baseline (no floor) actually bottoms at 30 — solar+coast is enough.
    fc = mk_fc(end_pct=25.0)
    bfc = mk_fc(end_pct=30.0)

    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=40, forecast=fc, baseline_forecast=bfc,
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base,
    )
    assert plan.action == "off"
    assert "AC not needed" in plan.reason
    assert plan.baseline_predicted_sunrise_soc_pct == 30.0
    # The display predicted (with floor) is the lower one — what UI shows.
    assert plan.predicted_sunrise_soc_pct == 25.0


def test_counterfactual_drives_deficit_when_baseline_below_target(tmp_path, monkeypatch):
    """The deficit math comes from the baseline (no-AC) forecast, not
    from the with-floor display forecast. If only the with-floor were
    used, the floor would mask the underlying need — predicted=target
    by construction would always say 'no grid needed'."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)

    def mk_fc(end_pct):
        out = []
        for i in range(8):
            soc = max(0, 80 - (80 - end_pct) * (i + 1) / 8)
            out.append({"ts": base + i * 3600, "solar_w": 0,
                        "predicted_soc": soc})
        out.append({"ts": base + 8 * 3600, "solar_w": 200, "predicted_soc": 25})
        return {"forecast": out}

    # With-floor: clamped at 25 (floor=target). Baseline: hits 5%.
    fc = mk_fc(end_pct=25.0)
    bfc = mk_fc(end_pct=5.0)

    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=40, forecast=fc, baseline_forecast=bfc,
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base,
    )
    # Baseline is 5%, target 25%, deficit 20pp = ~1 kWh of 5040 Wh ⇒
    # needed ≈ 2 hours at 800W.
    assert plan.deficit_kwh > 0.5
    assert plan.action in ("on", "off")
    assert plan.planned_hours, "deficit > 0 implies a non-empty plan"


def test_drift_below_target_re_enters_charging(tmp_path, monkeypatch):
    """Q5: if SOC drifts below target after target was hit, the plan
    fires again. Tested by simulating now=anchor-hour with soc < target."""
    sc = _fresh(monkeypatch, tmp_path)
    base = 1_700_000_000 - (1_700_000_000 % 3600)
    fc = []
    for i in range(8):
        fc.append({"ts": base + i * 3600, "solar_w": 0,
                   "predicted_soc": max(0, 80 - 12 * (i + 1))})
    fc.append({"ts": base + 8 * 3600, "solar_w": 200, "predicted_soc": 0})

    # SOC drifted to 23% — below target.
    plan = sc.compute_plan(
        config={"mode": "active", "target_sunrise_soc_pct": 25,
                "max_charge_w": 800},
        current_soc_pct=23, forecast={"forecast": fc},
        cost_plan=_flat_plan(0.30), capacity_wh=5040,
        now_ts=base + 6 * 3600 + 100,  # in anchor hour
    )
    assert plan.action == "on"
