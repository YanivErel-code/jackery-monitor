"""
Battery-state-of-charge automation engine.

A "rule" is: when SOC <op> threshold, turn a Kasa device on or off. Rules
are edge-triggered — the action fires once when the condition transitions
from false to true, NOT on every poll. So a "<20% turn off heater" rule
fires the moment SOC drops below 20%, and won't fire again until SOC has
gone back above 20% and dropped below it again.

Rules persist to /data/automation.json; the schema is the same dict shape
the dashboard uses, so the UI form and the JSON file are 1:1.

Rule schema:
{
  "id":            "8-char hex",
  "name":          "Turn off heater when low",
  "enabled":       true,
  "trigger":       "battery_percent",
  "operator":      "<" | "<=" | "=" | ">=" | ">",
  "value":         20,
  "action":        "off" | "on",
  "kasa_host":     "192.168.1.50",
  "kasa_alias":    "Heater",
  "jackery_device_sn": "ABC123",   # which Jackery device to watch; null = any
  "jackery_device_name": "Explorer 5000 Plus",  # for display only
  "last_fired":    <unix-ts | null>,
  "last_state":    <bool | null>,
  "last_error":    <str | null>,
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import kasa_client
from errors import ConfigError

log = logging.getLogger("automation")

RULES_PATH = os.environ.get("JACKERY_RULES_FILE", "/data/automation.json")
EQUALS_TOLERANCE = 0.5   # SOC is noisy; "= 50" matches 49.5..50.5 to avoid flap


VALID_OPERATORS = ("<", "<=", "=", ">=", ">")
VALID_ACTIONS = ("on", "off")
VALID_TRIGGERS = ("battery_percent",)


class AutomationError(ConfigError, ValueError):
    """Invalid automation rule (bad operator, missing host, etc.).
    Multiple inheritance preserves `except ValueError` callers."""
    pass


def _matches(rule: dict, soc: float) -> bool:
    op = rule.get("operator")
    try:
        threshold = float(rule.get("value"))
    except (TypeError, ValueError):
        return False
    if op == "<":
        return soc < threshold
    if op == "<=":
        return soc <= threshold
    if op == "=":
        return abs(soc - threshold) <= EQUALS_TOLERANCE
    if op == ">=":
        return soc >= threshold
    if op == ">":
        return soc > threshold
    return False


def _validate(rule: dict) -> dict:
    """Normalise + reject obviously bad rules. Returns a clean rule dict."""
    name = (rule.get("name") or "").strip() or "Unnamed rule"
    trigger = rule.get("trigger") or "battery_percent"
    if trigger not in VALID_TRIGGERS:
        raise AutomationError(f"trigger must be one of {VALID_TRIGGERS}")
    op = rule.get("operator")
    if op not in VALID_OPERATORS:
        raise AutomationError(f"operator must be one of {VALID_OPERATORS}")
    try:
        value = float(rule.get("value"))
    except (TypeError, ValueError):
        raise AutomationError("value must be a number")
    action = rule.get("action")
    if action not in VALID_ACTIONS:
        raise AutomationError(f"action must be one of {VALID_ACTIONS}")
    host = (rule.get("kasa_host") or "").strip()
    if not host:
        raise AutomationError("kasa_host is required")
    return {
        "id":         (rule.get("id") or uuid.uuid4().hex[:8]),
        "name":       name,
        "enabled":    bool(rule.get("enabled", True)),
        "trigger":    trigger,
        "operator":   op,
        "value":      value,
        "action":     action,
        "kasa_host":  host,
        "kasa_alias": (rule.get("kasa_alias") or "").strip() or host,
        # null means "any/active device" — preserves behavior of pre-multi-
        # device rules. New rules from the UI always set a specific sn.
        "jackery_device_sn":   (rule.get("jackery_device_sn") or None),
        "jackery_device_name": (rule.get("jackery_device_name") or "").strip() or None,
        "last_fired": rule.get("last_fired"),
        "last_state": rule.get("last_state"),
        "last_error": rule.get("last_error"),
    }


class AutomationEngine:
    """Stateful rule store + edge-triggered evaluator. One instance per server."""

    def __init__(self, firing_recorder=None) -> None:
        """`firing_recorder` is an optional callable invoked on every
        successful firing — server.py wires it to
        `EnergyDB.record_automation_fire` so each fire lands in a
        persistent audit table. Kept as a callback (rather than a
        direct DB import) to avoid a circular dependency and to keep
        unit tests free of DB setup."""
        self.rules: list[dict] = []
        self._lock = asyncio.Lock()
        self._firing_recorder = firing_recorder
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            with open(RULES_PATH) as f:
                data = json.load(f)
            self.rules = list(data.get("rules") or [])
        except FileNotFoundError:
            self.rules = []
        except Exception as e:
            log.warning("rules file %s unreadable: %s; starting empty", RULES_PATH, e)
            self.rules = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(RULES_PATH) or ".", exist_ok=True)
            tmp = RULES_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"rules": self.rules}, f, indent=2)
            os.replace(tmp, RULES_PATH)
        except Exception as e:
            log.error("failed to save rules: %s", e)

    # ---- CRUD ----
    def list_rules(self) -> list[dict]:
        return list(self.rules)

    def upsert(self, rule: dict) -> dict:
        clean = _validate(rule)
        for i, r in enumerate(self.rules):
            if r["id"] == clean["id"]:
                # Preserve runtime state on edit unless caller passed it explicitly.
                clean["last_fired"] = clean.get("last_fired") or r.get("last_fired")
                clean["last_state"] = clean.get("last_state")
                self.rules[i] = clean
                self._save()
                return clean
        self.rules.append(clean)
        self._save()
        return clean

    def delete(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        changed = len(self.rules) != before
        if changed:
            self._save()
        return changed

    # ---- evaluation ----
    async def evaluate(self, soc_by_sn: dict, active_sn: str | None = None) -> list[dict]:
        """Walk all enabled rules. `soc_by_sn` is a dict mapping each Jackery
           device's serial number to its current battery_percent (None for
           devices we don't have data for yet). Each rule is evaluated
           against ITS target device's SOC; rules with no target sn fall
           back to the active device (legacy behavior).

           Edge-triggered: fire (and return) the ones whose condition just
           transitioned from false to true. State is mutated in place and
           persisted on any change."""
        if not soc_by_sn:
            return []
        fired: list[dict] = []
        dirty = False
        async with self._lock:
            for rule in self.rules:
                if not rule.get("enabled", True):
                    rule["last_state"] = None  # reset edge state when disabled
                    continue
                target_sn = rule.get("jackery_device_sn") or active_sn
                soc = soc_by_sn.get(target_sn) if target_sn else None
                if soc is None:
                    # No data for this rule's target device; skip without
                    # changing edge state so we don't spuriously fire when
                    # it comes back online.
                    continue
                matches_now = _matches(rule, float(soc))
                last = rule.get("last_state")
                if matches_now and not last:
                    # Edge: transition from false -> true (or unknown -> true)
                    try:
                        await kasa_client.set_state(
                            rule["kasa_host"],
                            rule["action"] == "on",
                        )
                        rule["last_fired"] = time.time()
                        rule["last_error"] = None
                        rule["last_state"] = True   # consume the edge ONLY on success
                        fired.append(rule)
                        log.info("Automation fired: %s [%s SOC=%s] -> %s %s",
                                 rule["name"], target_sn, soc,
                                 rule["action"], rule["kasa_alias"])
                        # Persist a row to the firings audit table. Best-
                        # effort — DB hiccups shouldn't roll back the
                        # successful Kasa toggle or block subsequent rules.
                        if self._firing_recorder:
                            try:
                                self._firing_recorder(
                                    rule_id=rule["id"],
                                    rule_name=rule.get("name"),
                                    action=rule["action"],
                                    kasa_host=rule["kasa_host"],
                                    jackery_sn=target_sn,
                                    soc_at_fire=float(soc),
                                    operator=rule.get("operator"),
                                    threshold=float(rule.get("value") or 0),
                                )
                            except Exception as e:
                                log.warning(
                                    "Automation firing audit-log write failed for %s: %s",
                                    rule["name"], e,
                                )
                    except Exception as e:
                        rule["last_error"] = str(e)
                        # Leave last_state unchanged so we retry on the next
                        # poll. Avoids the "stuck failed rule" footgun where
                        # one transient error means the rule never fires
                        # again until the battery exits and re-enters range.
                        log.warning("Automation %s failed (will retry): %s",
                                    rule["name"], e)
                    dirty = True
                else:
                    if last != matches_now:
                        dirty = True  # state changed but didn't fire
                    rule["last_state"] = matches_now
            if dirty:
                self._save()
        return fired
