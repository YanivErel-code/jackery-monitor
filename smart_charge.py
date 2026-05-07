"""
Smart grid-charge controller.

Toggles a Kasa smart plug ON/OFF to grid-charge the Jackery only when the
forecast says solar won't bridge to the next sunrise. Schedules charging
during the cheapest TOU hours in the gap.

Three modes (the user picks via Automation tab):
  - off:    no decisions, no toggling. Idle.
  - test:   decisions computed and logged, but the plug is NEVER toggled.
            Used to validate behavior over a few days before going live.
  - active: full control — toggles Kasa plug per decisions.

Decision policy is deterministic and rule-based, NOT machine-learned. The
goal is simple: SOC at sunrise must be ≥ target_sunrise_soc_pct. Overshoot
is fine; undershoot is failure.

Algorithm:
  1. Pull the *baseline* (counterfactual) forecast — what SOC would do
     with NO AC charging. If predicted_sunrise_soc ≥ target, solar/coast
     will bridge it: action=off, done.
  2. Else compute the energy deficit:
        deficit_kwh = (target - baseline_predicted) * capacity_wh / 100 / 1000
        needed_hours = ceil(deficit_kwh * 1000 / max_charge_w)
  3. Build the set of *planned ON hours* — the hours when the plug
     should be on. The window is bounded by [now, sunrise - SUNRISE_MARGIN_S]
     so charging finishes 1h before sunrise (buffer for charge-curve tail
     and forecast error). Within that window:
       * The HOUR ENDING at (sunrise - margin) is mandatory — anchor the
         schedule against the deadline.
       * Pick the cheapest (needed_hours - 1) other hours from the rest.
       * The result may be discontinuous (e.g., cheap evening + hour
         right before margin) — the plug toggles between segments.
  4. Decide:
       * if `now_hour ∈ planned_hours` → on
       * elif now ≥ (sunrise - margin) AND soc < target → on
         (post-margin extension: we're behind schedule, push past
          sunrise if needed — Q2 lock-in)
       * else → off
  5. Mid-session check: every tick the baseline forecast is recomputed
     from the *current* SOC. If baseline_predicted_sunrise rises to ≥
     target during the session, step 1 short-circuits to off — we're
     done early (Q4).
  6. Re-entry: if SOC drifts below target after a target-hit and we're
     still pre-sunrise, step 1 will once again show baseline < target
     and the planner re-fires (Q5: overshoot ok, undershoot not).

Safety:
  - Test mode lets the user observe a few days of decisions without any
    plug movement.
  - The mandatory anchor hour means a forecast error can never let the
    plug stay off through the whole night when target is at risk.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger("smart_charge")

CONFIG_PATH = os.environ.get("JACKERY_SMART_CHARGE_FILE", "/data/smart_charge.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "off",                       # off | test | active
    "kasa_device_host": None,           # which saved Kasa plug = the grid input
    "target_sunrise_soc_pct": 25,        # SOC we want at sunrise
    # AC charging rate the unit will pull from the wall while the plug
    # is on. Per-model defaults live in models.json under
    # default_max_charge_w (or per-entry charging_modes_w["fast"]); the
    # server picks the right one when persisting a fresh config for a
    # new device. The 800W here is just the cold-start fallback for
    # users on a model the catalog doesn't know yet — they should tune
    # it to whatever their unit actually pulls.
    "max_charge_w": 800,
    "max_on_duration_minutes": 480,      # safety cap per dusk-dawn cycle
    "claude_enabled": False,             # optional decision narrator
}

# Decisions are persisted to energy_db.smart_charge_decisions by the server,
# not held in-memory here. That way they survive container restarts and can
# be joined to the actual SOC samples for predicted-vs-actual analytics.

# Charging finishes this many seconds before sunrise. Buffer for charge-
# curve tail-off (last few % charge slowly), Kasa toggle latency, and
# forecast error. 1h was the user's pick; tweak per device if needed.
SUNRISE_MARGIN_S = 3600

_config_lock = threading.Lock()


# ---------- Config persistence ----------
def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Coerce a config dict into a normalized shape; bad fields fall back
    to defaults."""
    out = dict(DEFAULT_CONFIG)
    if not isinstance(cfg, dict):
        return out
    mode = str(cfg.get("mode") or "off").lower()
    if mode in ("off", "test", "active"):
        out["mode"] = mode
    if cfg.get("kasa_device_host"):
        out["kasa_device_host"] = str(cfg["kasa_device_host"])[:128]
    try:
        v = int(cfg.get("target_sunrise_soc_pct") or 25)
        if 5 <= v <= 95:
            out["target_sunrise_soc_pct"] = v
    except (TypeError, ValueError):
        pass
    try:
        v = int(cfg.get("max_charge_w") or 800)
        if 50 <= v <= 5000:
            out["max_charge_w"] = v
    except (TypeError, ValueError):
        pass
    try:
        v = int(cfg.get("max_on_duration_minutes") or 480)
        if 30 <= v <= 1440:
            out["max_on_duration_minutes"] = v
    except (TypeError, ValueError):
        pass
    out["claude_enabled"] = bool(cfg.get("claude_enabled"))
    return out


