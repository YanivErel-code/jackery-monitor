"""Pure, bounded charge-schedule construction using the existing SOC simulator."""

from __future__ import annotations

import math
from itertools import pairwise

import forecaster
from cost import rate_at

SOC_EPSILON = 1e-7
SCHEDULE_POLICY = "overnight_first_v1"
# A started charge run is held at least this long. Small top-ups otherwise end
# with the 15-minute nowcast slot they began in and restart minutes later
# (20 of 34 ON runs in the week to 2026-11-20 lasted 15 minutes or less).
MIN_CHARGE_BLOCK_S = 45 * 60


# A trimmed interval ends on this grid and leaves this much above the sunrise
# target, so a small forecast drift does not restart charging minutes later.
TRIM_STEP_S = 300
TRIM_HEADROOM_PCT = 1.0
# An idle plug stays off while the forecast sits this close under the sunrise
# target, and a running charge carries on to the trim headroom above it.
# Together they stop a forecast wobbling around the target from switching the
# plug (a six-minute run at 04:05 on 2026-11-27 for a 0.2-point dip).
RESTART_DEADBAND_PCT = 1.0


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
    min_block_until_ts=None,
    charging_now=False,
):
    """Prefer earliest low-rate overnight charging while preserving a valid SOC path."""
    inputs = {
        "forecast": forecast,
        "current_soc_pct": current_soc_pct,
        "capacity_wh": capacity_wh,
        "now": now,
        "sunrise": sunrise,
        "target": target,
        "reserve": reserve,
        "power_w": power_w,
        "budget_seconds": budget_seconds,
        "cost_plan": cost_plan,
        "tz_offset_seconds": tz_offset_seconds,
        "charge_not_before_ts": charge_not_before_ts,
        "unavailable": unavailable,
        "margin_seconds": margin_seconds,
        "committed_until_ts": committed_until_ts,
        "min_block_until_ts": min_block_until_ts,
        "charging_now": charging_now,
    }
    result = _build_schedule(**inputs, overnight_first=True)
    preference_relaxed = False
    if not result["plan_feasible"]:
        alternative = _build_schedule(**inputs, overnight_first=False)
        if alternative["plan_feasible"]:
            result = alternative
            preference_relaxed = True
    result["schedule_policy"] = SCHEDULE_POLICY
    overnight_start = _overnight_start(sunrise, tz_offset_seconds)
    intervals = result["planned_intervals"]
    if preference_relaxed:
        prefix = "overnight preference relaxed to find a feasible reserve/sunrise plan"
    elif any(interval["start"] < overnight_start for interval in intervals):
        if result["retained_charge_until_ts"] is not None and now < overnight_start:
            prefix = "continuing a validated earlier-charge interval; overnight-first for remaining demand"
        else:
            prefix = "overnight-first plan includes earlier charging for its reserve/sunrise path"
    else:
        prefix = "overnight-first: prefer earliest low-rate hours from local midnight"
    result["reason"] = prefix + "; " + result["reason"]
    return result


def _overnight_start(sunrise, tz_offset_seconds):
    offset = _finite(tz_offset_seconds, minimum=-14 * 3600, maximum=14 * 3600)
    return math.floor((sunrise + offset) / 86400) * 86400 - offset


