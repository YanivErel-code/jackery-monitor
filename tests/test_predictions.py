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
