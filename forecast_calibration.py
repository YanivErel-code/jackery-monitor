"""Quality-gated empirical SOC drain calibration, separate from physical fits."""

from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from collections import defaultdict
from typing import Any, NotRequired, TypedDict, TypeGuard

Sample = tuple[int, float, float, float, float | None, float | None, str | None]
Pack = tuple[int, str, float | None, float | None, str | None, int | None]


class SocDrainBand(TypedDict):
    min_soc: float
    max_soc: float
    baseline_w: float
    n_days: int
    newest_run_ts: int
    source: NotRequired[str]


class DischargeInterval(TypedDict, total=False):
    quality: str
    start_ts: int
    end_ts: int
    capacity_wh: int
    pack_count: int
    pack_signature: str
    current_pack_signature: str | None
    start_soc: float
    end_soc: float
    output_wh: float


class CalibrationHistoryRow(TypedDict, total=False):
    ts: float
    calibration: DischargeInterval | None


class SystemDrainFit(TypedDict):
    baseline_w: float
    n_runs: int
    n_days: int
    newest_run_ts: int
    source: str
    soc_bands: list[SocDrainBand]


def build_discharge_intervals(
    samples: list[Sample],
    packs: list[Pack],
    *,
    main_capacity_wh: int,
    pack_capacity_wh: int,
    since_ts: int,
    until_ts: int,
) -> dict[int, DischargeInterval]:
    """Annotate closed hours using minute energy and synchronized SOC receipts."""
    if main_capacity_wh <= 0 or pack_capacity_wh < 0 or until_ts <= since_ts:
        return {}
    sample_by_bucket = {row[0]: row for row in samples if row[0] < until_ts}
    sample_times = sorted(sample_by_bucket)
    pack_frames: dict[int, list[Pack]] = defaultdict(list)
    for row in packs:
        if row[0] <= until_ts:
            pack_frames[row[0]].append(row)
    frame_times = sorted(pack_frames)

    def nearest(times: list[int], timestamp: int) -> int | None:
        position = bisect.bisect_left(times, timestamp)
        choices = times[max(0, position - 1) : position + 1]
        return min(choices, key=lambda value: abs(value - timestamp)) if choices else None

    def frame_signature(frame: list[Pack]) -> str:
        return hashlib.sha256("\n".join(sorted(row[1] for row in frame)).encode()).hexdigest()

    latest_frame = pack_frames[frame_times[-1]] if frame_times else []
    signature = frame_signature(latest_frame)
    pack_count = len(latest_frame)
    current_signature: str | None = signature
    if latest_frame and any(
        not _finite(row[3]) or not 0 <= until_ts - row[3] <= 600 for row in latest_frame
    ):
        current_signature = None

    capacity = main_capacity_wh + pack_capacity_wh * pack_count

    def point(timestamp: int) -> tuple[float, float] | None:
        sample_time = nearest(sample_times, timestamp)
        if sample_time is None or abs(sample_time - timestamp) > 60:
            return None
        sample = sample_by_bucket[sample_time]
        if not _valid_receipt(sample[4], sample[5], sample[6], timestamp, 120, until_ts):
            return None
        frame_time = nearest(frame_times, timestamp)
        frame = pack_frames[frame_time] if frame_time is not None else []
        if pack_count:
            if frame_signature(frame) != signature or len(frame) != pack_count:
                return None
            if any(
                row[5] != 0 or not _valid_receipt(row[2], row[3], row[4], timestamp, 180, until_ts)
                for row in frame
            ):
                return None
        pack_socs = [float(row[2] or 0) for row in frame]
        system_soc = (
            float(sample[4] or 0) * main_capacity_wh + sum(pack_socs) * pack_capacity_wh
        ) / capacity
        return system_soc, min([float(sample[4] or 0), *pack_socs])

    intervals: dict[int, DischargeInterval] = {}
    for start in range(since_ts // 3600 * 3600, until_ts, 3600):
        end = start + 3600
        interval: DischargeInterval = {
            "quality": "incomplete",
            "start_ts": start,
            "end_ts": end,
            "capacity_wh": capacity,
            "pack_count": pack_count,
            "pack_signature": signature,
            "current_pack_signature": current_signature,
        }
        intervals[start] = interval
        if end + 180 > until_ts:
            continue
        buckets = [sample_by_bucket.get(timestamp) for timestamp in range(start, end, 60)]
        if any(row is None for row in buckets):
            interval["quality"] = "coverage"
            continue
        complete_buckets = [row for row in buckets if row is not None]
        if any(
            not _valid_receipt(row[4], row[5], row[6], row[0] + 30, 120, until_ts)
            or any(not _finite(value) or value < 0 for value in row[1:4])
            for row in complete_buckets
        ):
            interval["quality"] = "stale"
            continue
        if (
            sum(row[2] for row in complete_buckets) > 5
            or sum(row[3] for row in complete_buckets) > 1
        ):
            interval["quality"] = "charging"
            continue
        start_index = bisect.bisect_left(frame_times, start)
        end_index = bisect.bisect_right(frame_times, end)
        interval_frames = [
            pack_frames[timestamp] for timestamp in frame_times[start_index:end_index]
        ]
        if any(
            frame_signature(frame) != signature
            or any(
                row[5] != 0 or not _valid_receipt(row[2], row[3], row[4], row[0], 600, until_ts)
                for row in frame
            )
            for frame in interval_frames
        ):
            interval["quality"] = "pack_error_or_stale"
            continue
        points = [point(timestamp) for timestamp in range(start, end + 1, 900)]
        if any(value is None for value in points):
            interval["quality"] = "pack_error_or_stale"
            continue
        valid_points = [value for value in points if value is not None]
        interval["start_soc"] = valid_points[0][0]
        interval["end_soc"] = valid_points[-1][0]
        interval["output_wh"] = sum(row[1] for row in complete_buckets)
        discontinuity = False
        for position in range(4):
            dropped_wh = (
                (valid_points[position][0] - valid_points[position + 1][0]) * capacity / 100
            )
            output_wh = sum(row[1] for row in complete_buckets[position * 15 : (position + 1) * 15])
            noise_wh = capacity * 0.01
            if dropped_wh < -noise_wh or dropped_wh > output_wh + 250 + noise_wh:
                discontinuity = True
        if discontinuity:
            interval["quality"] = "soc_jump"
        elif any(value[0] < 20 or value[0] > 95 or value[1] < 10 for value in valid_points):
            interval["quality"] = "soc_range"
        elif interval["output_wh"] < 50:
            interval["quality"] = "low_load"
        else:
            interval["quality"] = "ok"
    return intervals


def _finite(value: Any) -> TypeGuard[int | float]:
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)


