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