def _load_raw() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("smart_charge config unreadable (%s); using defaults", e)
        return {}


def _is_per_device_shape(data: dict[str, Any]) -> bool:
    """Per-device file format: {"by_device": {"<sn>": {...}, ...}}.
    Legacy: a single config dict at the top level."""
    return isinstance(data, dict) and isinstance(data.get("by_device"), dict)


def has_user_set_field(device_sn: str | None, field: str) -> bool:
    """Return True iff `field` has been explicitly persisted on disk
    for `device_sn` (vs the in-memory default merging through). The
    device_params resolver uses this to know when the user has
    expressed a preference via the Smart-charge form, so an auto-fit
    doesn't silently override it.

    `get_config()` always returns DEFAULT_CONFIG-merged values so it
    can't be used to distinguish — we have to inspect _load_raw."""
    if not device_sn or not field:
        return False
    with _config_lock:
        data = _load_raw()
    if _is_per_device_shape(data):
        per = data.get("by_device", {}).get(device_sn) or {}
        return field in per
    # Legacy single-config — treat as the active device's, if any field
    # is present we say yes.
    return field in data


def get_config(device_sn: str | None = None) -> dict[str, Any]:
    """Read this device's config. With no device_sn, returns the legacy
    single-config shape — for back-compat with callers that haven't been
    updated. New callers should always pass device_sn."""
    with _config_lock:
        data = _load_raw()
    if _is_per_device_shape(data):
        if device_sn:
            return _validate_config(data["by_device"].get(device_sn) or {})
        # No device_sn but per-device format on disk → return defaults.
        return dict(DEFAULT_CONFIG)
    # Legacy single-config file. If a device_sn was given, treat the
    # legacy config as that device's config (one-time migration on save).
    return _validate_config(data)


def set_config(cfg: dict[str, Any], device_sn: str | None = None) -> dict[str, Any]:
    """Validate + persist + return the saved config for a device. With
    no device_sn, writes in legacy single-config shape (back-compat)."""
    validated = _validate_config(cfg)
    with _config_lock:
        existing = _load_raw()
        if device_sn:
            # Migrate legacy single-config to per-device on first write
            # by stuffing the old config into the by_device map under
            # the legacy "default" key, so the previous behavior survives.
            if not _is_per_device_shape(existing):
                migrated = {"by_device": {}}
                if existing and "mode" in existing:
                    migrated["by_device"]["__legacy__"] = existing
                existing = migrated
            existing["by_device"][device_sn] = validated
            payload = existing
        else:
            payload = validated
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    log.info("smart_charge config saved: device=%s mode=%s target=%s",
             device_sn or "(legacy)",
             validated["mode"], validated["target_sunrise_soc_pct"])
    return validated


def get_all_configs() -> dict[str, dict[str, Any]]:
    """Map of device_sn → config for every device that has one. Used
    by the periodic loop so it can iterate all enabled devices."""
    with _config_lock:
        data = _load_raw()
    if _is_per_device_shape(data):
        return {
            sn: _validate_config(cfg)
            for sn, cfg in data["by_device"].items()
            if sn != "__legacy__"
        }
    # Legacy single-config: no device association — return empty so
    # the loop doesn't double-evaluate against the active device.
    return {}


