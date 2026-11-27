"""V2 dashboard overview: headline states, plan summary, rates and activity."""

from __future__ import annotations

import itertools

import ui_overview

TZ = -8 * 3600
# 2026-11-20 14:35 PST. Winter rates: off-peak until 15:00, peak 16-21.
NOW = 1795214100.0
DAY_START = NOW - (14 * 3600 + 35 * 60)
MIDNIGHT = DAY_START + 86400
SUNRISE = MIDNIGHT + 7 * 3600
PLAN = {"type": "tou", "tou_rates": [
    {"start_hour": 0, "end_hour": 15, "rate": 0.25},
    {"start_hour": 15, "end_hour": 16, "rate": 0.41},
    {"start_hour": 16, "end_hour": 21, "rate": 0.43},
    {"start_hour": 21, "end_hour": 24, "rate": 0.41},
]}
CONFIG = {"mode": "active", "target_sunrise_soc_pct": 50, "reserve_soc_pct": 10.0,
          "kasa_device_host": "192.168.3.109", "ac_supply_limit_w": 1750.0,
          "lg_interlock_enabled": True, "max_charge_w": 939.0}


def _decision(**over):
    return {"decided_at": int(NOW) - 20, "action": "off", "executed": True, "reason": "waiting",
            "current_soc_pct": 50.7, "predicted_sunrise_soc_pct": 3.0,
            "target_sunrise_soc_pct": 50.0, "sunrise_ts": SUNRISE, **over}


def _snapshot(**over):
    hourly = [{"start": MIDNIGHT + h * 3600, "end": MIDNIGHT + (h + 1) * 3600} for h in range(7)]
    return {"capacity_wh": 30240, "system_soc_pct": 50.7, "tz_offset_seconds": TZ,
            "charge_rate": {"value": 939.0}, "model": {"charge_efficiency": 0.871},
            "planned_intervals": hourly,
            "schedule_projection": {"scheduled_sunrise_soc_pct": 52.4, "scheduled_min_soc_pct": 34.7,
                                    "reserve_soc_pct": 10.0, "plan_feasible": True, "warnings": []},
            "lg_protection": {"blocked_now": False, "appliances": []},
            "charge_verification": {"status": "idle"},
            "baseline_forecast": [], **over}


def _overview(decision=None, snapshot=None, config=CONFIG, **kwargs):
    return ui_overview.build_overview(
        now=NOW, device_sn="SN", decision=decision or _decision(), snapshot=snapshot or _snapshot(),
        config=config, cost_plan=PLAN, tz_offset_seconds=TZ, day_start=DAY_START, **kwargs)


def test_scheduled_headline_names_next_start_and_off_peak_rate():
    out = _overview()
    assert out["headline"]["state"] == "scheduled"
    assert out["headline"]["tone"] == "ok"
    assert out["headline"]["title"] == "Not charging. Next charge starts at 12 AM."
    assert "dip to about 35% and be at 52% by sunrise at 7 AM" in out["headline"]["detail"]
    assert "Every planned hour is at the $0.25 off-peak rate." in out["headline"]["detail"]


def test_hourly_slots_merge_into_one_block_with_cost_estimate():
    plan = _overview()["plan"]
    assert plan["blocks"] == [{"start": MIDNIGHT, "end": SUNRISE}]
    assert plan["minutes"] == 420
    assert plan["est_kwh"] == 6.6          # 7 h at 939 W
    assert plan["est_cost"] == 1.64        # all at $0.25


def test_solar_enough_when_nothing_is_planned():
    out = _overview(decision=_decision(predicted_sunrise_soc_pct=58.0),
                    snapshot=_snapshot(planned_intervals=[]))
    assert out["headline"]["state"] == "solar_enough"
    assert out["headline"]["title"] == "No grid charging needed."
    assert "about 58% by sunrise" in out["headline"]["detail"]
    assert out["plan"]["blocks"] == [] and out["plan"]["est_cost"] == 0


def test_laundry_hold_names_the_appliance_and_flags_reserve_risk():
    lg = {"blocked_now": True, "expected_finish_ts": NOW + 1800, "charge_not_before_ts": NOW + 2100,
          "soc_at_finish_pct": 8.5, "appliances": [{"device_id": "d1", "activity": "active"},
                                                   {"device_id": "w1", "activity": "off"}]}
    proj = {"scheduled_sunrise_soc_pct": 50.4, "scheduled_min_soc_pct": 8.3, "reserve_soc_pct": 10.0,
            "plan_feasible": False, "warnings": ["Reserve 10% cannot be maintained by this plan"]}
    out = _overview(snapshot=_snapshot(lg_protection=lg, schedule_projection=proj),
                    appliance_aliases={"d1": "Dryer", "w1": "Washer"})
    assert out["headline"]["state"] == "hold"
    assert out["headline"]["tone"] == "alert"
    assert out["headline"]["title"] == "Charging is on hold while the dryer runs."
    assert "around 3:05 PM; charging can resume at 3:10 PM" in out["headline"]["detail"]
    assert "below the 10% reserve" in out["headline"]["detail"]
    assert out["laundry"]["blocked"] is True and out["laundry"]["names"] == "the dryer"