def _valid_receipt(
    soc: Any, source_ts: Any, source: str | None, timestamp: int, tolerance: int, until_ts: int
) -> bool:
    return (
        _finite(soc)
        and 0 <= soc <= 100
        and _finite(source_ts)
        and source in ("http", "mqtt", "ble")
        and source_ts <= until_ts
        and abs(source_ts - timestamp) <= tolerance
    )


def fit_system_residual(
    history: list[CalibrationHistoryRow],
    capacity_wh: int,
    *,
    pack_count: int,
    overhead_pct: float,
    now_ts: float,
    utc_offset_seconds: int = 0,
) -> SystemDrainFit | None:
    """Learn the combined baseline, not separate main/pack physical losses."""
    timed_rows = [row for row in history if _finite(row.get("ts")) and row["ts"] <= now_ts]
    annotations: list[DischargeInterval] = []
    for row in sorted(timed_rows, key=lambda row: row["ts"]):
        annotation = row.get("calibration")
        if isinstance(annotation, dict):
            annotations.append(annotation)
    if not annotations or capacity_wh <= 0 or not 0 <= overhead_pct <= 1:
        return None
    signature = annotations[-1].get("current_pack_signature")
    if signature is None:
        return None
    runs: list[list[DischargeInterval]] = []
    current: list[DischargeInterval] = []
    for interval in annotations:
        valid = (
            interval.get("quality") == "ok"
            and all(
                _finite(interval.get(key))
                for key in ("start_ts", "end_ts", "start_soc", "end_soc", "output_wh")
            )
            and interval["end_ts"] - interval["start_ts"] == 3600
            and 0 <= interval["start_soc"] <= 100
            and 0 <= interval["end_soc"] <= 100
            and interval["output_wh"] >= 0
            and interval.get("capacity_wh") == capacity_wh
            and interval.get("pack_count") == pack_count
            and interval.get("pack_signature") == signature
            and now_ts - 14 * 86400 <= interval["start_ts"]
            and interval["end_ts"] <= now_ts
        )
        continuous = not current or (
            valid
            and interval["start_ts"] == current[-1]["end_ts"]
            and abs(interval.get("start_soc", 0) - current[-1]["end_soc"]) <= 0.5
        )
        if not valid or not continuous or len(current) >= 12:
            if current:
                runs.append(current)
            current = []
        if valid:
            current.append(interval)
    if current:
        runs.append(current)
    bands: list[SocDrainBand] = []
    for minimum, maximum, required_days in (
        (20, 40, 3),
        (40, 60, 3),
        (60, 80, 3),
        (80, 95, 3),
        (40, 95, 5),
    ):
        by_day: dict[int, list[tuple[float, int, float, float]]] = defaultdict(list)
        for run in runs:
            for position in range(len(run) - 3):
                window = run[position : position + 4]
                if any(
                    not minimum <= interval["start_soc"] <= maximum
                    or not minimum <= interval["end_soc"] <= maximum
                    for interval in window
                ):
                    continue
                dropped_wh = (window[0]["start_soc"] - window[-1]["end_soc"]) * capacity_wh / 100
                output_wh = sum(interval["output_wh"] for interval in window)
                baseline = (dropped_wh - output_wh * (1 + overhead_pct)) / 4
                if dropped_wh < output_wh - capacity_wh * 0.01 or not -75 <= baseline <= 1000:
                    continue
                day = (window[-1]["end_ts"] + utc_offset_seconds - 12 * 3600) // 86400
                by_day[day].append(
                    (
                        baseline,
                        window[-1]["end_ts"],
                        min(min(interval["start_soc"], interval["end_soc"]) for interval in window),
                        max(max(interval["start_soc"], interval["end_soc"]) for interval in window),
                    )
                )
        nights = sorted(by_day.items())[-10:]
        evidence = [
            (
                statistics.median(value[0] for value in observations),
                max(value[1] for value in observations),
                min(value[2] for value in observations),
                max(value[3] for value in observations),
            )
            for _, observations in nights
        ]
        if (
            len(evidence) < required_days
            or now_ts - max(value[1] for value in evidence) > 2 * 86400
        ):
            continue
        baselines = sorted(value[0] for value in evidence)
        median = statistics.median(baselines)
        lower = baselines[int((len(baselines) - 1) * 0.25)]
        upper = baselines[math.ceil((len(baselines) - 1) * 0.75)]
        consistency_limit = max(100, abs(median) * 0.5)
        uncertainty_w = capacity_wh * 0.005 / 4
        if upper - lower > consistency_limit:
            continue
        if required_days == 5 and max(baselines) - upper > consistency_limit + uncertainty_w:
            continue
        band: SocDrainBand = {
            "min_soc": min(value[2] for value in evidence) if required_days == 5 else minimum,
            "max_soc": max(value[3] for value in evidence) if required_days == 5 else maximum,
            "baseline_w": max(0.0, upper) + uncertainty_w,
            "n_days": len(evidence),
            "newest_run_ts": max(value[1] for value in evidence),
        }
        if required_days == 5:
            band["source"] = "pooled_discharge"
            coverage: list[tuple[float, float, int]] = []
            for start_soc, end_soc, newest in sorted(
                (value[2], value[3], value[1])
                for _, observations in nights
                for value in observations
            ):
                if coverage and start_soc <= coverage[-1][1]:
                    previous = coverage[-1]
                    coverage[-1] = (
                        previous[0],
                        max(previous[1], end_soc),
                        max(previous[2], newest),
                    )
                else:
                    coverage.append((start_soc, end_soc, newest))
            for start_soc, end_soc, newest in coverage:
                if now_ts - newest <= 2 * 86400:
                    bands.append(
                        {**band, "min_soc": start_soc, "max_soc": end_soc, "newest_run_ts": newest}
                    )
        else:
            bands.append(band)
    if not bands:
        return None
    selected = max(bands, key=lambda band: band["n_days"])
    return {
        "baseline_w": selected["baseline_w"],
        "n_runs": selected["n_days"],
        "n_days": selected["n_days"],
        "newest_run_ts": selected["newest_run_ts"],
        "source": "validated_system_residual",
        "soc_bands": bands,
    }