# ---------- Decision plan ----------
@dataclass
class Plan:
    """A snapshot of what the controller has decided to do RIGHT NOW.

    The server caller turns `action` into a Kasa toggle in active mode or
    just logs it in test mode.

    `predicted_sunrise_soc_pct` is the *display* prediction — the
    smart-charge floor IS injected, so it shows what the user can
    expect AT sunrise assuming the controller runs as planned.
    `baseline_predicted_sunrise_soc_pct` is the *counterfactual* —
    what SOC would be at sunrise if the AC stayed OFF from now on.
    The controller bases its deficit + needs-charge decision on the
    counterfactual; the display value is for the UI/log.

    `planned_hours` is the set of hour-aligned unix timestamps where
    the plug is scheduled to be ON for this dusk-dawn cycle. May be
    discontinuous — the plug toggles between segments. Empty for
    skip/off-at-source decisions.

    `extension_active` is true when the plug is on past the planned
    window because SOC was still under target at margin time
    (the post-sunrise lock-in).
    """
    action: str                                # "on" | "off" | "skip"
    reason: str
    mode: str                                  # off | test | active
    decided_at: int                            # unix ts
    current_soc_pct: float | None = None
    predicted_sunrise_soc_pct: float | None = None
    baseline_predicted_sunrise_soc_pct: float | None = None
    target_sunrise_soc_pct: float = 25.0
    deficit_kwh: float = 0.0
    window_start: int | None = None
    window_end: int | None = None
    sunrise_ts: int | None = None
    cheapest_rate: float | None = None         # $/kWh in the chosen window
    planned_hours: list[int] | None = None
    extension_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _find_next_sunrise(forecast_hours: list[dict[str, Any]]) -> int | None:
    """Return the timestamp of the first hour with solar_w > 0 that comes
    after at least one dark hour. None if no upcoming sunrise found in the
    forecast window."""
    if not forecast_hours:
        return None
    seen_dark = (forecast_hours[0].get("solar_w") or 0) <= 0
    for h in forecast_hours:
        sun = (h.get("solar_w") or 0) > 0
        if seen_dark and sun:
            return int(h["ts"])
        if not sun:
            seen_dark = True
    return None


def _predicted_sunrise_soc(forecast_hours: list[dict[str, Any]],
                           sunrise_ts: int) -> float | None:
    """Return predicted_soc at the last dark hour before sunrise — the
    trough we're trying to keep above target. Returns None if the
    forecast doesn't span far enough to find it."""
    for i, h in enumerate(forecast_hours):
        if int(h.get("ts") or 0) == sunrise_ts and i > 0:
            return float(forecast_hours[i - 1].get("predicted_soc") or 0)
    return None


def _pick_planned_hours(candidates: list[tuple[int, float]],
                        needed_hours: int,
                        mandatory_hour: int | None) -> list[int]:
    """Choose which hour-aligned timestamps to be ON.

    `candidates` is [(hour_start_ts, rate_per_kwh), ...] for the eligible
    hours [now_floor, sunrise - margin). `needed_hours` is how many
    hours of charging the deficit math says we need. `mandatory_hour`
    is the hour-start that MUST be included (the anchor adjacent to
    the sunrise margin) — None when the available window is empty.

    Picks `needed_hours` cheapest, with `mandatory_hour` always included
    if present in candidates. If `needed_hours` ≥ len(candidates), uses
    them all. Returns hour-starts sorted ascending.
    """
    if not candidates or needed_hours <= 0:
        return []
    if needed_hours >= len(candidates):
        return sorted(c[0] for c in candidates)

    if mandatory_hour is not None and any(c[0] == mandatory_hour for c in candidates):
        rest = [c for c in candidates if c[0] != mandatory_hour]
        # Cheapest among the rest, tie-break by latest so we hug the
        # deadline; we already have the mandatory anchor included.
        rest_sorted = sorted(rest, key=lambda c: (c[1], -c[0]))
        chosen_rest = [c[0] for c in rest_sorted[:needed_hours - 1]]
        return sorted([mandatory_hour, *chosen_rest])

    # No mandatory anchor in this set (e.g. now is past the anchor) —
    # fall back to plain cheapest-N, latest-tie-break.
    sorted_by_cost = sorted(candidates, key=lambda c: (c[1], -c[0]))
    return sorted(c[0] for c in sorted_by_cost[:needed_hours])