def test_laundry_hold_without_a_finish_estimate_still_explains_the_risk():
    lg = {"blocked_now": True, "expected_finish_ts": None, "charge_not_before_ts": None,
          "appliances": [{"device_id": "d1", "activity": "active"}, {"device_id": "w1", "activity": "active"}]}
    proj = {"scheduled_sunrise_soc_pct": 0.0, "scheduled_min_soc_pct": 0.0, "reserve_soc_pct": 10.0,
            "plan_feasible": False, "warnings": ["Reserve 10% cannot be maintained by this plan"]}
    head = _overview(snapshot=_snapshot(lg_protection=lg, schedule_projection=proj, planned_intervals=[]),
                     appliance_aliases={"d1": "Dryer", "w1": "Washer"})["headline"]
    assert head["title"] == "Charging is on hold while the dryer and washer run."
    assert head["detail"] == ("The battery is on course to fall below the 10% reserve before charging "
                              "can resume. Pause the appliance or reduce other loads.")
    assert head["tone"] == "alert"


def test_charging_headline_distinguishes_cheap_from_peak_charging():
    on = _decision(action="on", executed=True)
    cheap = _overview(decision=on, snapshot=_snapshot(
        planned_intervals=[{"start": NOW - 600, "end": NOW + 1500}]))
    assert cheap["headline"]["title"] == "Charging from the grid at $0.25 until 3 PM."
    peak = ui_overview.build_overview(
        now=NOW + 2 * 3600, device_sn="SN", decision=on,
        snapshot=_snapshot(planned_intervals=[{"start": NOW + 7000, "end": NOW + 9000}]),
        config=CONFIG, cost_plan=PLAN, tz_offset_seconds=TZ, day_start=DAY_START)
    assert peak["headline"]["state"] == "charging_peak"
    assert peak["headline"]["title"] == "Charging now at the $0.43 rate to protect the 10% reserve."


def test_plug_on_without_charging_outranks_everything():
    out = _overview(decision=_decision(action="on"),
                    snapshot=_snapshot(charge_verification={"status": "not_charging"}))
    assert out["headline"]["state"] == "not_charging"
    assert out["headline"]["tone"] == "alert"


def test_inactive_mode_hides_the_stale_plan():
    out = _overview(config={**CONFIG, "mode": "off"})
    assert out["headline"]["state"] == "manual"
    assert out["plan"]["blocks"] == [] and out["projection"] == []


def test_rate_segments_cover_history_and_next_day_with_tiers():
    rates = _overview()["rates"]
    assert rates[0]["start"] == NOW - 6 * 3600 and rates[-1]["end"] == NOW + 24 * 3600
    assert [(s["rate"], s["tier"]) for s in rates] == [
        (0.25, "low"), (0.41, "mid"), (0.43, "peak"), (0.41, "mid"), (0.25, "low")]
    assert all(a["end"] == b["start"] for a, b in itertools.pairwise(rates))


def test_grid_today_splits_purchases_by_rate_and_lists_empty_tiers():
    rows = [{"ts": DAY_START + 13 * 3600, "ac_input_wh": 1000.0},
            {"ts": DAY_START + 17 * 3600, "ac_input_wh": 500.0},
            {"ts": DAY_START - 3600, "ac_input_wh": 9999.0}]     # yesterday: ignored
    today = _overview(hourly_rows=rows)["grid_today"]
    assert today["kwh"] == 1.5 and today["cost"] == round(0.25 + 0.215, 2)
    assert [(t["tier"], t["kwh"]) for t in today["tiers"]] == [("low", 1.0), ("mid", 0.0), ("peak", 0.5)]


