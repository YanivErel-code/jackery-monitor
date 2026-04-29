"""
Electricity cost tracking — converts the energy_db history into dollar
savings (solar production avoided buying grid power) and grid cost (what
you actually paid to charge from AC).

Plan storage: /data/cost.json. Two shapes supported:
  - Flat: {"type": "flat", "rate_per_kwh": 0.30, "currency": "USD"}
  - TOU:  {"type": "tou", "currency": "USD",
           "tou_rates": [{"start_hour": 16, "end_hour": 21,
                          "rate": 0.62, "label": "peak"}, ...]}

TOU slots are inclusive of start_hour, exclusive of end_hour, evaluated
in the device's local timezone (from /data/location.json). Slots may
wrap midnight (e.g. start=23, end=7 means 23:00-07:00).

Savings model matches the user's mental model:
  solar_savings = solar_kWh * rate(at_time)   # value of solar captured
  grid_cost     = grid_kWh   * rate(at_time)  # paid for grid charging
  net_savings   = solar_savings - grid_cost   # net dollar benefit

Grid kWh is derived as input_wh - solar_wh (car-input is almost always
zero on these devices and isn't tracked separately).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("cost")

COST_PATH = os.environ.get("JACKERY_COST_FILE", "/data/cost.json")

DEFAULT_PLAN: dict[str, Any] = {
    "type": "flat",
    "rate_per_kwh": 0.30,
    "currency": "USD",
}

# Built-in plan presets. Rates are approximate as of 2026-04 — users on
# these utilities should verify current rates and override via custom.
PRESETS: dict[str, dict[str, Any]] = {
    "flat-default": {
        "label": "Flat $0.30/kWh",
        "plan": {"type": "flat", "rate_per_kwh": 0.30, "currency": "USD"},
    },
    "pge-ev2a": {
        "label": "PG&E EV2-A (CA)",
        "plan": {
            "type": "tou",
            "currency": "USD",
            "tou_rates": [
                # Peak: 4pm-9pm, ~$0.61/kWh
                {"start_hour": 16, "end_hour": 21, "rate": 0.61, "label": "peak"},
                # Partial-peak: 3pm-4pm and 9pm-12am, ~$0.51/kWh
                {"start_hour": 15, "end_hour": 16, "rate": 0.51, "label": "partial-peak"},
                {"start_hour": 21, "end_hour": 24, "rate": 0.51, "label": "partial-peak"},
                # Off-peak: 12am-3pm, ~$0.31/kWh
                {"start_hour": 0, "end_hour": 15, "rate": 0.31, "label": "off-peak"},
            ],
        },
    },
    "pge-etouc": {
        "label": "PG&E E-TOU-C (CA)",
        "plan": {
            "type": "tou",
            "currency": "USD",
            "tou_rates": [
                {"start_hour": 16, "end_hour": 21, "rate": 0.49, "label": "peak"},
                {"start_hour": 0, "end_hour": 16, "rate": 0.40, "label": "off-peak"},
                {"start_hour": 21, "end_hour": 24, "rate": 0.40, "label": "off-peak"},
            ],
        },
    },
    "sce-touprime": {
        "label": "SCE TOU-D-PRIME (CA)",
        "plan": {
            "type": "tou",
            "currency": "USD",
            "tou_rates": [
                {"start_hour": 16, "end_hour": 21, "rate": 0.55, "label": "peak"},
                {"start_hour": 0, "end_hour": 16, "rate": 0.30, "label": "off-peak"},
                {"start_hour": 21, "end_hour": 24, "rate": 0.30, "label": "off-peak"},
            ],
        },
    },
}

_lock = threading.Lock()


def _validate(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Sanity-check a candidate plan; return a normalized copy or None."""
    if not isinstance(plan, dict):
        return None
    plan_type = plan.get("type")
    currency = str(plan.get("currency") or "USD")[:8]
    if plan_type == "flat":
        try:
            rate = float(plan.get("rate_per_kwh") or 0)
        except (TypeError, ValueError):
            return None
        if not 0 <= rate <= 5.0:
            return None
        return {"type": "flat", "rate_per_kwh": rate, "currency": currency}
    if plan_type == "tou":
        slots_in = plan.get("tou_rates") or []
        if not isinstance(slots_in, list) or not slots_in:
            return None
        slots_out: list[dict[str, Any]] = []
        for raw in slots_in:
            if not isinstance(raw, dict):
                return None
            try:
                s = int(raw.get("start_hour"))
                e = int(raw.get("end_hour"))
                rate = float(raw.get("rate"))
            except (TypeError, ValueError):
                return None
            if not (0 <= s <= 24 and 0 <= e <= 24):
                return None
            if not 0 <= rate <= 5.0:
                return None
            label = str(raw.get("label") or "")[:32]
            slots_out.append({"start_hour": s, "end_hour": e,
                              "rate": rate, "label": label})
        return {"type": "tou", "currency": currency, "tou_rates": slots_out}
    return None


