"""Empirical calibration cannot learn from charging, gaps, stale data, or SOC jumps."""

from copy import deepcopy

import pytest

from forecast_calibration import build_discharge_intervals, fit_system_residual

BASE = 1_790_000_000 // 86400 * 86400
CAPACITY = 30240
SIGNATURE = "fixture-five-packs"


def clean_history(days=5, baseline=140.0, load=500.0):
    rows = []
    for day in range(days):
        start = BASE + day * 86400
        for hour in range(8):
            timestamp = start + hour * 3600
            rows.append(
                {
                    "ts": timestamp,
                    "calibration": {
                        "quality": "ok",
                        "start_ts": timestamp,
                        "end_ts": timestamp + 3600,
                        "start_soc": 65 - hour * (baseline + load * 1.1) / CAPACITY * 100,
                        "end_soc": 65 - (hour + 1) * (baseline + load * 1.1) / CAPACITY * 100,
                        "output_wh": load,
                        "capacity_wh": CAPACITY,
                        "pack_count": 5,
                        "pack_signature": SIGNATURE,
                        "current_pack_signature": SIGNATURE,
                    },
                }
            )
    return rows


def fitted(rows, now=None, pack_count=5):
    return fit_system_residual(
        rows,
        CAPACITY,
        pack_count=pack_count,
        overhead_pct=0.1,
        now_ts=now or BASE + 5 * 86400,
        utc_offset_seconds=0,
    )


def test_low_soc_calibration_learns_system_baseline_without_fixed_pack_floor():
    """Reserve half a SOC point of four-hour endpoint uncertainty."""
    result = fitted(clean_history())
    assert result is not None
    assert 140 <= result["baseline_w"] <= 185
    assert result["baseline_w"] < 300
    assert result["n_days"] == 5
    assert result["source"] == "validated_system_residual"


@pytest.mark.parametrize("reason", ["charging", "stale", "coverage", "soc_jump", "pack_error"])
def test_bad_quality_intervals_cannot_calibrate(reason):
    rows = clean_history()
    for row in rows:
        row["calibration"]["quality"] = reason
    assert fitted(rows) is None


def test_calibration_requires_three_independent_days_not_overlapping_windows():
    rows = clean_history(days=2)
    assert fitted(rows, now=BASE + 3 * 86400) is None
    assert fitted(rows + deepcopy(rows), now=BASE + 3 * 86400) is None


def test_one_night_crossing_midnight_cannot_count_as_two_independent_days():
    rows = clean_history(days=2)
    for row in rows:
        row["ts"] += 19 * 3600
        row["calibration"]["start_ts"] += 19 * 3600
        row["calibration"]["end_ts"] += 19 * 3600
        row["calibration"]["start_soc"] -= 10
        row["calibration"]["end_soc"] -= 10
    assert fitted(rows, now=BASE + 3 * 86400) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_soc", float("nan")),
        ("end_soc", "invalid"),
        ("output_wh", -10),
        ("start_ts", None),
        ("end_ts", BASE),
    ],
)
def test_malformed_calibration_annotations_fail_closed(field, value):
    rows = clean_history()
    for row in rows:
        row["calibration"][field] = value
    assert fitted(rows) is None


def test_calibration_rejects_future_incomplete_and_stale_evidence():
    rows = clean_history()
    assert fitted(rows, now=BASE + 86400) is None
    assert fitted(rows, now=BASE + 12 * 86400) is None


def test_pack_configuration_and_capacity_changes_invalidate_old_calibration():
    rows = clean_history()
    rows[-1]["calibration"]["current_pack_signature"] = "replacement-packs"
    assert fitted(rows) is None
    assert fitted(clean_history(), pack_count=4) is None
    for row in rows:
        row["calibration"]["capacity_wh"] = 10080
    assert fitted(rows) is None


def test_inconsistent_night_residuals_do_not_relax_fallback():
    """Coherent bimodal nights must fail, not merely broken adjacent endpoints."""
    rows = clean_history()
    for index, row in enumerate(rows):
        if index // 8 % 2:
            hour = index % 8
            row["calibration"]["start_soc"] = 65 - hour * 1250 / CAPACITY * 100
            row["calibration"]["end_soc"] = 65 - (hour + 1) * 1250 / CAPACITY * 100
    assert fitted(rows) is None


def crossing_history(days=5):
    rows = clean_history(days=days)
    for index, row in enumerate(rows):
        hour = index % 8
        row["calibration"]["start_soc"] += 20
        row["calibration"]["end_soc"] += 20
        if hour >= 4:
            row["calibration"]["quality"] = "charging"
    return rows