def test_projection_adds_planned_charging_to_the_no_charge_forecast():
    forecast = [{"ts": NOW + (i + 1) * 3600, "duration_h": 1.0, "net_w": -500.0} for i in range(17)]
    points = _overview(snapshot=_snapshot(baseline_forecast=forecast))["projection"]
    assert points[0] == {"ts": NOW, "soc": 50.7}
    by_ts = {p["ts"]: p["soc"] for p in points}
    # 9 h of pure drain at 500 W, then partial hours that overlap the midnight block.
    assert by_ts[NOW + 9 * 3600] == round(50.7 - 9 * 500 / 30240 * 100, 1)
    assert by_ts[NOW + 17 * 3600] > by_ts[NOW + 10 * 3600]


def test_charge_estimate_includes_home_use_the_grid_carries():
    forecast = [{"ts": MIDNIGHT + (h + 1) * 3600, "duration_h": 1.0, "net_w": -500.0, "output_w": 500.0}
                for h in range(-12, 8)]
    plain = _overview(snapshot=_snapshot(baseline_forecast=forecast))["plan"]
    bypass = _overview(snapshot=_snapshot(baseline_forecast=forecast),
                       config={**CONFIG, "ac_input_excludes_load": True})["plan"]
    assert plain["est_kwh"] == round(7 * 0.939, 1)
    assert bypass["est_kwh"] == round(7 * (0.939 + 0.5), 1)
    assert bypass["est_cost"] > plain["est_cost"]


def test_projection_follows_the_validated_path_then_the_forecast():
    """The chart line must agree with the sunrise number, which comes from the
    scheduler's own trajectory, not from a re-integration of the hourly rows."""
    forecast = [{"ts": NOW + (i + 1) * 3600, "duration_h": 1.0, "net_w": -500.0} for i in range(17)]
    path = [{"ts": NOW + 3600, "soc": 49.0}, {"ts": NOW + 2 * 3600, "soc": 58.0},
            {"ts": NOW + 3 * 3600, "soc": 52.4}]
    snapshot = _snapshot(baseline_forecast=forecast)
    snapshot["schedule_projection"]["scheduled_trajectory"] = path
    points = _overview(snapshot=snapshot)["projection"]
    assert points[:4] == [{"ts": NOW, "soc": 50.7}, *path]
    # Past the validated path the hourly forecast carries on from where it ended.
    assert points[4] == {"ts": NOW + 4 * 3600, "soc": round(52.4 - 500 / 30240 * 100, 1)}
    assert len(points) == 1 + 17


def test_activity_reports_changes_not_ticks():
    def row(offset, action, reason, soc):
        return {"decided_at": int(NOW) + offset, "action": action, "executed": True,
                "reason": reason, "current_soc_pct": soc}
    rows = [row(0, "off", "LG 240 V interlock: Dryer is ON", 46.8),
            row(30, "off", "LG 240 V interlock: Dryer is ON", 46.7),
            row(10800, "off", "waiting for next validated charge interval", 39.3),
            row(12600, "on", "overnight-first plan", 41.3),
            row(12630, "on", "overnight-first plan", 41.4),
            row(17100, "off", "waiting", 50.7)]
    events = ui_overview.activity(rows, TZ)
    assert [e["kind"] for e in events] == ["charge_end", "charge_start", "hold_end"]
    assert events[0]["detail"] == "75 minutes · 41% to 51%"
    assert "180 minutes on hold" in events[2]["detail"]


def test_missing_decision_still_returns_a_page():
    out = ui_overview.build_overview(
        now=NOW, device_sn="SN", decision=None, snapshot=None, config={"mode": "off"},
        cost_plan={"type": "flat", "rate_per_kwh": 0.3}, tz_offset_seconds=TZ, day_start=DAY_START)
    assert out["headline"]["state"] == "manual"
    assert out["grid_today"] == {"kwh": 0.0, "cost": 0.0, "tiers": [{"rate": 0.3, "tier": "low", "kwh": 0.0, "cost": 0.0}]}
    assert len(out["rates"]) == 1


def test_charging_power_shown_is_the_learned_one_the_planner_used():
    snapshot = _snapshot()
    snapshot["config"] = {"max_charge_w": 972.0}
    assert _overview(snapshot=snapshot, config={**CONFIG, "max_charge_w": 1000})["config"]["max_charge_w"] == 972.0
    snapshot["config"] = {}
    assert _overview(snapshot=snapshot, config={**CONFIG, "max_charge_w": 1000})["config"]["max_charge_w"] == 1000


def test_a_charge_waiting_on_weak_sunlight_is_passed_to_the_page():
    snapshot = _snapshot()
    snapshot["charge_verification"] = {"status": "pending", "waiting_on": "weak_sunlight", "on_since": NOW - 600}
    assert _overview(snapshot=snapshot)["verification"] == {"status": "pending", "waiting_on": "weak_sunlight"}
