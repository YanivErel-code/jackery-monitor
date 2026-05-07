"""Forecast prediction storage + accuracy join."""
from __future__ import annotations

import time

import pytest

from energy_db import EnergyDB


@pytest.fixture()
def db(tmp_path):
    return EnergyDB(str(tmp_path / "energy.db"))


def test_record_forecast_collapses_within_same_hour(db):
    sn = "SN-PRED"
    now = time.time()
    preds = [{"ts": int(now) + i * 3600, "predicted_soc": 50 + i}
             for i in range(5)]
    n1 = db.record_forecast(sn, now, preds)
    # Same hour (different made_at second) → INSERT OR REPLACE collapses.
    n2 = db.record_forecast(sn, now + 60, preds)
    assert n1 == 5
    assert n2 == 5  # same five rows, just overwritten


def test_record_forecast_skips_invalid(db):
    sn = "SN-PRED"
    preds = [
        {"ts": None, "predicted_soc": 50},
        {"ts": 1_700_000_000, "predicted_soc": None},
        {"ts": 1_700_000_000 + 3600, "predicted_soc": 60},  # only this is valid
    ]
    n = db.record_forecast(sn, time.time(), preds)
    assert n == 1


def test_prediction_accuracy_returns_matched_pairs(db):
    sn = "SN-ACC"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    # Made a forecast 2 hours ago for "1 hour ago" target. Now record some
    # actual SOC samples around that target, and verify the join returns
    # the predicted/actual pair.
    now = time.time()
    target = int((now - 3600) // 3600) * 3600  # 1h ago, hour-aligned
    made = int((now - 2 * 3600) // 3600) * 3600
    db.record_forecast(sn, made, [{"ts": target, "predicted_soc": 70.0}])
    # Drop in some actual samples within the 30-min window around `target`.
    # We need the trapezoidal integration to fire — so push two samples.
    db.record(sn, target - 60, input_w=0, output_w=0,
              battery_pct=65, solar_w=0)
    db.record(sn, target + 60, input_w=0, output_w=0,
              battery_pct=65, solar_w=0)
    out = db.prediction_accuracy(sn)
    assert len(out) == 1
    row = out[0]
    assert row["target"] == target
    assert row["predicted_soc"] == 70.0
    assert row["actual_soc"] == 65.0
    assert row["error"] == 5.0


def test_prediction_accuracy_skips_predictions_without_actuals(db):
    sn = "SN-NOACT"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    target = int(time.time() - 3600) // 3600 * 3600
    db.record_forecast(sn, time.time() - 7200,
                       [{"ts": target, "predicted_soc": 70.0}])
    # No actual samples → no matched pairs returned.
    out = db.prediction_accuracy(sn)
    assert out == []


def test_prediction_accuracy_excludes_future_targets(db):
    sn = "SN-FUT"
    target = int(time.time() + 24 * 3600)  # 1 day in the future
    db.record_forecast(sn, time.time(), [{"ts": target, "predicted_soc": 70.0}])
    out = db.prediction_accuracy(sn)
    assert out == []


def test_list_daily_summary_filters_by_date_not_updated_at(db):
    """Regression: filter must use the row's `date` column, not the
    backfill-bumped `updated_at`. Otherwise a 'days=3' query returns
    every row that was touched recently regardless of which day it
    covers — defeating the Forecast tab's days-window dropdown."""
    sn = "SN-DAYS-FILTER"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc)

    # Seed 10 days of rows, dates today-9 .. today.
    for offset in range(10):
        d = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        db.upsert_daily_summary(
            device_sn=sn, local_date=d,
            sunset_ts=int(today.timestamp()) - offset * 86400,
            sunrise_ts=int(today.timestamp()) - offset * 86400 + 3600,
            predicted_sunset_soc_pct=80.0 - offset,
        )

    # All rows just got their updated_at bumped to "now". A days=3
    # query MUST limit by the row's date column; if it filtered by
    # updated_at it would return all 10 rows.
    rows3 = db.list_daily_summary(sn, days=3)
    assert len(rows3) <= 4, f"expected ≤4 rows for days=3, got {len(rows3)}"
    rows30 = db.list_daily_summary(sn, days=30)
    assert len(rows30) == 10, f"expected 10 rows for days=30, got {len(rows30)}"


def test_backfill_daily_actuals_fills_missing_actuals(db):
    """The single-shot tick writes only today's row, but each row's
    sunrise_ts falls on the FOLLOWING calendar day — so today's tick
    can never back-fill yesterday's sunrise. backfill_daily_actuals
    walks recent rows and fills any null actuals whose `*_ts` has
    aged into the past with samples available."""
    sn = "SN-BACKFILL"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    now = int(time.time())
    sunset_ts = now - 3 * 3600       # 3h ago, in the past
    sunrise_ts = now - 1 * 3600      # 1h ago, in the past
    future_sunrise_ts = now + 6 * 3600  # 6h ahead, still future

    # Row 1: sunset + sunrise both in the past; both actuals null at first.
    db.upsert_daily_summary(
        device_sn=sn, local_date="2026-05-04",
        sunset_ts=sunset_ts, sunrise_ts=sunrise_ts,
        predicted_sunset_soc_pct=80.0,
        predicted_sunrise_soc_pct=45.0,
    )
    # Row 2: sunset past, sunrise still in the future.
    db.upsert_daily_summary(
        device_sn=sn, local_date="2026-05-05",
        sunset_ts=sunset_ts, sunrise_ts=future_sunrise_ts,
        predicted_sunset_soc_pct=78.0,
        predicted_sunrise_soc_pct=42.0,
    )
    # Seed samples around each past ts so the join finds an actual.
    for ts, soc in [(sunset_ts - 60, 76), (sunset_ts + 60, 76),
                    (sunrise_ts - 60, 49), (sunrise_ts + 60, 49)]:
        db.record(sn, ts, input_w=0, output_w=0, battery_pct=soc, solar_w=0)

    filled = db.backfill_daily_actuals(sn, days=14)
    # Row 1: sunset + sunrise = 2 fills. Row 2: sunset only = 1 fill.
    assert filled == 3

    rows = {r["date"]: r for r in db.list_daily_summary(sn, days=14)}
    assert rows["2026-05-04"]["actual_sunset_soc_pct"] == 76.0
    assert rows["2026-05-04"]["actual_sunrise_soc_pct"] == 49.0
    assert rows["2026-05-05"]["actual_sunset_soc_pct"] == 76.0
    # Future sunrise still null — not yet eligible for back-fill.
    assert rows["2026-05-05"]["actual_sunrise_soc_pct"] is None

    # Idempotent: rerun does nothing because actuals are already filled.
    again = db.backfill_daily_actuals(sn, days=14)
    assert again == 0


def test_smart_charge_analytics_includes_mode_and_reason(db):
    """The DB persists `mode` + `reason` on every decision, but the
    analytics SELECT was missing them — so the advisor's decisions
    table showed mode=None and reason=None on every row, hiding
    whether OFF was a 'pred>target skip' vs a 'test mode skip' vs
    anything else. Confirmed by advisor flag on 2026-05-05T15:50."""
    sn = "SN-DECISIONS"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    now = int(time.time())
    sunrise = now - 86400  # yesterday's sunrise (in the past)

    db.record_smart_charge_decision(
        device_sn=sn,
        plan={
            "decided_at": sunrise - 3600,
            "mode": "test",
            "action": "off",
            "reason": "predicted SOC 60% >= target 35%; coasting",
            "current_soc_pct": 65.0,
            "predicted_sunrise_soc_pct": 60.0,
            "target_sunrise_soc_pct": 35.0,
            "sunrise_ts": sunrise,
            # Baseline (counterfactual no-AC) sunrise SOC must round-trip
            # through the DB so the advisor can distinguish floor-clamp
            # from structural pessimism on historic rows.
            "baseline_predicted_sunrise_soc_pct": 42.5,
        },
        executed=False,
    )
    # Need an actual SOC reading near sunrise so the analytics row is
    # included (it gates on main_soc IS NOT NULL).
    db.record(sn, sunrise - 60, input_w=0, output_w=0,
              battery_pct=58, solar_w=0)
    db.record(sn, sunrise + 60, input_w=0, output_w=0,
              battery_pct=58, solar_w=0)

    rows = db.smart_charge_analytics(sn, days=2)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "test"
    assert row["action"] == "off"
    assert "coasting" in row["reason"]
    assert row["baseline_predicted_sunrise_soc_pct"] == 42.5

    # Also round-trips through list_smart_charge_decisions (the path the
    # advisor bundle reads from).
    listed = db.list_smart_charge_decisions(sn, limit=10)
    assert len(listed) == 1
    assert listed[0]["baseline_predicted_sunrise_soc_pct"] == 42.5


def test_prediction_accuracy_capacity_weights_actual_soc(db):
    """When capacity hints are passed, the actual SOC is the capacity-
    weighted system SOC (main + packs at target ts), so it can be compared
    apples-to-apples with the predicted (which is system-weighted)."""
    sn = "SN-SYSWEIGHT"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    now = time.time()
    target = int((now - 3600) // 3600) * 3600  # 1h ago, hour-aligned
    made = int((now - 2 * 3600) // 3600) * 3600

    # Predicted system SOC was 88. Main hits 100, packs at 76 — main-only
    # comparison would say "actual 100, error 12pp" but the truth is the
    # system was at exactly the predicted 88 (packs dragging the average
    # down). With capacity weighting, error should collapse near zero.
    db.record_forecast(sn, made, [{"ts": target, "predicted_soc": 88.0}])
    db.record(sn, target - 60, input_w=0, output_w=0, battery_pct=100, solar_w=0)
    db.record(sn, target + 60, input_w=0, output_w=0, battery_pct=100, solar_w=0)
    db.record_battery_packs(sn, [
        {"deviceSn": "PACK-A", "deviceOrder": 0, "rb": 76},
        {"deviceSn": "PACK-B", "deviceOrder": 1, "rb": 76},
    ], ts=target)

    # Without capacity hints: legacy main-only behavior.
    main_only = db.prediction_accuracy(sn)
    assert len(main_only) == 1
    assert main_only[0]["actual_soc"] == 100.0
    assert main_only[0]["error"] == 12.0

    # With capacity hints: capacity-weighted (5040 main + 2x5040 packs):
    # (100*5040 + 76*5040 + 76*5040) / (3*5040) = 84.0
    weighted = db.prediction_accuracy(
        sn, main_capacity_wh=5040, pack_capacity_wh=5040,
    )
    assert len(weighted) == 1
    assert weighted[0]["actual_soc"] == 84.0
    assert weighted[0]["error"] == 4.0


def test_prediction_accuracy_falls_back_to_main_when_no_pack_data(db):
    """Single-unit devices (HomePower 3000) and pre-pack-recording history
    have no battery_packs rows — the actual_soc should degenerate to
    main-only even when capacity hints are passed."""
    sn = "SN-NOPACKS"
    db.upsert_device(sn, "Tester", 19, "HomePower 3000")
    now = time.time()
    target = int((now - 3600) // 3600) * 3600
    db.record_forecast(sn, target - 3600, [{"ts": target, "predicted_soc": 70.0}])
    db.record(sn, target - 60, input_w=0, output_w=0, battery_pct=65, solar_w=0)
    db.record(sn, target + 60, input_w=0, output_w=0, battery_pct=65, solar_w=0)
    # No record_battery_packs call — single-unit device.

    out = db.prediction_accuracy(
        sn, main_capacity_wh=3024, pack_capacity_wh=3024,
    )
    assert len(out) == 1
    assert out[0]["actual_soc"] == 65.0  # main-only fallback
    assert out[0]["error"] == 5.0


def test_system_soc_at_capacity_weights_main_and_packs(db):
    """Standalone helper: capacity-weighted system SOC at a single ts."""
    sn = "SN-SOCAT"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    ts = int(time.time()) - 600
    db.record(sn, ts - 60, input_w=0, output_w=0, battery_pct=100, solar_w=0)
    db.record(sn, ts + 60, input_w=0, output_w=0, battery_pct=100, solar_w=0)
    db.record_battery_packs(sn, [
        {"deviceSn": "PACK-A", "deviceOrder": 0, "rb": 50},
    ], ts=ts)
    # main 100% (5040 Wh) + 1 pack 50% (5040 Wh) → 75%
    val = db.system_soc_at(sn, ts,
                           main_capacity_wh=5040, pack_capacity_wh=5040)
    assert val == 75.0
    # Without capacity hints, returns main-only.
    plain = db.system_soc_at(sn, ts)
    assert plain == 100.0


def test_prediction_accuracy_filters_by_made_at_cutoff(db):
    """`since_made_at_ts` excludes predictions older than the cutoff so
    the dashboard's headline summary can ignore stale rows produced by
    pre-fix forecaster code."""
    sn = "SN-CUTOFF"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    now = time.time()
    target = int((now - 3600) // 3600) * 3600  # 1h ago

    # Two forecasts for the same target: one OLD, one NEW.
    made_old = target - 5 * 3600   # made 5h before target
    made_new = target - 1 * 3600   # made 1h before target
    db.record_forecast(sn, made_old, [{"ts": target, "predicted_soc": 50.0}])
    db.record_forecast(sn, made_new, [{"ts": target, "predicted_soc": 70.0}])
    db.record(sn, target - 60, input_w=0, output_w=0,
              battery_pct=65, solar_w=0)
    db.record(sn, target + 60, input_w=0, output_w=0,
              battery_pct=65, solar_w=0)

    # Without cutoff: both rows show up.
    all_rows = db.prediction_accuracy(sn)
    assert len(all_rows) == 2

    # Cutoff between the two — only the newer row remains.
    cutoff = made_new - 60
    filtered = db.prediction_accuracy(sn, since_made_at_ts=cutoff)
    assert len(filtered) == 1
    assert filtered[0]["predicted_soc"] == 70.0


def test_bucket_accuracy_returns_signed_bias_alongside_mae():
    """`_bucket_accuracy` must return BOTH `mae` (unsigned) and `bias_pp`
    (signed mean of predicted - actual). Two predictions in the ≤6h
    bucket: one over by 4pp, one under by 2pp. MAE = 3.0, bias = +1.0.
    Bias surfaces systematic skew that MAE collapses to its absolute
    value and hides."""
    pytest.importorskip("Crypto", reason="server.py needs pycryptodome")
    from server import _bucket_accuracy
    samples = [
        # over-prediction: predicted 70, actual 66  → err=4, signed=+4
        {"lead_time_h": 2, "predicted_soc": 70.0, "actual_soc": 66.0,
         "error": 4.0},
        # under-prediction: predicted 50, actual 52 → err=2, signed=-2
        {"lead_time_h": 5, "predicted_soc": 50.0, "actual_soc": 52.0,
         "error": 2.0},
    ]
    out = _bucket_accuracy(samples)
    bucket = out["≤6h"]
    assert bucket["n"] == 2
    assert bucket["mae"] == 3.0
    assert bucket["bias_pp"] == 1.0  # (+4 + -2) / 2 = +1.0
