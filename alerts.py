"""Alerts: decide when something needs the owner's attention, and what the
lock-screen Live Activity should show. Delivery is apns.py's job.

* `conditions()` turns a plain dict of facts (battery level, headline,
  connection, ...) into the alerts that are true right now. Pure.
* `step()` compares that with what was true last time and returns only
  the alerts to send: one when a condition starts (after its delay, if it
  has one), none while it stays true, and it re-arms once the condition
  clears. Pure; the caller persists the memory.
* `live_content()` and `live_step()` do the same for the Live Activity:
  what it shows, and whether to start, update or end it.

Config lives in /data/alerts.json; the memory and the recent-alert log in
/data/alerts-state.json so a restart does not repeat alerts already sent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger("alerts")


def _config_file() -> str:
    return os.environ.get("JACKERY_ALERTS_FILE", "/data/alerts.json")


def _state_file() -> str:
    return os.environ.get("JACKERY_ALERTS_STATE_FILE", "/data/alerts-state.json")


RECENT_LIMIT = 50
LOW_BATTERY_CLEAR_MARGIN_PCT = 5.0

# name -> (label shown in Settings, defaults). A rule's numeric option, if
# it has one, is listed with its allowed range in OPTION_RANGES.
RULES: dict[str, tuple[str, dict[str, Any]]] = {
    "low_battery": ("Battery low", {"enabled": True, "threshold_pct": 20}),
    "reserve_risk": ("Battery on course to fall below the reserve", {"enabled": True}),
    "charge_failed": ("Plug is on but the battery isn't charging", {"enabled": True}),
    "inverter_trip": ("AC output tripped on overload", {"enabled": True}),
    "laundry_hold": ("Charging held by the washer or dryer for a long time",
                     {"enabled": True, "after_min": 120}),
    "offline": ("Lost contact with the battery", {"enabled": True, "after_min": 15}),
    "backup_failed": ("Backup failed", {"enabled": True}),
}
OPTION_RANGES = {"threshold_pct": (5, 95), "after_min": (1, 1440)}


@dataclass(frozen=True)
class Alert:
    rule: str
    title: str
    message: str
    priority: str = "default"     # default | high | urgent
    delay_s: float = 0.0          # how long the condition must hold first
    key: str = ""                 # a new key on a live condition alerts again


def default_config() -> dict[str, Any]:
    return {"enabled": False, "live_activity": True,
            "rules": {name: dict(defaults) for name, (_, defaults) in RULES.items()}}


def _read(path: str) -> dict:
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def get_config() -> dict[str, Any]:
    """Stored config laid over the defaults, so a rule added in a later
    version shows up with its default."""
    stored = _read(_config_file())
    config = default_config()
    for key in ("enabled", "live_activity"):
        if key in stored:
            config[key] = bool(stored[key])
    for name, rule in (stored.get("rules") or {}).items():
        if name in config["rules"] and isinstance(rule, dict):
            config["rules"][name].update(
                {k: v for k, v in rule.items() if k in config["rules"][name]})
    return config


def set_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial config. Raises ValueError with a plain message on
    anything that would not work."""
    config = get_config()
    if "enabled" in patch:
        config["enabled"] = bool(patch["enabled"])
    if "live_activity" in patch:
        config["live_activity"] = bool(patch["live_activity"])
    for name, rule in (patch.get("rules") or {}).items():
        if name not in config["rules"] or not isinstance(rule, dict):
            raise ValueError(f"Unknown alert: {name}")
        current = config["rules"][name]
        if "enabled" in rule:
            current["enabled"] = bool(rule["enabled"])
        for option, (low, high) in OPTION_RANGES.items():
            if option in rule and option in current:
                try:
                    value = int(rule[option])
                except (TypeError, ValueError):
                    raise ValueError(f"{option} must be a whole number.") from None
                if not low <= value <= high:
                    raise ValueError(f"{option} must be between {low} and {high}.")
                current[option] = value
    _write(_config_file(), config)
    return config


