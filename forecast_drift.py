"""Compare stored morning forecasts with what the battery then did.

The planner corrects its battery model by itself (forecaster.fit_model_correction).
This module is the end-to-end check on top: it takes the forecast saved each
morning, the battery level that followed, and reports when the two have stayed
apart for days, with the share owed to solar, home use and the rest.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from typing import Any

START_HOUR = 8            # local hour of the forecast that is judged
DAY_CHECK_H = 9           # 5 PM the same day
NEXT_CHECK_H = 23         # 7 AM the next day
WINDOW_DAYS = 7
MIN_DAYS = 5
DRIFT_POINTS = 8.0        # mean error, in battery points, that counts as drift
MIN_SAME_SIGN = 5         # days that must err in the same direction


def _predicted_at(snapshot: dict, start_ts: float, start_soc: float, ts: float) -> float | None:
    previous_ts, previous_soc = start_ts, start_soc
    for point in snapshot.get("baseline_forecast") or []:
        end, soc = point.get("ts"), point.get("predicted_soc")
        if end is None or soc is None or end <= previous_ts:
            continue
        if end >= ts:
            return previous_soc + (soc - previous_soc) * (ts - previous_ts) / (end - previous_ts)
        previous_ts, previous_soc = end, soc
    return None


def day_error(snapshot: dict, *, made_at: float, start_ts: int, start_soc: float,
              hours: list[dict], soc_at: Callable[[int], float | None],
              car_load_w: float) -> dict[str, Any] | None:
    """One day's forecast error, with grid and car charging taken out.

    `hours` are the hourly history rows from `start_ts`. The saved forecast
    plans no grid charging and no car charging, so the energy those moved is
    removed from the measured level before the two are compared.
    """
    capacity = float(snapshot.get("capacity_wh") or 0)
    model = snapshot.get("model") or {}
    efficiency = float(model.get("charge_efficiency") or 0.9)
    overhead = float(model.get("inverter_overhead_pct") or 0.1)
    by_ts = {row["ts"]: row for row in hours}
    if capacity <= 0:
        return None
    # The car plug has no meter and may be on with a full car, so the car is
    # only credited with what an hour drew above the plug-off level.
    plug_off = sorted(float(row.get("output_wh") or 0) for row in hours
                      if not row.get("solar_charge_plug_on_frac"))
    plug_off_wh = plug_off[len(plug_off) // 2] if plug_off else 0.0
    out: dict[str, Any] = {"start_ts": start_ts}
    grid_wh = car_wh = solar_wh = home_wh = 0.0
    for offset in range(NEXT_CHECK_H):
        row = by_ts.get(start_ts + offset * 3600)
        if row is None:
            return None
        ac = float(row.get("ac_input_wh") or 0)
        output = float(row.get("output_wh") or 0)
        car = min(float(row.get("solar_charge_plug_on_frac") or 0) * car_load_w,
                  max(0.0, output - plug_off_wh))
        grid_wh += ac * efficiency + (output * (1 + overhead) if ac > 50 else 0)
        car_wh += car * (1 + overhead)
        solar_wh += float(row.get("solar_wh") or 0)
        home_wh += output - car
        for name, check in (("day", DAY_CHECK_H), ("next", NEXT_CHECK_H)):
            if offset + 1 != check:
                continue
            ts = start_ts + check * 3600
            actual, predicted = soc_at(ts), _predicted_at(snapshot, made_at, start_soc, ts)
            if actual is None or predicted is None:
                return None
            adjusted = actual - (grid_wh - car_wh) / capacity * 100
            out[f"error_{name}"] = predicted - adjusted
    forecast = [p for p in snapshot.get("baseline_forecast") or []
                if made_at < (p.get("ts") or 0) <= start_ts + NEXT_CHECK_H * 3600]
    solar_fc = sum(float(p.get("solar_w") or 0) * float(p.get("duration_h") or 0) for p in forecast)
    if any("output_w" not in p for p in forecast):
        return None             # saved before forecasts carried the home-use figure
    home_fc = sum(float(p["output_w"] or 0) * float(p.get("duration_h") or 0) for p in forecast)
    # Battery points each input error is worth, signed like the forecast error.
    out["solar_points"] = (solar_fc - solar_wh) * efficiency / capacity * 100
    out["home_points"] = (home_wh - home_fc) * (1 + overhead) / capacity * 100
    out["solar_kwh"] = (solar_fc - solar_wh) / 1000
    out["home_kwh"] = (home_wh - home_fc) / 1000
    return out


def assess(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether recent forecasts have stayed off in one direction, and why."""
    recent = sorted(days, key=lambda day: day["start_ts"])[-WINDOW_DAYS:]
    if len(recent) < MIN_DAYS:
        return {"status": "learning", "days": len(recent), "message": None}
    error_day = statistics.mean(day["error_day"] for day in recent)
    error_next = statistics.mean(day["error_next"] for day in recent)
    worst = max((error_day, error_next), key=abs)
    same_sign = sum((day["error_next"] > 0) == (error_next > 0) for day in recent)
    solar = statistics.mean(day["solar_points"] for day in recent)
    home = statistics.mean(day["home_points"] for day in recent)
    model = error_next - solar - home
    result = {
        "status": "ok", "days": len(recent), "message": None,
        "error_day_points": round(error_day, 1), "error_next_points": round(error_next, 1),
        "solar_points": round(solar, 1), "home_points": round(home, 1), "model_points": round(model, 1),
        "solar_kwh": round(statistics.mean(day["solar_kwh"] for day in recent), 1),
        "home_kwh": round(statistics.mean(day["home_kwh"] for day in recent), 1),
        "since_ts": recent[0]["start_ts"],
    }
    if abs(worst) < DRIFT_POINTS or same_sign < MIN_SAME_SIGN:
        return result
    direction = "high" if worst > 0 else "low"
    parts = {"solar": solar, "home": home, "model": model}
    # The cause is the part pushing hardest in the direction of the error.
    cause = max(parts, key=lambda name: parts[name] * (1 if worst > 0 else -1))
    reason = {
        "solar": (f"Solar came in {abs(result['solar_kwh']):.1f} kWh a day "
                  f"{'below' if solar > 0 else 'above'} the forecast."),
        "home": (f"Home use ran {abs(result['home_kwh']):.1f} kWh a day "
                 f"{'above' if home > 0 else 'below'} the usual pattern."),
        "model": "Solar and home use were close; the battery itself behaved differently than modelled.",
    }[cause]
    result.update(
        status="drifting", direction=direction, cause=cause,
        message=(f"Over the last {len(recent)} days the morning forecast has been about "
                 f"{abs(worst):.0f} points too {direction}. {reason}"))
    return result