def get_plan() -> dict[str, Any]:
    """Load the saved plan or fall back to DEFAULT_PLAN."""
    with _lock:
        try:
            with open(COST_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            return dict(DEFAULT_PLAN)
        except Exception as e:
            log.warning("cost plan unreadable (%s); using default", e)
            return dict(DEFAULT_PLAN)
    validated = _validate(data)
    return validated or dict(DEFAULT_PLAN)


def set_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and persist a plan. Returns the saved plan, or None on failure."""
    validated = _validate(plan)
    if validated is None:
        return None
    with _lock:
        os.makedirs(os.path.dirname(COST_PATH) or ".", exist_ok=True)
        tmp = COST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(validated, f, indent=2)
        os.replace(tmp, COST_PATH)
    log.info("cost plan saved (type=%s, currency=%s)",
             validated["type"], validated["currency"])
    return validated


def list_presets() -> list[dict[str, Any]]:
    """For the settings UI dropdown — id, label, plan."""
    return [{"id": k, **v} for k, v in PRESETS.items()]


def _hour_in_slot(hour: int, start: int, end: int) -> bool:
    """Inclusive-start, exclusive-end. Handles wraparound (start>end)."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def rate_at(plan: dict[str, Any], ts: float,
            tz_offset_seconds: int = 0) -> float:
    """Return $/kWh active at the given timestamp.

    `tz_offset_seconds` shifts the timestamp before extracting the hour
    so TOU slots are evaluated in the device's local time, not UTC.
    """
    if plan.get("type") == "flat":
        return float(plan.get("rate_per_kwh") or 0)
    if plan.get("type") == "tou":
        local = datetime.fromtimestamp(ts + tz_offset_seconds, tz=timezone.utc)
        hour = local.hour
        for slot in plan.get("tou_rates") or []:
            if _hour_in_slot(hour, int(slot["start_hour"]),
                             int(slot["end_hour"])):
                return float(slot["rate"])
    return 0.0


def compute_savings(history: list[dict[str, Any]],
                    plan: dict[str, Any] | None = None,
                    tz_offset_seconds: int = 0) -> dict[str, float]:
    """Walk hourly buckets, integrate savings/cost in dollars.

    `history` rows must have ts + solar_wh + input_wh (the energy_db
    history shape). Returns floats rounded to 2 decimals.
    """
    plan = plan or get_plan()
    solar_savings = 0.0
    grid_cost = 0.0
    solar_kwh_total = 0.0
    grid_kwh_total = 0.0
    for row in history:
        ts = row.get("ts")
        if ts is None:
            continue
        rate = rate_at(plan, float(ts), tz_offset_seconds)
        solar_kwh = float(row.get("solar_wh") or 0) / 1000.0
        input_kwh = float(row.get("input_wh") or 0) / 1000.0
        grid_kwh = max(0.0, input_kwh - solar_kwh)
        solar_savings += solar_kwh * rate
        grid_cost += grid_kwh * rate
        solar_kwh_total += solar_kwh
        grid_kwh_total += grid_kwh
    return {
        "solar_savings": round(solar_savings, 2),
        "grid_cost": round(grid_cost, 2),
        "net_savings": round(solar_savings - grid_cost, 2),
        "solar_kwh": round(solar_kwh_total, 3),
        "grid_kwh": round(grid_kwh_total, 3),
        "currency": plan.get("currency", "USD"),
    }


def lifetime_savings(history: list[dict[str, Any]],
                     plan: dict[str, Any] | None = None,
                     tz_offset_seconds: int = 0) -> dict[str, float]:
    """Alias for compute_savings — same math, just intended over the whole
    sample history. Kept separate so callers self-document intent."""
    return compute_savings(history, plan, tz_offset_seconds)


def today_savings(history_today: list[dict[str, Any]],
                  plan: dict[str, Any] | None = None,
                  tz_offset_seconds: int = 0) -> dict[str, float]:
    """Caller passes only today's hourly buckets (server filters by
    _start_of_day). Same math."""
    return compute_savings(history_today, plan, tz_offset_seconds)


__all__ = [
    "COST_PATH",
    "DEFAULT_PLAN",
    "PRESETS",
    "compute_savings",
    "get_plan",
    "lifetime_savings",
    "list_presets",
    "rate_at",
    "set_plan",
    "today_savings",
]
