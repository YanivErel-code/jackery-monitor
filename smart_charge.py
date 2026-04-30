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
logic is:
  1. Read predicted SOC at the next sunrise from /api/forecast (already
     computed by the existing forecaster).
  2. If predicted_sunrise_soc >= target_sunrise_soc_pct → action OFF
     (solar will bridge it; no grid needed).
  3. Else compute the energy deficit:
        deficit_kwh = (target - predicted) * capacity_wh / 100 / 1000
  4. Pick the lowest-cost TOU hours between now and sunrise that total at
     least the needed charging time at max_charge_w. Those hours are the
     "charging window."
  5. While we're inside the window AND current_soc < target → ON.
     Otherwise → OFF. Stop early if SOC reaches target.

Safety:
  - max_on_duration_minutes caps total ON time per dusk-to-dawn cycle so a
    forecast bug or outage can't keep the plug on indefinitely.
  - Test mode lets the user observe a few days of decisions without any
    plug movement.
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
    # is on. 5000 Plus default "Fast" mode is ~1500W; Standard is ~600W;
    # Super-fast on 240V split-phase up to ~2400W. Pick what the unit
    # is actually set to in its app/UI.
    "max_charge_w": 1500,
    "max_on_duration_minutes": 480,      # safety cap per dusk-dawn cycle
    "claude_enabled": False,             # optional decision narrator
}

# Decisions are persisted to energy_db.smart_charge_decisions by the server,
# not held in-memory here. That way they survive container restarts and can
# be joined to the actual SOC samples for predicted-vs-actual analytics.

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
    """
    action: str                                # "on" | "off" | "skip"
    reason: str
    mode: str                                  # off | test | active
    decided_at: int                            # unix ts
    current_soc_pct: float | None = None
    predicted_sunrise_soc_pct: float | None = None
    target_sunrise_soc_pct: float = 25.0
    deficit_kwh: float = 0.0
    window_start: int | None = None
    window_end: int | None = None
    sunrise_ts: int | None = None
    cheapest_rate: float | None = None         # $/kWh in the chosen window

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


def compute_plan(
    *,
    config: dict[str, Any],
    current_soc_pct: float | None,
    forecast: dict[str, Any],
    cost_plan: dict[str, Any],
    capacity_wh: int,
    tz_offset_seconds: int = 0,
    now_ts: float | None = None,
) -> Plan:
    """Pure function: given inputs, decide what to do. No side effects."""
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
        return Plan(action="off", reason="no forecast yet — set location first",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target)

    sunrise_ts = _find_next_sunrise(fc)
    if sunrise_ts is None:
        return Plan(action="off", reason="no upcoming sunrise in forecast",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target)

    # Predicted SOC at the last dark hour before sunrise (the trough).
    predicted = None
    for i, h in enumerate(fc):
        if int(h.get("ts") or 0) == sunrise_ts and i > 0:
            predicted = float(fc[i - 1].get("predicted_soc") or 0)
            break

    if predicted is None:
        return Plan(action="off", reason="predicted sunrise SOC unavailable",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    target_sunrise_soc_pct=target,
                    sunrise_ts=sunrise_ts)

    # Solar will bridge it on its own → nothing to do.
    if predicted >= target:
        return Plan(
            action="off",
            reason=f"predicted sunrise SOC {predicted:.0f}% ≥ target {target:.0f}%; no grid needed",
            mode=mode, decided_at=now,
            current_soc_pct=current_soc_pct,
            predicted_sunrise_soc_pct=predicted,
            target_sunrise_soc_pct=target,
            sunrise_ts=sunrise_ts,
        )

    # Deficit math
    deficit_pct = target - predicted
    deficit_kwh = round((deficit_pct / 100.0) * capacity_wh / 1000.0, 3)
    max_charge_w = float(config.get("max_charge_w") or 800)
    import math
    needed_hours = max(1, math.ceil(deficit_kwh * 1000.0 / max_charge_w))

    # Pick the cheapest TOU hours between now and sunrise that total
    # `needed_hours`. Then pack them into the latest contiguous window
    # ending just before sunrise — matches user intuition ("top off
    # right before sunrise"). For a TOU rate where evening = cheaper,
    # this puts charging right where it should be.
    from cost import rate_at
    candidates: list[tuple[int, float]] = []
    cur = (now // 3600) * 3600
    while cur < sunrise_ts:
        candidates.append((cur, rate_at(cost_plan, cur, tz_offset_seconds)))
        cur += 3600
    if not candidates:
        return Plan(action="off", reason="not enough time before sunrise",
                    mode=mode, decided_at=now,
                    current_soc_pct=current_soc_pct,
                    predicted_sunrise_soc_pct=predicted,
                    target_sunrise_soc_pct=target,
                    deficit_kwh=deficit_kwh,
                    sunrise_ts=sunrise_ts)

    # Lowest-cost hours; tie-break by latest (so we charge close to sunrise).
    sorted_by_cost = sorted(candidates, key=lambda c: (c[1], -c[0]))
    chosen = sorted(sorted_by_cost[:needed_hours], key=lambda c: c[0])
    window_start = chosen[0][0]
    window_end = chosen[-1][0] + 3600
    cheapest_rate = max(rate for _, rate in chosen)

    in_window = window_start <= now < window_end
    soc_now = float(current_soc_pct) if current_soc_pct is not None else 0.0

    if soc_now >= target:
        return Plan(
            action="off",
            reason=f"target {target:.0f}% already reached (SOC {soc_now:.0f}%)",
            mode=mode, decided_at=now,
            current_soc_pct=soc_now,
            predicted_sunrise_soc_pct=predicted,
            target_sunrise_soc_pct=target,
            deficit_kwh=deficit_kwh,
            window_start=window_start, window_end=window_end,
            sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
        )

    if in_window:
        return Plan(
            action="on",
            reason=(f"charging now: deficit {deficit_pct:.0f}pp ({deficit_kwh:.1f} kWh), "
                    f"in cheap window @ ${cheapest_rate:.2f}/kWh"),
            mode=mode, decided_at=now,
            current_soc_pct=soc_now,
            predicted_sunrise_soc_pct=predicted,
            target_sunrise_soc_pct=target,
            deficit_kwh=deficit_kwh,
            window_start=window_start, window_end=window_end,
            sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
        )

    # Outside the window for now — wait.
    return Plan(
        action="off",
        reason=(f"waiting for cheap window (starts {window_start}, "
                f"deficit {deficit_pct:.0f}pp)"),
        mode=mode, decided_at=now,
        current_soc_pct=soc_now,
        predicted_sunrise_soc_pct=predicted,
        target_sunrise_soc_pct=target,
        deficit_kwh=deficit_kwh,
        window_start=window_start, window_end=window_end,
        sunrise_ts=sunrise_ts, cheapest_rate=cheapest_rate,
    )


