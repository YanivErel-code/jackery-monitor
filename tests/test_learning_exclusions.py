"""Unreconciled energy must not train SOC fits or erase the evidence."""
import pytest

import forecaster
from energy_db import EnergyDB


def test_exclusion_preserves_history_and_blocks_adjacent_soc_fits(tmp_path):
    db = EnergyDB(str(tmp_path / "energy.db"))
    with db._conn() as c:
        for sn in ("MAIN", "OTHER"):
            c.executemany(
                "INSERT INTO samples(device_sn,bucket,input_wh,output_wh,last_battery_pct) "
                "VALUES(?,?,?,?,?)",
                [(sn, h * 3600, 1000, 0, 30 + h * 3) for h in range(5)],
            )
    before = db.history("MAIN", hours=10, bucket_s=3600, until_ts=18000)
    assert forecaster.fit_charge_efficiency(before, 30000, min_windows=1) == (0.9, 4)
    db.exclude_learning_interval("MAIN", 3600, 7200, reason="unreconciled AC energy")
    # Exclusions are durable, half-open, and scoped to one device.
    db = EnergyDB(db.path)
    rows = db.history("MAIN", hours=10, bucket_s=3600, until_ts=18000)
    assert [r["ts"] for r in rows if r.get("soc_fit_excluded")] == [3600]
    assert [{k: v for k, v in r.items() if k != "soc_fit_excluded"} for r in rows] == before
    assert not any(r.get("soc_fit_excluded") for r in db.history(
        "OTHER", hours=10, bucket_s=3600, until_ts=18000))
    assert forecaster.fit_charge_efficiency(rows, 30000, min_windows=1) == (0.9, 2)
    assert forecaster._row_soc(rows[1]) is None
    db.restore_learning_interval("MAIN", 3600)
    assert db.history("MAIN", hours=10, bucket_s=3600, until_ts=18000) == before


def test_exclusions_cover_partial_buckets_and_restore_independently(tmp_path):
    db = EnergyDB(str(tmp_path / "energy.db"))
    with db._conn() as c:
        c.executemany(
            "INSERT INTO samples(device_sn,bucket,last_battery_pct) VALUES(?,?,?)",
            [("MAIN", ts, 40) for ts in (3540, 3600, 3660, 3720)],
        )
    db.exclude_learning_interval("MAIN", 3601, 3661, reason="first")
    db.exclude_learning_interval("MAIN", 3601, 3661, reason="first")
    db.exclude_learning_interval("MAIN", 3660, 3720, reason="second")
    db.restore_learning_interval("MAIN", 3600)
    rows = db.history("MAIN", hours=2, bucket_s=60, until_ts=4000)
    assert [r["ts"] for r in rows if r.get("soc_fit_excluded")] == [3660]
    with pytest.raises(ValueError, match="unknown"):
        db.restore_learning_interval("MAIN", 0)


def test_multi_pack_clean_runs_cannot_cross_an_excluded_interior_row():
    rows = [dict(ts=h * 3600, system_soc=95 - h, output_wh=200) for h in range(8)]
    rows[3]["soc_fit_excluded"] = True
    runs = forecaster._clean_discharge_runs(rows, 30000, require_system_soc=True)
    assert [duration for _, _, duration in runs] == [2.0, 3.0]


def test_exclusion_invalidates_empirical_calibration_without_removing_soc(tmp_path):
    from tests.test_forecast_calibration import BASE, telemetry_fixture

    db = EnergyDB(str(tmp_path / "energy.db"))
    samples, packs = telemetry_fixture()
    with db._conn() as c:
        c.executemany(
            "INSERT INTO samples(device_sn,bucket,output_wh,solar_wh,ac_input_wh,"
            "last_battery_pct,last_soc_source_ts,last_soc_source) VALUES(?,?,?,?,?,?,?,?)",
            [("MAIN", *row) for row in samples],
        )
        c.executemany(
            "INSERT INTO battery_packs(parent_sn,ts,pack_sn,soc_pct,source_ts,source,error_code) "
            "VALUES(?,?,?,?,?,?,?)", [("MAIN", *row) for row in packs],
        )
    kwargs = dict(hours=24, bucket_s=3600, main_capacity_wh=5040,
                  pack_capacity_wh=5040, include_calibration=True, until_ts=BASE + 7200)
    before = next(r for r in db.history("MAIN", **kwargs) if r["ts"] == BASE)
    assert before["calibration"]["quality"] == "ok"
    db.exclude_learning_interval("MAIN", BASE + 60, BASE + 120, reason="audit")
    after = next(r for r in db.history("MAIN", **kwargs) if r["ts"] == BASE)
    assert after["calibration"]["quality"] == "learning_excluded"
    assert after["system_soc"] == before["system_soc"]
    assert after["output_wh"] == before["output_wh"]


@pytest.mark.parametrize("start,end", [(True, 100), (0, float("inf")), (100, 100)])
def test_exclusion_rejects_invalid_intervals(tmp_path, start, end):
    db = EnergyDB(str(tmp_path / "energy.db"))
    with pytest.raises(ValueError):
        db.exclude_learning_interval("MAIN", start, end, reason="audit")
