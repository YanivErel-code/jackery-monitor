"""Learn a sustained AC input rate from fresh, completed minute readings."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import median
from typing import TypedDict

DEFAULT_CHARGE_W = 1000
LOOKBACK_DAYS = 14
WINDOW_MINUTES = 6


class ChargeSample(TypedDict):
    ts: int
    input_w: float
    ac_input_w: float
    solar_w: float
    value_used: float
    path: str


class ChargeRate(TypedDict):
    value: float
    source: str
    n_samples: int
    updated_at: int
    last_sample_ts: int | None
    note: str
    samples: list[ChargeSample]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def learn_charge_rate(
    history: Sequence[Mapping[str, object]],
    *,
    now_ts: float,
    tz_offset_seconds: int | None = None,
) -> ChargeRate:
    """Prefer the newest stable six-minute window, never an isolated peak.

    Cloud-classified AC is accepted when solar is near zero. Unclassified
    input additionally requires known local night hours. Missing provenance,
    stale receipts, tapering SOC and incomplete minutes cannot train the rate.
    """
    result: ChargeRate = {
        "value": DEFAULT_CHARGE_W,
        "source": "default",
        "n_samples": 0,
        "updated_at": int(now_ts),
        "last_sample_ts": None,
        "note": "Using 1,000 W until six consecutive fresh charging minutes are available",
        "samples": [],
    }
    completed_until = int(now_ts // 60) * 60
    candidates: dict[int, ChargeSample] = {}
    for row in history:
        timestamp = _number(row.get("ts"))
        received_min = _number(row.get("soc_source_ts_min"))
        received_max = _number(row.get("soc_source_ts_max"))
        known = _number(row.get("soc_source_known_buckets"))
        soc = _number(row.get("battery_pct"))
        power = _number(row.get("input_w"))
        ac_power = _number(row.get("ac_input_w"))
        solar = _number(row.get("solar_w"))
        if (
            timestamp is None
            or received_min is None
            or received_max is None
            or known is None
            or known < 1
            or soc is None
            or not 0 <= soc < 90
            or power is None
            or ac_power is None
            or solar is None
            or not 0 <= power <= 5000
            or not 0 <= ac_power <= 5000
            or not 0 <= solar <= 50
            or row.get("measurement_suspect")
        ):
            continue
        if (
            timestamp % 60
            or timestamp < now_ts - LOOKBACK_DAYS * 86400
            or timestamp + 60 > completed_until
            or not timestamp - 120 <= received_min <= received_max < timestamp + 60
        ):
            continue
        if ac_power >= 100:
            value, path = ac_power, "ac_input_w"
        elif power >= 100 and tz_offset_seconds is not None:
            local_hour = ((timestamp + tz_offset_seconds) // 3600) % 24
            if 6 <= local_hour < 21:
                continue
            value, path = power, "input_w_night"
        else:
            continue
        candidates[int(timestamp)] = {
            "ts": int(timestamp),
            "input_w": power,
            "ac_input_w": ac_power,
            "solar_w": solar,
            "value_used": value,
            "path": path,
        }
    ordered = sorted(candidates.values(), key=lambda sample: sample["ts"])
    for end in range(len(ordered), WINDOW_MINUTES - 1, -1):
        window = ordered[end - WINDOW_MINUTES : end]
        if window[-1]["ts"] - window[0]["ts"] != (WINDOW_MINUTES - 1) * 60:
            continue
        center = median(sample["value_used"] for sample in window)
        stable = [
            sample for sample in window if abs(sample["value_used"] - center) <= center * 0.15
        ]
        if len(stable) < WINDOW_MINUTES - 1:
            continue
        result["value"] = float(round(median(sample["value_used"] for sample in stable)))
        result["source"] = "fit"
        result["n_samples"] = len(stable)
        result["last_sample_ts"] = stable[-1]["ts"]
        result["note"] = (
            "Median of the latest stable six-minute AC charging window; updates automatically"
        )
        result["samples"] = stable
        break
    return result
