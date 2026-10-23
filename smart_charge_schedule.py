"""Pure, bounded charge-schedule construction using the existing SOC simulator."""

from __future__ import annotations

import math
from itertools import pairwise

import forecaster
from cost import rate_at

SOC_EPSILON = 1e-7


def _finite(value, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Forecast contains an invalid numeric input")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError("Forecast contains an out-of-range input")
    return float(value)


def _hours(forecast, now, sunrise):
    if not now < sunrise <= now + 36 * 3600:
        raise ValueError("Forecast horizon is unavailable or exceeds 36 hours")
    result = []
    covered = float(now)
    for hour in forecast.get("forecast") or []:
        end = _finite(hour.get("ts"))
        duration = _finite(hour.get("duration_h"), minimum=0, maximum=1) * 3600
        start = end - duration
        if end <= now or start >= sunrise:
            continue
        if start > covered + 1 or end <= covered:
            raise ValueError("Forecast does not continuously cover now through sunrise")
        if start < covered - 1 and covered > now:
            raise ValueError("Forecast intervals overlap")
        start, end = max(covered, start), min(float(sunrise), end)
        result.append(
            {
                **hour,
                "ts": end,
                "duration_h": (end - start) / 3600,
                "solar_w": _finite(hour.get("solar_w"), minimum=0),
                "load_w": _finite(hour.get("load_w"), minimum=0),
            }
        )
        covered = end
        if covered == sunrise:
            break
    if abs(covered - sunrise) > 1:
        raise ValueError("Forecast ends before sunrise")
    return result


def build_schedule(
    *,
    forecast,
    current_soc_pct,
    capacity_wh,
    now,
    sunrise,
    target,
    reserve,
    power_w,
    budget_seconds,
    cost_plan,
    tz_offset_seconds,
    charge_not_before_ts,
    unavailable,
    margin_seconds,
    committed_until_ts=None,
):
    """Repair reserve first, then sunrise SOC, before preferring lower-cost slots.

    The bounded greedy search reports when it cannot validate a feasible plan;
    it does not claim global cost optimality or guaranteed physical feasibility.
    """
    soc = _finite(current_soc_pct, minimum=0, maximum=100)
    capacity = _finite(capacity_wh, minimum=1)
    power = _finite(power_w, minimum=1)
    efficiency = _finite(forecast.get("charge_efficiency") or 0.90, minimum=0.01, maximum=1)
    hours = _hours(forecast, now, sunrise)
    bands = (forecast.get("drain_calibration") or {}).get("soc_bands") or []
    ceiling = forecast.get("charge_ceiling_pct")
    if ceiling is not None:
        ceiling = _finite(ceiling, minimum=0, maximum=100)
    not_before = (
        max(now, _finite(charge_not_before_ts)) if charge_not_before_ts is not None else now
    )
    slots = []
    for hour in hours:
        end = int(hour["ts"])
        start = max(now, round(hour["ts"] - hour["duration_h"] * 3600), math.ceil(not_before))
        if not unavailable and start < end:
            rate = _finite(rate_at(cost_plan, start, tz_offset_seconds), minimum=0)
            slots.append({"start": start, "end": end, "rate": rate})

    def project(intervals):
        split_hours = []
        for hour in hours:
            start = hour["ts"] - hour["duration_h"] * 3600
            boundaries = sorted(
                {
                    start,
                    hour["ts"],
                    *(
                        edge
                        for interval in intervals
                        for edge in (interval["start"], interval["end"])
                        if start < edge < hour["ts"]
                    ),
                }
            )
            for begin, end in pairwise(boundaries):
                grid = (
                    power
                    if any(interval["start"] <= begin < interval["end"] for interval in intervals)
                    else 0
                )
                split_hours.append(
                    {
                        **hour,
                        "ts": end,
                        "duration_h": (end - begin) / 3600,
                        "solar_w": hour["solar_w"] + grid,
                    }
                )
        trace = forecaster.simulate_soc(
            soc,
            capacity,
            split_hours,
            charge_efficiency=efficiency,
            soc_ceiling_pct=ceiling,
            drain_calibration_bands=bands,
            round_soc=False,
        )
        return trace

    def value_at(trace, timestamp):
        previous_time, previous_soc = now, soc
        for hour in trace:
            if hour["ts"] >= timestamp:
                fraction = (timestamp - previous_time) / (hour["ts"] - previous_time)
                return previous_soc + fraction * (hour["predicted_soc"] - previous_soc)
            previous_time, previous_soc = hour["ts"], hour["predicted_soc"]
        return previous_soc

    ideal = project(slots)
    baseline = project([])

    def violation(trace):
        for hour in trace:
            achievable = min(reserve, value_at(ideal, hour["ts"]))
            if hour["predicted_soc"] + SOC_EPSILON < achievable:
                return hour["ts"]
        return None

    selected = []
    unused = list(range(len(slots)))
    remaining = max(0, budget_seconds)
    trace = baseline
    retained_interval = None
    if committed_until_ts is not None:
        committed_until_ts = _finite(committed_until_ts)
        baseline_covered = (
            min(soc, *(hour["predicted_soc"] for hour in baseline)) >= reserve - SOC_EPSILON
            and baseline[-1]["predicted_soc"] >= target - SOC_EPSILON
        )
        if (
            committed_until_ts > now
            and not_before <= now
            and remaining >= 1
            and not baseline_covered
        ):
            for index, slot in enumerate(slots):
                if slot["start"] <= now < slot["end"]:
                    end = math.floor(min(committed_until_ts, slot["end"], now + remaining))
                    if end > now:
                        retained_interval = {"start": now, "end": end}
                        selected.append(retained_interval)
                        remaining -= end - now
                        if end == slot["end"]:
                            unused.remove(index)
                        else:
                            slot["start"] = end
                        trace = project(selected)
                    break
    while unused and remaining >= 1:
        breach = violation(trace)
        if breach is None and trace[-1]["predicted_soc"] >= target - SOC_EPSILON:
            break
        metric_time = breach if breach is not None else sunrise
        previous = value_at(trace, metric_time)
        choices = []
        for index in unused:
            slot = slots[index]
            if slot["start"] >= metric_time:
                continue
            duration = min(slot["end"] - slot["start"], int(remaining))
            start = slot["start"] if breach is not None else slot["end"] - duration
            interval = {"start": start, "end": start + duration}
            projected = project([*selected, interval])
            gain = value_at(projected, metric_time) - previous
            if gain <= SOC_EPSILON:
                continue
            price = slot["rate"] * power * duration / 3600000
            margin = slot["start"] >= sunrise - margin_seconds and breach is None
            choices.append(
                ((margin, round(price / gain, 9), -slot["start"]), index, interval, projected)
            )
        if not choices:
            break
        _, index, interval, trace = min(choices, key=lambda choice: choice[0])
        selected.append(interval)
        unused.remove(index)
        remaining -= interval["end"] - interval["start"]

    feasible = violation(trace) is None and trace[-1]["predicted_soc"] >= target - SOC_EPSILON
    if feasible:
        expensive_first = sorted(
            selected,
            key=lambda interval: (
                rate_at(cost_plan, interval["start"], tz_offset_seconds),
                -interval["start"],
            ),
            reverse=True,
        )
        for interval in expensive_first:
            if interval is retained_interval:
                continue
            candidate = [item for item in selected if item is not interval]
            projected = project(candidate)
            if (
                violation(projected) is None
                and projected[-1]["predicted_soc"] >= target - SOC_EPSILON
            ):
                selected, trace = candidate, projected
    selected.sort(key=lambda interval: interval["start"])
    minimum = min([soc, *(hour["predicted_soc"] for hour in trace)])
    scheduled_sunrise = trace[-1]["predicted_soc"]
    warnings = []
    if minimum < reserve - SOC_EPSILON:
        warnings.append(
            f"Reserve {reserve:g}% cannot be maintained by this plan (projected minimum {minimum:.1f}%)"
        )
    if scheduled_sunrise < target - SOC_EPSILON:
        warnings.append(
            f"Sunrise target {target:g}% is not achievable by this plan (projected {scheduled_sunrise:.1f}%); check charging power, duration budget and holds"
        )
    active = any(interval["start"] <= now < interval["end"] for interval in selected)
    if budget_seconds < 1:
        reason = "charging duration budget exhausted"
    elif unavailable:
        reason = "charging unavailable while appliance/manual hold is active"
    elif active:
        reason = "charging now: protecting reserve and sunrise target"
    elif selected:
        reason = "waiting for next validated charge interval; reserve and sunrise path checked"
    else:
        reason = "AC not needed; reserve and sunrise target are covered"
    if warnings:
        reason = "best-effort plan (constraints unmet): " + reason + "; " + "; ".join(warnings)
    unserved_wh = 0.0
    previous_soc = soc
    for hour in baseline:
        net = hour["net_w"] * (efficiency if hour["net_w"] > 0 else 1)
        energy = previous_soc * capacity / 100 + net * hour["duration_h"]
        unserved_wh += max(0, -energy)
        previous_soc = hour["predicted_soc"]
    deficit = max(0, (target - baseline[-1]["predicted_soc"]) * capacity / 100 + unserved_wh) / 1000
    return {
        "action": "on" if active else "off",
        "reason": reason,
        "planned_intervals": selected,
        "planned_hours": sorted({interval["start"] // 3600 * 3600 for interval in selected}),
        "window_start": selected[0]["start"] if selected else None,
        "window_end": selected[-1]["end"] if selected else None,
        "cheapest_rate": max(
            (rate_at(cost_plan, interval["start"], tz_offset_seconds) for interval in selected),
            default=None,
        ),
        "scheduled_sunrise_soc_pct": round(scheduled_sunrise, 1),
        "scheduled_min_soc_pct": round(minimum, 1),
        "retained_charge_until_ts": retained_interval["end"] if retained_interval else None,
        "reserve_soc_pct": reserve,
        "plan_feasible": not warnings,
        "warnings": warnings,
        "trajectory_validated": True,
        "deficit_kwh": round(deficit, 3),
        "extension_active": active and now >= sunrise - margin_seconds,
    }