def test_pooled_discharge_does_not_lose_evidence_at_arbitrary_soc_bin_edges():
    result = fitted(crossing_history())
    assert result is not None
    pooled = next(band for band in result["soc_bands"] if band.get("source") == "pooled_discharge")
    assert pooled["n_days"] == 5
    assert pooled["baseline_w"] == pytest.approx(177.8)
    assert pooled["min_soc"] > 70
    assert pooled["max_soc"] == 85


def test_pooled_discharge_requires_five_independent_nights():
    assert fitted(crossing_history(days=4)) is None


def test_pooled_discharge_cannot_hide_a_recent_higher_drain_regime():
    rows = crossing_history(days=10)
    for hour, row in enumerate(rows[-8:]):
        row["calibration"]["start_soc"] = 85 - hour * 1150 / CAPACITY * 100
        row["calibration"]["end_soc"] = 85 - (hour + 1) * 1150 / CAPACITY * 100
    assert fitted(rows, now=BASE + 10 * 86400) is None


def test_pooled_discharge_keeps_low_soc_and_bad_intervals_out():
    rows = crossing_history()
    for row in rows:
        row["calibration"]["start_soc"] -= 50
        row["calibration"]["end_soc"] -= 50
    result = fitted(rows)
    assert result is None or all(
        band.get("source") != "pooled_discharge" for band in result["soc_bands"]
    )
    rows = crossing_history()
    for row in rows:
        if row["calibration"]["start_ts"] % 86400 == 2 * 3600:
            row["calibration"]["quality"] = "stale"
    assert fitted(rows) is None


def test_pooled_discharge_cannot_fill_unobserved_gaps_between_soc_ranges():
    rows = crossing_history(days=10)
    for row in rows[5 * 8 :]:
        row["calibration"]["start_soc"] -= 30
        row["calibration"]["end_soc"] -= 30
    result = fitted(rows, now=BASE + 10 * 86400)
    assert result is not None
    assert not any(
        band["min_soc"] <= 65 <= band["max_soc"]
        for band in result["soc_bands"]
        if band.get("source") == "pooled_discharge"
    )


def telemetry_fixture():
    samples = []
    packs = []
    for minute in range(-5, 66):
        timestamp = BASE + minute * 60
        soc = 65 - minute / 60 * 700 / CAPACITY * 100
        samples.append((timestamp, 500 / 60, 0.0, 0.0, soc, timestamp + 30, "http"))
        if minute % 5 == 0:
            for number in range(5):
                packs.append(
                    (timestamp + 30, f"fixture-pack-{number}", soc, timestamp + 30, "mqtt", 0)
                )
    return samples, packs


def intervals(samples, packs):
    return build_discharge_intervals(
        samples,
        packs,
        main_capacity_wh=5040,
        pack_capacity_wh=5040,
        since_ts=BASE,
        until_ts=BASE + 2 * 3600,
    )


def test_discharge_interval_requires_complete_fresh_synchronized_pack_readings():
    samples, packs = telemetry_fixture()
    result = intervals(samples, packs)
    assert result[BASE]["quality"] == "ok"
    assert result[BASE]["output_wh"] == pytest.approx(500)
    assert result[BASE]["capacity_wh"] == CAPACITY
    assert result[BASE]["pack_count"] == 5
    stale = [
        (timestamp, serial, soc, source - 3600, kind, error)
        for timestamp, serial, soc, source, kind, error in packs
    ]
    assert intervals(samples, stale)[BASE]["quality"] != "ok"


def test_interval_rejects_missing_minutes_charging_and_pack_errors():
    samples, packs = telemetry_fixture()
    assert (
        intervals([row for row in samples if row[0] != BASE + 1800], packs)[BASE]["quality"] != "ok"
    )
    charged = [(*row[:3], 8.0 if row[0] == BASE + 1800 else row[3], *row[4:]) for row in samples]
    assert intervals(charged, packs)[BASE]["quality"] != "ok"
    broken = [(*row[:5], 1 if row[0] == BASE + 1800 + 30 else row[5]) for row in packs]
    assert intervals(samples, broken)[BASE]["quality"] != "ok"


def test_pack_error_between_soc_checkpoints_still_rejects_interval():
    samples, packs = telemetry_fixture()
    broken = [(*row[:5], 1 if row[0] == BASE + 2100 + 30 else row[5]) for row in packs]
    assert intervals(samples, broken)[BASE]["quality"] != "ok"


def test_interval_rejects_soc_recalibration_even_with_recent_receipts():
    samples, packs = telemetry_fixture()
    samples = [
        (*row[:4], row[4] - (9 if row[0] >= BASE + 1800 else 0), *row[5:]) for row in samples
    ]
    packs = [(*row[:2], row[2] - (9 if row[0] >= BASE + 1830 else 0), *row[3:]) for row in packs]
    assert intervals(samples, packs)[BASE]["quality"] == "soc_jump"
