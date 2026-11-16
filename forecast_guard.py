"""Short-lived planning evidence; never rewrites SOC or trains physical losses."""

from __future__ import annotations

import math
from typing import Any, TypedDict, TypeGuard


class PowerSample(TypedDict):
    bucket: int
    output_wh: float
    solar_wh: float
    ac_input_wh: float
    last_source_ts: float | None
    sample_count: int


class BatteryInputs(TypedDict, total=False):
    captured_at: float
    capacity_wh: int
    system_soc_pct: float
    main_soc_source_ts: float
    main_soc_source: str
    pack_source_ts: float
    pack_source: str
    pack_sns: list[str]
    pack_soc_pct: list[float]


class PlanningEvidence(TypedDict):
    power: list[PowerSample]
    previous_inputs: BatteryInputs | None


class LoadNowcast(TypedDict):
    source: str
    observed_output_w: float
    planning_output_w: float
    uncertainty_pct: float
    evidence_start_ts: int
    evidence_end_ts: int
    until_ts: int
    appliance_active: bool


class EnergyReconciliation(TypedDict, total=False):
    status: str
    reason: str
    start_ts: float
    end_ts: float
    observed_soc_drop_wh: float
    metered_net_wh: float
    modeled_drop_wh: float
    unexplained_wh: float
    tolerance_wh: float
    low_soc_learning_excluded: bool


