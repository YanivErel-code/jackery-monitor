"""Alerts: decide when something needs the owner's attention and send it
to their phone.

Three parts, kept apart so each can be tested alone:

* `conditions()` turns a plain dict of facts (battery level, headline,
  connection, ...) into the alerts that are true right now. Pure.
* `step()` compares that with what was true last time and returns only
  the alerts to send: one when a condition starts (after its delay, if it
  has one), none while it stays true, and it re-arms once the condition
  clears. Pure; the caller persists the memory.
* `send_ntfy()` delivers one alert through an ntfy topic.

Config lives in /data/alerts.json; the memory and the recent-alert log in
/data/alerts-state.json so a restart does not repeat alerts already sent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

log = logging.getLogger("alerts")


def _config_file() -> str:
    return os.environ.get("JACKERY_ALERTS_FILE", "/data/alerts.json")


def _state_file() -> str:
    return os.environ.get("JACKERY_ALERTS_STATE_FILE", "/data/alerts-state.json")


RECENT_LIMIT = 50
LOW_BATTERY_CLEAR_MARGIN_PCT = 5.0
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
    priority: str = "default"     # ntfy: min | low | default | high | urgent
    tag: str = "battery"          # ntfy emoji short code
    delay_s: float = 0.0          # how long the condition must hold first
    key: str = ""                 # a new key on a live condition alerts again


def default_config() -> dict[str, Any]:
    return {"enabled": False, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
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
    for key in ("enabled", "ntfy_server", "ntfy_topic"):
        if key in stored:
            config[key] = stored[key]
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
    if "ntfy_topic" in patch:
        topic = str(patch["ntfy_topic"] or "").strip()
        if topic and not TOPIC_RE.match(topic):
            raise ValueError("The topic can only use letters, numbers, dashes and underscores.")
        config["ntfy_topic"] = topic
    if "ntfy_server" in patch:
        server = str(patch["ntfy_server"] or "").strip().rstrip("/") or "https://ntfy.sh"
        if not re.match(r"^https?://[^\s/]+(:\d+)?$", server):
            raise ValueError("The server must look like https://ntfy.sh.")
        config["ntfy_server"] = server
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
    if config["enabled"] and not config["ntfy_topic"]:
        raise ValueError("Enter an ntfy topic before turning alerts on.")
    _write(_config_file(), config)
    return config


def load_state() -> dict[str, Any]:
    state = _read(_state_file())
    return {"memory": state.get("memory") or {}, "recent": state.get("recent") or []}


def save_state(state: dict[str, Any]) -> None:
    try:
        _write(_state_file(), {"memory": state["memory"], "recent": state["recent"][:RECENT_LIMIT]})
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
                priority="high", tag="low_battery")

    if on("charge_failed") and state == "not_charging":
        found["charge_failed"] = Alert(
            "charge_failed", "Battery isn't charging", f"{title} {detail}".strip(),
            priority="high", tag="warning")

    if on("reserve_risk") and tone == "alert" and state != "not_charging":
        found["reserve_risk"] = Alert(
            "reserve_risk", "Battery heading below the reserve", f"{title} {detail}".strip(),
            priority="high", tag="warning")

    watchdog = facts.get("watchdog") or {}
    if on("inverter_trip") and (watchdog.get("hw_trip_active") or watchdog.get("error_message")):
        found["inverter_trip"] = Alert(
            "inverter_trip", "AC output tripped",
            watchdog.get("error_message")
            or "The inverter tripped on overload. The app is trying to turn AC back on.",
            priority="urgent", tag="rotating_light")

    if on("laundry_hold") and (facts.get("laundry") or {}).get("blocked"):
        minutes = float(rules["laundry_hold"]["after_min"])
        found["laundry_hold"] = Alert(
            "laundry_hold", f"Charging held for {_hours(minutes)}", f"{title} {detail}".strip(),
            tag="hourglass", delay_s=minutes * 60)

    connection = facts.get("connection")
    if on("offline") and connection is not None and connection != "connected":
        minutes = float(rules["offline"]["after_min"])
        found["offline"] = Alert(
            "offline", "Lost contact with the battery",
            f"The monitor has had no data from the battery for {_hours(minutes)}.",
            priority="high", tag="electric_plug", delay_s=minutes * 60)

    backup = facts.get("backup_last_run") or {}
    if on("backup_failed") and backup and not backup.get("ok"):
        found["backup_failed"] = Alert(
            "backup_failed", "Backup failed",
            str(backup.get("error") or "The last backup did not complete."),
            tag="floppy_disk", key=str(backup.get("ts") or ""))

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


def record(state: dict[str, Any], alert: Alert, now: float, error: str | None) -> None:
    state["recent"].insert(0, {**asdict(alert), "ts": now, "delivered": error is None, "error": error})
    del state["recent"][RECENT_LIMIT:]


async def send_ntfy(config: dict[str, Any], alert: Alert, *,
                    client: httpx.AsyncClient | None = None) -> str | None:
    """Post one alert to the configured ntfy topic. Returns None on
    success or a short reason on failure; never raises."""
    topic = config.get("ntfy_topic")
    if not topic:
        return "No ntfy topic is set."
    body = {"topic": topic, "title": alert.title, "message": alert.message,
            "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(alert.priority, 3),
            "tags": [alert.tag]}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await client.post(config.get("ntfy_server") or "https://ntfy.sh", json=body)
        if response.status_code >= 300:
            return f"ntfy answered {response.status_code}."
        return None
    except httpx.HTTPError as error:
        return f"Could not reach ntfy: {type(error).__name__}."
    finally:
        if owned:
            await client.aclose()