def _build_schedule(
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
    committed_until_ts,
    min_block_until_ts,
    charging_now,
    overnight_first,
):
    """Repair reserve and sunrise SOC before applying the requested slot preference.

    The bounded greedy search reports when it cannot validate a feasible plan;
    it does not claim global cost optimality or guaranteed physical feasibility.
    """
    soc = _finite(current_soc_pct, minimum=0, maximum=100)
    capacity = _finite(capacity_wh, minimum=1)
    power = _finite(power_w, minimum=1)
    efficiency = _finite(forecast.get("charge_efficiency") or 0.90, minimum=0.01, maximum=1)
    bypass = forecast.get("ac_input_excludes_load") is True
    supply_limit = (_finite(forecast.get("ac_supply_limit_w"), minimum=50, maximum=20000)
                    if bypass else None)
    hours = _hours(forecast, now, sunrise)
    if bypass:
        for hour in hours:
            hour["output_w"] = _finite(hour.get("output_w", hour["load_w"]), minimum=0)
    overnight_start = _overnight_start(sunrise, tz_offset_seconds)
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
        if (not unavailable and start < end
                and (not bypass or hour["output_w"] < supply_limit)):
            boundaries = (
                [start, overnight_start, end] if start < overnight_start < end else [start, end]
            )
            for begin, finish in pairwise(boundaries):
                rate = _finite(rate_at(cost_plan, begin, tz_offset_seconds), minimum=0)
                slots.append({"start": begin, "end": finish, "rate": rate})

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
                point = {
                    **hour,
                    "ts": end,
                    "duration_h": (end - begin) / 3600,
                    "solar_w": hour["solar_w"] + (grid if not bypass else 0),
                }
                if bypass and grid:
                    battery_charge = min(grid, max(0.0, supply_limit - hour["output_w"]))
                    point.update(ac_bypass_active=True, ac_battery_charge_w=battery_charge,
                                 ac_wall_input_w=battery_charge + hour["output_w"])
                split_hours.append(point)
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

    def wall_wh(points):
        return sum(float(hour.get("ac_wall_input_w") or 0) * hour["duration_h"] for hour in points)

    ideal = project(slots)
    baseline = project([])

    def violation(trace):
        for hour in trace:
            achievable = min(reserve, value_at(ideal, hour["ts"]))
            if hour["predicted_soc"] + SOC_EPSILON < achievable:
                return hour["ts"]
        return None

    # `accept` is the sunrise level the plan must reach to count as feasible;
    # `stop_at` is where it stops adding (and keeps) charging.
    accept = stop_at = target
    if charging_now:
        stop_at = target + TRIM_HEADROOM_PCT
    elif (violation(baseline) is None
          and baseline[-1]["predicted_soc"] >= target - RESTART_DEADBAND_PCT - SOC_EPSILON):
        accept = stop_at = min(target, baseline[-1]["predicted_soc"])
    selected = []
    end_anchored = []
    unused = list(range(len(slots)))
    remaining = max(0, budget_seconds)
    trace = baseline
    retained_interval = None
    if committed_until_ts is not None:
        committed_until_ts = _finite(committed_until_ts)
    if min_block_until_ts is not None and slots:
        # The minimum block only moves charging earlier inside the cheapest
        # remaining tariff tier; it never buys dearer energy than the plan
        # would, and it obeys the same hold, budget and coverage gates below.
        min_block_until_ts = _finite(min_block_until_ts)
        current = next((slot for slot in slots if slot["start"] <= now < slot["end"]), None)
        if current is not None and current["rate"] <= min(slot["rate"] for slot in slots):
            committed_until_ts = max(committed_until_ts or min_block_until_ts, min_block_until_ts)
    if committed_until_ts is not None:
        baseline_covered = (
            min(soc, *(hour["predicted_soc"] for hour in baseline)) >= reserve - SOC_EPSILON
            and baseline[-1]["predicted_soc"] >= accept - SOC_EPSILON
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
        if breach is None and trace[-1]["predicted_soc"] >= stop_at - SOC_EPSILON:
            break
        metric_time = breach if breach is not None else sunrise
        previous = value_at(trace, metric_time)
        choices = []
        for index in unused:
            slot = slots[index]
            if slot["start"] >= metric_time:
                continue
            duration = min(slot["end"] - slot["start"], int(remaining))
            from_start = breach is not None or (
                overnight_first and slot["start"] >= overnight_start)
            start = slot["start"] if from_start else slot["end"] - duration
            interval = {"start": start, "end": start + duration}
            projected = project([*selected, interval])
            gain = value_at(projected, metric_time) - previous
            if gain <= SOC_EPSILON:
                continue
            if bypass:
                price = slot["rate"] * (wall_wh(projected) - wall_wh(trace)) / 1000
            else:
                price = slot["rate"] * power * duration / 3600000
            margin = slot["start"] >= sunrise - margin_seconds and breach is None
            if overnight_first and slot["start"] >= overnight_start:
                priority = (0, slot["rate"], slot["start"], round(price / gain, 9))
            elif overnight_first and breach is None:
                # Energy the overnight hours cannot supply is bought as late
                # as possible, so it joins the overnight block and is still
                # open to revision. A dawn forecast of the next sunrise runs
                # far too low (9% forecast, about 40% real on 2026-11-26) and
                # used to buy a morning top-up that was never needed.
                priority = (1, -slot["start"])
            elif overnight_first:
                # Protecting the reserve: cheapest tariff first, and within
                # it the slot closest before the dip, so the purchase can
                # still be withdrawn as the forecast firms up. Ranking by
                # modelled cost per point instead favoured sunless hours
                # (grid power there also spares the battery the home's
                # draw) and bought "right now" at 07:00-10:00, hours ahead
                # of dips the forecast later dropped (2026-11-11 to 11-21).
                priority = (1, slot["rate"], margin, -slot["start"])
            else:
                priority = (round(price / gain, 9), margin, -slot["start"])
            choices.append((priority, index, interval, projected, from_start))
        if not choices:
            break
        _, index, interval, trace, from_start = min(choices, key=lambda choice: choice[0])
        selected.append(interval)
        if not from_start:
            end_anchored.append(interval)
        unused.remove(index)
        remaining -= interval["end"] - interval["start"]

    feasible = violation(trace) is None and trace[-1]["predicted_soc"] >= accept - SOC_EPSILON
    if feasible:
        expensive_first = sorted(
            selected,
            key=lambda interval: (
                interval["start"] < overnight_start if overnight_first else False,
                rate_at(cost_plan, interval["start"], tz_offset_seconds),
                interval["start"]
                if overnight_first and interval["start"] >= overnight_start
                else -interval["start"],
            ),
            reverse=True,
        )
        # Overnight charging is one run: once a later interval has to stay,
        # the earlier ones at that rate stay too. Dropping them instead left
        # a gap after "now" on every replan, which switched the plug off
        # mid-charge and back on minutes later (five runs on 2026-11-25).
        kept_rates = set()
        for interval in expensive_first:
            if interval is retained_interval:
                continue
            overnight = overnight_first and interval["start"] >= overnight_start
            rate = rate_at(cost_plan, interval["start"], tz_offset_seconds)
            if overnight and rate in kept_rates:
                continue
            candidate = [item for item in selected if item is not interval]
            projected = project(candidate)
            if (
                violation(projected) is None
                and projected[-1]["predicted_soc"] >= min(stop_at, trace[-1]["predicted_soc"]) - SOC_EPSILON
            ):
                selected, trace = candidate, projected
            elif overnight:
                kept_rates.add(rate)
        # Whole slots usually buy more than the target needs. Shorten the
        # interval the plan values least until the path only just holds, so
        # the battery is not charged past the target.
        trimmable = [
            interval for interval in expensive_first
            if interval is not retained_interval and any(item is interval for item in selected)
        ]
        if trimmable:
            interval = trimmable[0]
            others = [item for item in selected if item is not interval]

            from_end = interval["start"] > now and any(item is interval for item in end_anchored)

            def shortened(steps):
                # An interval placed against the end of its slot keeps that
                # end, so it still runs into the charging that follows it.
                # Otherwise the start stays put: the interval may begin now
                # to protect the reserve, and only its end is free to move.
                if from_end:
                    return {"start": interval["end"] - steps * TRIM_STEP_S, "end": interval["end"]}
                return {"start": interval["start"], "end": interval["start"] + steps * TRIM_STEP_S}

            def holds(projected):
                return (violation(projected) is None
                        and projected[-1]["predicted_soc"] >= target + TRIM_HEADROOM_PCT - SOC_EPSILON)

            low, high = 1, (interval["end"] - interval["start"]) // TRIM_STEP_S
            if high >= 1 and holds(project([*others, shortened(high)])):
                while low < high:
                    middle = (low + high) // 2
                    if holds(project([*others, shortened(middle)])):
                        high = middle
                    else:
                        low = middle + 1
                if low * TRIM_STEP_S < interval["end"] - interval["start"]:
                    selected = [*others, shortened(low)]
                    trace = project(selected)
    selected.sort(key=lambda interval: interval["start"])
    minimum = min([soc, *(hour["predicted_soc"] for hour in trace)])
    scheduled_sunrise = trace[-1]["predicted_soc"]
    warnings = []
    if minimum < reserve - SOC_EPSILON:
        warnings.append(
            f"Reserve {reserve:g}% cannot be maintained by this plan (projected minimum {minimum:.1f}%)"
        )
    if scheduled_sunrise < accept - SOC_EPSILON:
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
        # The validated path itself, so displays draw the plan the scheduler
        # checked instead of re-deriving an approximation of it.
        "scheduled_trajectory": [
            {"ts": int(hour["ts"]), "soc": round(hour["predicted_soc"], 1)} for hour in trace
        ],
        "retained_charge_until_ts": retained_interval["end"] if retained_interval else None,
        "reserve_soc_pct": reserve,
        "plan_feasible": not warnings,
        "warnings": warnings,
        "trajectory_validated": True,
        "deficit_kwh": round(deficit, 3),
        "extension_active": active and now >= sunrise - margin_seconds,
    }