def compute_plan(
    *,
    config: dict[str, Any],
    current_soc_pct: float | None,
    forecast: dict[str, Any],
    cost_plan: dict[str, Any],
    capacity_wh: int,
    baseline_forecast: dict[str, Any] | None = None,
    tz_offset_seconds: int = 0,
    now_ts: float | None = None,
    forecast_unavailable_reason: str | None = None,
) -> Plan:
    """Pure function: given inputs, decide what to do. No side effects.

    `forecast` is the with-AC-floor forecast — what the user will see
    on the dashboard, since the floor models the controller's own
    intervention. The displayed `predicted_sunrise_soc_pct` comes from
    here.

    `baseline_forecast` is the *counterfactual* (no-AC) forecast — what
    SOC would do if the plug stayed off from now on. The deficit and
    "do we need to charge" decision come from this. Defaults to
    `forecast` for back-compat with callers that haven't been updated;
    those callers will compute zero deficit when the floor is at target
    (the floor masks the underlying need).

    `forecast_unavailable_reason` lets callers explain *why* they
    couldn't supply a forecast — e.g. "calibrating: 8 of 24h captured" —
    so the decision row carries useful context instead of a generic
    error. Falls back to a "set location first" hint when not provided.
    """
    now = int(now_ts or time.time())
    mode = str(config.get("mode") or "off")
    target = float(config.get("target_sunrise_soc_pct") or 25)

    # Mode=off short-circuits but still returns a Plan so the UI can render
    # status.
    if mode == "off":
        return Plan(action="skip", reason="smart-charge disabled (mode=off)",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target)

    fc = forecast.get("forecast") or []
    if not fc:
        reason = forecast_unavailable_reason or "no forecast yet — set location first"
        return Plan(action="off", reason=reason,
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target)

    sunrise_ts = _find_next_sunrise(fc)
    if sunrise_ts is None:
        return Plan(action="off", reason="no upcoming sunrise in forecast",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target)

    # Display prediction (from the with-floor forecast — this is what
    # the dashboard shows the user).
    predicted = _predicted_sunrise_soc(fc, sunrise_ts)

    # Counterfactual: what would SOC be if AC stayed OFF the whole night?
    # If the caller didn't pass a separate baseline, the with-floor
    # forecast is the only thing we have; use it (back-compat). New
    # callers always pass an explicit baseline so the deficit math
    # isn't masked by the floor.
    bfc = (baseline_forecast or forecast).get("forecast") or []
    baseline_predicted = _predicted_sunrise_soc(bfc, sunrise_ts)
    if baseline_predicted is None:
        baseline_predicted = predicted

    if predicted is None:
        return Plan(action="off", reason="predicted sunrise SOC unavailable",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target,
                    sunrise_ts=sunrise_ts)

    # Counterfactual: solar/coast bridges target without our help → off.
    # (Q4: when this fires mid-session, current SOC has risen enough
    # that the rest of the night is fine; release lock-in.)
    if baseline_predicted is not None and baseline_predicted >= target:
        return Plan(
            action="off",
            reason=(f"baseline sunrise SOC {baseline_predicted:.0f}% ≥ target "
                    f"{target:.0f}%; AC not needed"),
            mode=mode, decided_at=now,
            current_soc_pct=current_soc_pct,
            predicted_sunrise_soc_pct=predicted,
            baseline_predicted_sunrise_soc_pct=baseline_predicted,
            target_sunrise_soc_pct=target,
            sunrise_ts=sunrise_ts,
        )

    # Deficit math — based on counterfactual (the floor in `forecast`
    # would otherwise mask the need).
    deficit_pct = target - baseline_predicted
    deficit_kwh = round((deficit_pct / 100.0) * capacity_wh / 1000.0, 3)
    max_charge_w = float(config.get("max_charge_w") or 800)
    import math
    needed_hours = max(1, math.ceil(deficit_kwh * 1000.0 / max_charge_w))

    # Build the planned-hours set. Window: [now_floor, sunrise - margin).
    # The hour ENDING at (sunrise - margin) — i.e., starting at
    # (sunrise - margin - 1h) — is mandatory: it anchors the schedule
    # against the deadline so a forecast wobble can't drop the last
    # hour of charging.
    from cost import rate_at
    margin_end_ts = sunrise_ts - SUNRISE_MARGIN_S
    mandatory_hour = margin_end_ts - 3600 if margin_end_ts > now else None
    candidates: list[tuple[int, float]] = []
    cur = (now // 3600) * 3600
    while cur < margin_end_ts:
        candidates.append((cur, rate_at(cost_plan, cur, tz_offset_seconds)))
        cur += 3600

    soc_now = float(current_soc_pct) if current_soc_pct is not None else 0.0
    extension_active = now >= margin_end_ts and soc_now < target

    if not candidates and not extension_active:
        return Plan(action="off", reason="not enough time before sunrise",
                    mode=mode, decided_at=now,
                    current_soc_pct=soc_now,
                    predicted_sunrise_soc_pct=predicted,
                    baseline_predicted_sunrise_soc_pct=baseline_predicted,
                    target_sunrise_soc_pct=target,
                    deficit_kwh=deficit_kwh,
                    sunrise_ts=sunrise_ts,
                    planned_hours=[])

    planned_hours = _pick_planned_hours(candidates, needed_hours, mandatory_hour)
    cheapest_rate = (max(rate_at(cost_plan, h, tz_offset_seconds)
                         for h in planned_hours)
                     if planned_hours else None)
    window_start = planned_hours[0] if planned_hours else None
    window_end = (planned_hours[-1] + 3600) if planned_hours else None

    now_hour = (now // 3600) * 3600
    in_planned = now_hour in planned_hours

    # Already at or above target. Don't charge UNLESS we're in a
    # planned hour for this cycle and baseline says we'll drift below
    # — overshooting now is fine (Q5: "we can overshoot, but not
    # undershoot"). The check is simpler than it sounds: if soc ≥
    # target right now AND we're not in a planned hour, off.
    #
    # Reason text distinguishes three flavors of "off":
    #   • coasting — no deficit, nothing scheduled (dead branch by
    #     here since baseline_predicted ≥ target returns earlier at
    #     line 418, kept as a defensive default).
    #   • deferred to cheaper window — current hour costs strictly
    #     more than the planned hour. Charging now would be wasteful.
    #   • deferred to deadline anchor — rates are tied (e.g., flat
    #     plan). The controller picked the latest eligible hour to
    #     "hug the deadline" and minimize drain between charge end
    #     and sunrise. Saying "cheap hour" here was misleading —
    #     no cheaper hour exists, just a deferral to reduce redrain.
    if soc_now >= target and not in_planned and not extension_active:
        if planned_hours:
            current_rate = rate_at(cost_plan, now_hour, tz_offset_seconds)
            from datetime import datetime, timedelta, timezone
            tz = timezone(timedelta(seconds=tz_offset_seconds or 0))
            hh_mm = datetime.fromtimestamp(planned_hours[0],
                                            tz=tz).strftime("%H:%M")
            saving_money = (
                current_rate is not None and cheapest_rate is not None
                and current_rate > cheapest_rate + 1e-6
            )
            if saving_money:
                why = f"cheaper window @ ${cheapest_rate:.2f}/kWh"
            else:
                why = "near sunrise to minimize drain"
            reason = (f"deferred: SOC {soc_now:.0f}% ≥ target now, "
                      f"sunrise {baseline_predicted:.0f}% < target "
                      f"{target:.0f}% — charging at {hh_mm} ({why})")
        else:
            reason = f"SOC {soc_now:.0f}% ≥ target {target:.0f}%; coasting"
        return Plan(
            action="off",
            reason=reason,
            mode=mode, decided_at=now,
            current_soc_pct=soc_now,
            predicted_sunrise_soc_pct=predicted,
            baseline_predicted_sunrise_soc_pct=baseline_predicted,
            target_sunrise_soc_pct=target,
            deficit_kwh=deficit_kwh,
            window_start=window_start, window_end=window_end,
            sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
            planned_hours=planned_hours,
        )

    if extension_active:
        return Plan(
            action="on",
            reason=(f"extension: past margin (sunrise-{SUNRISE_MARGIN_S // 3600}h) "
                    f"and SOC {soc_now:.0f}% < target {target:.0f}% — charging "
                    f"until target hit"),
            mode=mode, decided_at=now,
            current_soc_pct=soc_now,
            predicted_sunrise_soc_pct=predicted,
            baseline_predicted_sunrise_soc_pct=baseline_predicted,
            target_sunrise_soc_pct=target,
            deficit_kwh=deficit_kwh,
            window_start=window_start, window_end=window_end,
            sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
            planned_hours=planned_hours,
            extension_active=True,
        )

    if in_planned:
        return Plan(
            action="on",
            reason=(f"charging now: deficit {deficit_pct:.0f}pp ({deficit_kwh:.1f} kWh), "
                    f"in cheap window @ ${cheapest_rate:.2f}/kWh"),
            mode=mode, decided_at=now,
            current_soc_pct=soc_now,
            predicted_sunrise_soc_pct=predicted,
            baseline_predicted_sunrise_soc_pct=baseline_predicted,
            target_sunrise_soc_pct=target,
            deficit_kwh=deficit_kwh,
            window_start=window_start, window_end=window_end,
            sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
            planned_hours=planned_hours,
        )

    # Between planned segments — wait.
    return Plan(
        action="off",
        reason=(f"waiting for next planned hour (deficit {deficit_pct:.0f}pp, "
                f"{len(planned_hours)} planned hour(s))"),
        mode=mode, decided_at=now,
        current_soc_pct=soc_now,
        predicted_sunrise_soc_pct=predicted,
        baseline_predicted_sunrise_soc_pct=baseline_predicted,
        target_sunrise_soc_pct=target,
        deficit_kwh=deficit_kwh,
        window_start=window_start, window_end=window_end,
        sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
        planned_hours=planned_hours,
    )