def load_state() -> dict[str, Any]:
    state = _read(_state_file())
    return {"memory": state.get("memory") or {}, "recent": state.get("recent") or [],
            "live": state.get("live") or {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        _write(_state_file(), {"memory": state["memory"], "recent": state["recent"][:RECENT_LIMIT],
                              "live": state.get("live") or {}})
    except OSError as error:
        log.warning("could not save alert state: %s", error)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _hours(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)} minutes"
    hours = minutes / 60
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def conditions(facts: dict[str, Any], config: dict[str, Any],
               active: set[str] | None = None) -> dict[str, Alert]:
    """The alerts whose condition holds right now, by rule name.

    `active` names the rules that were already firing; the low-battery
    rule uses it to stay on until the level has recovered by a margin,
    so a level hovering at the threshold does not alert repeatedly."""
    active = active or set()
    rules = config["rules"]
    headline = facts.get("headline") or {}
    state, tone = headline.get("state"), headline.get("tone")
    title, detail = headline.get("title") or "", headline.get("detail") or ""
    found: dict[str, Alert] = {}

    def on(name: str) -> bool:
        return bool(rules.get(name, {}).get("enabled"))

    soc = _num(facts.get("soc_pct"))
    if on("low_battery") and soc is not None:
        threshold = float(rules["low_battery"]["threshold_pct"])
        limit = threshold + (LOW_BATTERY_CLEAR_MARGIN_PCT if "low_battery" in active else 0)
        if soc <= threshold or ("low_battery" in active and soc < limit):
            found["low_battery"] = Alert(
                "low_battery", "Battery low",
                f"The battery is at {soc:.0f}%. {title}".strip(),
                priority="high")

    if on("charge_failed") and state == "not_charging":
        found["charge_failed"] = Alert(
            "charge_failed", "Battery isn't charging", f"{title} {detail}".strip(),
            priority="high")

    if on("reserve_risk") and tone == "alert" and state != "not_charging":
        found["reserve_risk"] = Alert(
            "reserve_risk", "Battery heading below the reserve", f"{title} {detail}".strip(),
            priority="high")

    watchdog = facts.get("watchdog") or {}
    if on("inverter_trip") and (watchdog.get("hw_trip_active") or watchdog.get("error_message")):
        found["inverter_trip"] = Alert(
            "inverter_trip", "AC output tripped",
            watchdog.get("error_message")
            or "The inverter tripped on overload. The app is trying to turn AC back on.",
            priority="urgent")

    if on("laundry_hold") and (facts.get("laundry") or {}).get("blocked"):
        minutes = float(rules["laundry_hold"]["after_min"])
        found["laundry_hold"] = Alert(
            "laundry_hold", f"Charging held for {_hours(minutes)}", f"{title} {detail}".strip(), delay_s=minutes * 60)

    connection = facts.get("connection")
    if on("offline") and connection is not None and connection != "connected":
        minutes = float(rules["offline"]["after_min"])
        found["offline"] = Alert(
            "offline", "Lost contact with the battery",
            f"The monitor has had no data from the battery for {_hours(minutes)}.",
            priority="high", delay_s=minutes * 60)

    backup = facts.get("backup_last_run") or {}
    if on("backup_failed") and backup and not backup.get("ok"):
        found["backup_failed"] = Alert(
            "backup_failed", "Backup failed",
            str(backup.get("error") or "The last backup did not complete."), key=str(backup.get("ts") or ""))

    return found


def step(memory: dict[str, dict], current: dict[str, Alert], now: float) -> list[Alert]:
    """Update `memory` in place and return the alerts to send now."""
    to_send: list[Alert] = []
    for name in list(memory):
        if name not in current:
            del memory[name]            # condition cleared: re-arm
    for name, alert in current.items():
        entry = memory.setdefault(name, {"since": now, "sent_key": None})
        if entry["sent_key"] == alert.key:
            continue
        if now - entry["since"] >= alert.delay_s:
            entry["sent_key"] = alert.key
            to_send.append(alert)
    return to_send


def active_rules(memory: dict[str, dict]) -> set[str]:
    return {name for name, entry in memory.items() if entry.get("sent_key") is not None}


def undelivered(memory: dict[str, dict], alert: Alert) -> None:
    """Forget that `alert` was sent so the next pass tries it again."""
    if alert.rule in memory:
        memory[alert.rule]["sent_key"] = None


def record(state: dict[str, Any], alert: Alert, now: float, error: str | None) -> None:
    """Log one send. A failure that is still failing on a retry updates
    its existing row, so an outage does not flood the log."""
    last = next((r for r in state["recent"] if r["rule"] == alert.rule), None)
    if error and last and not last["delivered"] and last["key"] == alert.key:
        last.update(ts=now, error=error)
        return
    state["recent"].insert(0, {**asdict(alert), "ts": now, "delivered": error is None, "error": error})
    del state["recent"][RECENT_LIMIT:]


# ---- Live Activity ----------------------------------------------------

# Headline states that are worth a place on the lock screen while they last.
LIVE_STATES = {"charging", "charging_peak", "hold"}
LIVE_REFRESH_S = 10 * 60
LIVE_STATUS = {
    "charging": "Charging", "charging_peak": "Charging at peak rate", "hold": "Charging on hold",
    "not_charging": "Not charging", "scheduled": "Charge scheduled", "solar_enough": "Solar is enough",
    "manual": "Automatic charging off",
}


def live_content(summary: dict[str, Any], *, final: bool = False) -> dict[str, Any] | None:
    """What the Live Activity shows, or None when nothing is going on.
    `final` builds the closing state for an activity that is ending.

    The keys are the app's ChargingAttributes.ContentState, field for
    field: the phone silently drops a push it cannot decode."""
    headline = summary.get("headline") or {}
    state = headline.get("state")
    if state not in LIVE_STATES and not final:
        return None
    power, battery = summary.get("power") or {}, summary.get("battery") or {}
    return {
        "socPct": round(_num(battery.get("soc_pct")) or 0),
        "state": state or "",
        "status": LIVE_STATUS.get(state, "Charging finished") if state in LIVE_STATES else "Charging finished",
        "title": headline.get("title") or "",
        "tone": headline.get("tone") or "ok",
        "gridW": round(_num(power.get("grid_w")) or 0),
        "solarW": round(_num(power.get("solar_w")) or 0),
        "outputW": round(_num(power.get("output_w")) or 0),
        "untilTs": _num(summary.get("until_ts")) if state in LIVE_STATES else None,
    }


def live_step(memory: dict[str, Any], content: dict[str, Any] | None, now: float) -> str | None:
    """Update `memory` in place and say what to do: "start", "update",
    "end" or nothing. Power readings change every second, so they only
    ride along on a periodic refresh; a change of level, status or end
    time is pushed straight away."""
    if content is None:
        if memory:
            memory.clear()
            return "end"
        return None
    previous = memory.get("content")
    if previous is None:
        memory.update(content=content, pushed_ts=now, started_ts=now)
        return "start"
    changed = any(previous.get(k) != content.get(k) for k in ("socPct", "state", "title", "untilTs"))
    if changed or now - memory.get("pushed_ts", 0) >= LIVE_REFRESH_S:
        memory.update(content=content, pushed_ts=now)
        return "update"
    return None
