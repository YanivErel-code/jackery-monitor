"""Plain-language overview for the V2 dashboard.

Pure functions: the server gathers the latest smart-charge decision, its
saved inputs and a few history rows, and this module turns them into the
status headline, plan summary, rate strip and activity list the page
renders. Nothing here touches the database, the plug or the clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import cost as cost_module

HOUR_S = 3600
MERGE_GAP_S = 90          # consecutive planned slots closer than this are one block
PROJECTION_HOURS = 30
RATE_LOOKBACK_HOURS = 6     # the battery chart shows this much history
DEFAULT_CHARGE_EFFICIENCY = 0.87
MAX_ACTIVITY = 8


def _num(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def clock(ts: float, tz_offset_seconds: int) -> str:
    """Local wall-clock time as "7 AM" or "7:05 AM"."""
    local = datetime.fromtimestamp(ts + tz_offset_seconds, tz=timezone.utc)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour} {suffix}" if local.minute == 0 else f"{hour}:{local.minute:02d} {suffix}"


def money(rate: float) -> str:
    return f"${rate:.2f}"


def merge_intervals(intervals, now: float) -> list[dict]:
    """Planned slots that have not ended, with touching slots joined."""
    blocks: list[dict] = []
    for item in sorted(intervals or [], key=lambda i: _num(i.get("start"), 0)):
        start, end = _num(item.get("start")), _num(item.get("end"))
        if start is None or end is None or end <= now or end <= start:
            continue
        if blocks and start - blocks[-1]["end"] <= MERGE_GAP_S:
            blocks[-1]["end"] = max(blocks[-1]["end"], end)
        else:
            blocks.append({"start": start, "end": end})
    return blocks


def _tier(rate: float, rates: set[float]) -> str:
    if rate <= min(rates):
        return "low"
    return "peak" if rate >= max(rates) else "mid"


def rate_segments(plan: dict, start: float, hours: int, tz_offset_seconds: int) -> list[dict]:
    """Hour-aligned rate bands covering `hours` from `start`, equal neighbours joined."""
    first = int(start // HOUR_S) * HOUR_S
    hourly = [(first + i * HOUR_S, cost_module.rate_at(plan, first + i * HOUR_S, tz_offset_seconds))
              for i in range(hours + 1)]
    rates = {rate for _, rate in hourly}
    segments: list[dict] = []
    for ts, rate in hourly:
        if segments and segments[-1]["rate"] == rate:
            segments[-1]["end"] = ts + HOUR_S
        else:
            segments.append({"start": ts, "end": ts + HOUR_S, "rate": rate, "tier": _tier(rate, rates)})
    segments[0]["start"] = start
    segments[-1]["end"] = min(segments[-1]["end"], start + hours * HOUR_S)
    return segments


def grid_today(plan: dict, hourly_rows, day_start: float, tz_offset_seconds: int) -> dict:
    """Today's grid purchases split by the day's rate tiers."""
    rates = {cost_module.rate_at(plan, day_start + h * HOUR_S, tz_offset_seconds) for h in range(24)}
    tiers = {rate: {"rate": rate, "tier": _tier(rate, rates), "kwh": 0.0, "cost": 0.0} for rate in rates}
    for row in hourly_rows or []:
        ts, wh = _num(row.get("ts")), _num(row.get("grid_wh"), _num(row.get("ac_input_wh"), 0.0))
        if ts is None or ts < day_start or not wh or wh <= 0:
            continue
        rate = cost_module.rate_at(plan, ts, tz_offset_seconds)
        tier = tiers.setdefault(rate, {"rate": rate, "tier": _tier(rate, rates | {rate}), "kwh": 0.0, "cost": 0.0})
        tier["kwh"] += wh / 1000.0
        tier["cost"] += wh / 1000.0 * rate
    ordered = sorted(tiers.values(), key=lambda t: t["rate"])
    for tier in ordered:
        tier["kwh"], tier["cost"] = round(tier["kwh"], 2), round(tier["cost"], 2)
    return {"kwh": round(sum(t["kwh"] for t in ordered), 2),
            "cost": round(sum(t["cost"] for t in ordered), 2),
            "tiers": ordered}


def projection(snapshot: dict, blocks: list[dict], now: float, soc_pct: float | None) -> list[dict]:
    """Battery path: the scheduler's validated trajectory, then the saved forecast.

    Up to sunrise this is the path the scheduler checked, so the line agrees
    with the sunrise and lowest-level numbers shown beside it. Past the end of
    that path (and for snapshots saved without one) the hourly no-charge rows
    are re-integrated, which is only approximate. Each row is stamped with the
    END of the interval it covers.
    """
    capacity = _num(snapshot.get("capacity_wh"))
    if soc_pct is None or not capacity:
        return []
    charge_w = _num((snapshot.get("charge_rate") or {}).get("value"), 0.0)
    efficiency = _num((snapshot.get("model") or {}).get("charge_efficiency"), DEFAULT_CHARGE_EFFICIENCY)
    points = [{"ts": now, "soc": round(soc_pct, 1)}]
    soc = soc_pct
    covered_until = now
    for point in (snapshot.get("schedule_projection") or {}).get("scheduled_trajectory") or []:
        ts, value = _num(point.get("ts")), _num(point.get("soc"))
        if ts is None or value is None or ts <= now or ts > now + PROJECTION_HOURS * HOUR_S:
            continue
        soc, covered_until = value, ts
        points.append({"ts": ts, "soc": round(soc, 1)})
    for row in snapshot.get("baseline_forecast") or []:
        end, hours, net_w = _num(row.get("ts")), _num(row.get("duration_h"), 0.0), _num(row.get("net_w"), 0.0)
        if end is None or end <= covered_until or end > now + PROJECTION_HOURS * HOUR_S:
            continue
        start = max(covered_until, end - hours * HOUR_S)
        charging_s = sum(max(0.0, min(end, b["end"]) - max(start, b["start"])) for b in blocks)
        wh = net_w * (end - start) / HOUR_S + charge_w * efficiency * charging_s / HOUR_S
        soc = max(0.0, min(100.0, soc + wh / capacity * 100.0))
        points.append({"ts": end, "soc": round(soc, 1)})
    return points


def _kind(row: dict) -> str:
    if str(row.get("reason") or "").startswith("LG "):
        return "hold"
    return "charging" if row.get("action") == "on" and row.get("executed") else "idle"


def activity(rows, tz_offset_seconds: int) -> list[dict]:
    """Collapse per-tick decisions (oldest first) into the moments something changed."""
    events: list[dict] = []
    previous = None
    run_start = None
    for row in rows or []:
        kind = _kind(row)
        if previous is None:
            previous, run_start = kind, row
            continue
        if kind == previous:
            continue
        ts = row["decided_at"]
        if previous == "charging":
            minutes = round((ts - run_start["decided_at"]) / 60)
            a, b = _num(run_start.get("current_soc_pct")), _num(row.get("current_soc_pct"))
            detail = f"{minutes} minutes"
            if a is not None and b is not None:
                detail += f" · {a:.0f}% to {b:.0f}%"
            events.append({"ts": ts, "kind": "charge_end", "title": "Charge finished", "detail": detail})
        if previous == "hold" and kind != "hold":
            minutes = round((ts - run_start["decided_at"]) / 60)
            events.append({"ts": ts, "kind": "hold_end", "title": "Laundry finished",
                           "detail": f"Charging allowed again after {minutes} minutes on hold."})
        if kind == "charging":
            events.append({"ts": ts, "kind": "charge_start", "title": "Charge started",
                           "detail": str(row.get("reason") or "")[:140]})
        elif kind == "hold":
            events.append({"ts": ts, "kind": "hold_start", "title": "Charging held for laundry",
                           "detail": "The plug stays off while the washer or dryer runs."})
        previous, run_start = kind, row
    return list(reversed(events))[:MAX_ACTIVITY]


def _names(lg: dict, aliases: dict[str, str]) -> str:
    active = [aliases.get(a.get("device_id"), "") for a in lg.get("appliances") or []
              if a.get("activity") == "active"]
    names = [name.lower() for name in active if name]
    if not names:
        return "laundry"
    return "the " + (" and ".join(names) if len(names) < 3 else ", ".join(names))


def headline(*, mode: str, decision: dict | None, snapshot: dict, blocks: list[dict],
             now: float, plan: dict, tz: int, aliases: dict[str, str]) -> dict:
    """One sentence for "is it OK and what happens next", plus a supporting line."""
    if decision is None or mode != "active":
        return {"state": "manual", "tone": "info",
                "title": "Automatic charging is off.",
                "detail": "Turn on smart charge under Rules & plugs to plan grid charging around your rates."}
    proj = snapshot.get("schedule_projection") or {}
    lg = snapshot.get("lg_protection") or {}
    reserve = _num(proj.get("reserve_soc_pct"), _num((snapshot.get("config") or {}).get("reserve_soc_pct"), 0.0))
    target = _num(decision.get("target_sunrise_soc_pct"))
    low, sunrise_soc = _num(proj.get("scheduled_min_soc_pct")), _num(proj.get("scheduled_sunrise_soc_pct"))
    sunrise_ts = _num(decision.get("sunrise_ts"))
    reserve_risk = proj.get("plan_feasible") is False and low is not None and low < reserve
    window_rates = {cost_module.rate_at(plan, now + h * HOUR_S, tz) for h in range(25)}
    cheapest = min(window_rates)

    def outlook() -> str:
        parts = []
        if low is not None and sunrise_soc is not None and sunrise_ts:
            parts.append(f"The battery should dip to about {low:.0f}% and be at {sunrise_soc:.0f}% "
                         f"by sunrise at {clock(sunrise_ts, tz)}.")
        if blocks:
            rates = {cost_module.rate_at(plan, t, tz) for b in blocks
                     for t in range(int(b["start"]), int(b["end"]), 900)}
            if rates and max(rates) <= cheapest:
                parts.append(f"Every planned hour is at the {money(cheapest)} off-peak rate.")
            elif rates:
                parts.append(f"Some planned charging is at up to {money(max(rates))}.")
        return " ".join(parts)

    if (snapshot.get("charge_verification") or {}).get("status") == "not_charging":
        return {"state": "not_charging", "tone": "alert",
                "title": "The plug is on but the battery isn't charging.",
                "detail": "Check the charger cable and the plug in the garage."}
    if lg.get("blocked_now"):
        detail = []
        finish, resume = _num(lg.get("expected_finish_ts")), _num(lg.get("charge_not_before_ts"))
        if finish and resume and not lg.get("timing_uncertain"):
            detail.append(f"Expected to finish around {clock(finish, tz)}; charging can resume at {clock(resume, tz)}.")
        at_finish = _num(lg.get("soc_at_finish_pct"))
        if at_finish is not None:
            detail.append(f"The battery should be at about {at_finish:.0f}% by then.")
        if reserve_risk and at_finish is not None:
            detail.append(f"That is below the {reserve:.0f}% reserve.")
        elif reserve_risk:
            detail.append(f"The battery is on course to fall below the {reserve:.0f}% reserve before charging "
                          "can resume. Pause the appliance or reduce other loads.")
        names = _names(lg, aliases)
        return {"state": "hold", "tone": "alert" if reserve_risk else "warn",
                "title": f"Charging is on hold while {names} {'run' if ' and ' in names or ',' in names else 'runs'}.",
                "detail": " ".join(detail)}
    if decision.get("action") == "on" and decision.get("executed"):
        current = next((b for b in blocks if b["start"] <= now < b["end"]), blocks[0] if blocks else None)
        until = f" until {clock(current['end'], tz)}" if current else ""
        rate = cost_module.rate_at(plan, now, tz)
        if rate > cheapest:
            return {"state": "charging_peak", "tone": "warn",
                    "title": f"Charging now at the {money(rate)} rate to protect the {reserve:.0f}% reserve.",
                    "detail": outlook()}
        return {"state": "charging", "tone": "ok",
                "title": f"Charging from the grid at {money(rate)}{until}.",
                "detail": outlook()}
    tone = "alert" if reserve_risk else "ok"
    if tone == "ok" and target is not None and sunrise_soc is not None and sunrise_soc < target - 1:
        tone = "warn"
    extra = f" The plan cannot keep the {reserve:.0f}% reserve." if reserve_risk else ""
    if blocks:
        return {"state": "scheduled", "tone": tone,
                "title": f"Not charging. Next charge starts at {clock(blocks[0]['start'], tz)}.",
                "detail": outlook() + extra}
    no_charge = _num(decision.get("predicted_sunrise_soc_pct"))
    detail = (f"Solar should carry the battery to about {no_charge:.0f}% by sunrise."
              if no_charge is not None else "")
    return {"state": "solar_enough", "tone": tone,
            "title": "No grid charging needed.", "detail": detail + extra}


def build_overview(*, now: float, device_sn: str, decision: dict | None, snapshot: dict | None,
                   config: dict, cost_plan: dict, tz_offset_seconds: int,
                   decision_rows=None, hourly_rows=None, day_start: float,
                   appliance_aliases: dict[str, str] | None = None) -> dict[str, Any]:
    snapshot = snapshot or {}
    aliases = appliance_aliases or {}
    tz = tz_offset_seconds
    mode = str(config.get("mode") or "off")
    blocks = merge_intervals(snapshot.get("planned_intervals"), now) if mode == "active" else []
    proj = snapshot.get("schedule_projection") or {}
    lg = snapshot.get("lg_protection") or {}
    soc = _num((decision or {}).get("current_soc_pct"), _num(snapshot.get("system_soc_pct")))
    capacity = _num(snapshot.get("capacity_wh"))
    charge_w = _num((snapshot.get("charge_rate") or {}).get("value"), 0.0)

    # On a bypass device the grid also carries the home while charging, so
    # the purchase is the battery's charge plus the forecast home use.
    bypass = config.get("ac_input_excludes_load") is True
    hours = [(_num(r.get("ts"), 0.0), _num(r.get("duration_h"), 0.0), _num(r.get("output_w"), _num(r.get("load_w"), 0.0)))
             for r in snapshot.get("baseline_forecast") or []] if bypass else []

    supply_w = _num((snapshot.get("outlet_supply") or {}).get("value")) if bypass else None

    def home_w(ts: float) -> float:
        return next((w for end, dur, w in hours if end - dur * HOUR_S <= ts < end), 0.0)

    est_kwh = est_cost = 0.0
    for block in blocks:
        cursor = max(block["start"], now)
        while cursor < block["end"]:
            step = min(block["end"], (int(cursor // 900) + 1) * 900) - cursor
            wall_w = charge_w + home_w(cursor)
            kwh = (min(wall_w, supply_w) if supply_w else wall_w) * step / HOUR_S / 1000.0
            est_kwh += kwh
            est_cost += kwh * cost_module.rate_at(cost_plan, cursor, tz)
            cursor += step

    sunrise_ts = _num((decision or {}).get("sunrise_ts"))
    no_charge = _num((decision or {}).get("predicted_sunrise_soc_pct"))
    # Lowest level on the way to sunrise if the plug stayed off.
    no_charge_low = min(
        [v for v in [soc, no_charge] if v is not None]
        + [_num(r.get("predicted_soc"), 100.0) for r in snapshot.get("baseline_forecast") or []
           if sunrise_ts and now < _num(r.get("ts"), 0.0) <= sunrise_ts],
        default=None)

    return {
        "device_sn": device_sn,
        "now": now,
        "tz_offset_seconds": tz,
        "mode": mode,
        "decided_at": (decision or {}).get("decided_at"),
        "headline": headline(mode=mode, decision=decision, snapshot=snapshot, blocks=blocks,
                             now=now, plan=cost_plan, tz=tz, aliases=aliases),
        "battery": {
            "soc_pct": soc,
            "capacity_wh": capacity,
            "reserve_pct": _num(config.get("reserve_soc_pct")),
            "target_pct": _num(config.get("target_sunrise_soc_pct")),
        },
        "plan": {
            "blocks": blocks,
            "charging_now": bool(decision and decision.get("action") == "on" and decision.get("executed")),
            "minutes": round(sum(b["end"] - max(b["start"], now) for b in blocks) / 60),
            "est_kwh": round(est_kwh, 1),
            "est_cost": round(est_cost, 2),
            "sunrise_ts": (decision or {}).get("sunrise_ts"),
            "sunrise_soc_pct": _num(proj.get("scheduled_sunrise_soc_pct")),
            "min_soc_pct": _num(proj.get("scheduled_min_soc_pct")),
            "no_charge_sunrise_soc_pct": no_charge,
            "no_charge_min_soc_pct": no_charge_low,
            "feasible": proj.get("plan_feasible"),
            "warnings": list(proj.get("warnings") or []),
        },
        "rates": rate_segments(cost_plan, now - RATE_LOOKBACK_HOURS * HOUR_S,
                               RATE_LOOKBACK_HOURS + 24, tz),
        "laundry": {
            "enabled": bool(config.get("lg_interlock_enabled")),
            "blocked": bool(lg.get("blocked_now")),
            "names": _names(lg, aliases) if lg.get("blocked_now") else None,
            "expected_finish_ts": lg.get("expected_finish_ts"),
            "charge_not_before_ts": lg.get("charge_not_before_ts"),
            "monitoring_degraded": bool(lg.get("monitoring_degraded")),
        },
        "verification": {"status": (snapshot.get("charge_verification") or {}).get("status")},
        "projection": projection(snapshot, blocks, now, soc) if mode == "active" else [],
        "grid_today": grid_today(cost_plan, hourly_rows, day_start, tz),
        "activity": activity(decision_rows, tz),
        "config": {key: config.get(key) for key in (
            "target_sunrise_soc_pct", "reserve_soc_pct", "kasa_device_host",
            "ac_supply_limit_w", "lg_interlock_enabled", "max_charge_w")}
        | {"measured_supply_w": _num((snapshot.get("outlet_supply") or {}).get("value"))},
    }