def finite(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_power(row: PowerSample, now: float) -> bool:
    stamp, receipt = row.get("bucket"), row.get("last_source_ts")
    return (
        finite(stamp)
        and stamp % 60 == 0
        and stamp + 60 <= now
        and finite(receipt)
        and stamp - 60 <= receipt <= min(stamp + 65, now)
        and finite(row.get("sample_count"))
        and row["sample_count"] >= 5
        and all(
            finite(value) and 0 <= value <= 20000 / 60
            for value in (row.get("output_wh"), row.get("solar_wh"), row.get("ac_input_wh"))
        )
    )


def fit_load_nowcast(
    samples: list[PowerSample],
    *,
    now_ts: float,
    appliance_active: bool = False,
    hold_until_ts: float | None = None,
) -> LoadNowcast | None:
    """Use complete recent minutes, not an instantaneous heater peak.

    The larger five/fifteen-minute mean retains cycling demand. A declared
    planning allowance is 20% during an appliance hold, 10% otherwise. The
    observation only raises the existing load and expires after 15 minutes,
    or the provisional appliance release (at most two hours, then reobserve).
    """
    end = int(now_ts // 60 * 60)
    if len({row.get("bucket") for row in samples}) != len(samples):
        return None
    rows = {row["bucket"]: row for row in samples if _valid_power(row, now_ts)}
    if any(stamp not in rows for stamp in range(end - 300, end, 60)):
        return None
    lengths = [5]
    if all(stamp in rows for stamp in range(end - 900, end, 60)):
        lengths.append(15)
    observed = max(
        sum(rows[stamp]["output_wh"] for stamp in range(end - minutes * 60, end, 60)) * 60 / minutes
        for minutes in lengths
    )
    uncertainty = 0.2 if appliance_active else 0.1
    until = now_ts + (1800 if appliance_active else 900)
    if appliance_active and finite(hold_until_ts) and hold_until_ts > now_ts:
        until = min(hold_until_ts, now_ts + 7200)
    return {
        "source": "recent_metered_load",
        "observed_output_w": round(observed, 1),
        "planning_output_w": round(observed * (1 + uncertainty), 1),
        "uncertainty_pct": uncertainty * 100,
        "evidence_start_ts": end - max(lengths) * 60,
        "evidence_end_ts": end,
        "until_ts": math.ceil(until),
        "appliance_active": appliance_active,
    }


def reconcile_energy(
    previous: BatteryInputs | None,
    current: BatteryInputs,
    samples: list[PowerSample],
    *,
    baseline_w: float,
    overhead_pct: float,
) -> EnergyReconciliation:
    """Bound a discharge residual using unchanged, fresh SOC sources and packs.

    This is an unresolved reconciliation warning, not a claim that SOC or the
    power meter is correct. AC intervals are ineligible because the meter may
    exclude bypass load. Quantization allows one whole system SOC point plus
    one boundary minute of power; no residual is fitted as a physical loss.
    """
    unknown: EnergyReconciliation = {"status": "unavailable", "reason": "awaiting_matched_inputs"}
    if not previous:
        return unknown
    start, end, capacity = (
        previous.get("captured_at"),
        current.get("captured_at"),
        current.get("capacity_wh"),
    )
    if not finite(start) or not finite(end) or not finite(capacity):
        return unknown
    if (
        not finite(baseline_w)
        or not finite(overhead_pct)
        or baseline_w < 0
        or not 0 <= overhead_pct <= 1
    ):
        return unknown
    if not 900 <= end - start <= 1800 or capacity <= 0 or previous.get("capacity_wh") != capacity:
        return unknown
    for snapshot in (previous, current):
        soc, stamp = snapshot.get("system_soc_pct"), snapshot["captured_at"]
        if not finite(soc) or not 0 <= soc <= 100:
            return unknown
        for key in ("main_soc_source_ts", "pack_source_ts"):
            if not finite(snapshot.get(key)) or not -5 <= stamp - snapshot[key] <= 120:
                return {**unknown, "reason": "stale_soc"}
    if any(
        previous.get(key) != current.get(key) or current.get(key) not in ("http", "mqtt", "ble")
        for key in ("main_soc_source", "pack_source")
    ):
        return {**unknown, "reason": "source_changed"}
    packs = current.get("pack_sns")
    if (
        not isinstance(packs, list)
        or not packs
        or any(not isinstance(sn, str) or not sn for sn in packs)
        or len(set(packs)) != len(packs)
        or set(packs) != set(previous.get("pack_sns") or [])
        or len(previous.get("pack_soc_pct") or []) != len(packs)
        or len(current.get("pack_soc_pct") or []) != len(packs)
    ):
        return {**unknown, "reason": "pack_identity_unconfirmed"}
    first, last = int(start // 60 * 60), int(end // 60 * 60)
    # Only complete buckets are used; the final partial minute is explicitly
    # included in the uncertainty bound instead of inventing measured energy.
    by_stamp = {row["bucket"]: row for row in samples if _valid_power(row, end)}
    if any(stamp not in by_stamp for stamp in range(first, last, 60)):
        return {**unknown, "reason": "incomplete_power"}
    rows = [by_stamp[stamp] for stamp in range(first, last, 60)]
    if sum(row["ac_input_wh"] for row in rows) > 1:
        return {**unknown, "reason": "ac_bypass_interval"}
    output = sum(
        row["output_wh"] * max(0, min(end, row["bucket"] + 60) - max(start, row["bucket"])) / 60
        for row in rows
    )
    solar = sum(
        row["solar_wh"] * max(0, min(end, row["bucket"] + 60) - max(start, row["bucket"])) / 60
        for row in rows
    )
    modeled = output * (1 + overhead_pct) + baseline_w * (end - start) / 3600 - solar
    if modeled < 0:
        return {**unknown, "reason": "solar_surplus"}
    observed = (previous["system_soc_pct"] - current["system_soc_pct"]) * capacity / 100
    boundary = max(
        (row["output_wh"] * (1 + overhead_pct) + row["solar_wh"] for row in rows), default=0
    )
    tolerance = capacity * 0.01 + boundary + baseline_w / 60
    residual = observed - modeled
    return {
        "status": "discrepancy" if abs(residual) > tolerance else "consistent",
        "reason": "unresolved_soc_power_reconciliation"
        if abs(residual) > tolerance
        else "within_measurement_allowance",
        "start_ts": start,
        "end_ts": end,
        "observed_soc_drop_wh": round(observed, 1),
        "metered_net_wh": round(output - solar, 1),
        "modeled_drop_wh": round(modeled, 1),
        "unexplained_wh": round(residual, 1),
        "tolerance_wh": round(tolerance, 1),
        "low_soc_learning_excluded": min(previous["system_soc_pct"], current["system_soc_pct"])
        < 20,
    }
