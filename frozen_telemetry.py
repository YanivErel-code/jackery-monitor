"""Detect energy-inconsistent, unchanged measurements independently of receipts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypedDict

MIN_UNCHANGED_S = 1800
MAX_OBSERVATION_GAP_S = 180
MIN_DISCHARGE_FRACTION = 0.02
RECOVERY_COOLDOWN_S = 1800
MAX_RECOVERY_COOLDOWN_S = 14400


class MeasurementHealth(TypedDict):
    suspect: bool
    unchanged_s: float
    expected_discharge_wh: float
    recovery_attempts: int


@dataclass
class Observation:
    signature: tuple[float | str, ...]
    capacity_wh: float
    unchanged_since: float
    last_seen: float
    discharge_w: float
    discharge_wh: float = 0
    recovery_attempts: int = 0
    last_recovery_at: float = 0
    suspect: bool = False


class FrozenTelemetryDetector:
    def __init__(self) -> None:
        self._observations: dict[str, Observation] = {}

    def forget(self, device_sn: str | None = None) -> None:
        if device_sn is None:
            self._observations.clear()
        else:
            self._observations.pop(device_sn, None)

    def interrupt(self, device_sn: str) -> None:
        observation = self._observations.get(device_sn)
        if observation is not None and not observation.suspect:
            self.forget(device_sn)

    def observe(self, device_sn: str, *, signature: tuple[float | str, ...],
                discharge_w: float, capacity_wh: float, now: float) -> None:
        numbers = (discharge_w, capacity_wh, now)
        valid_numbers = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) for value in numbers
        )
        valid_signature = bool(signature) and all(
            (isinstance(value, str) and bool(value))
            or (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value)) for value in signature
        )
        if not valid_numbers or not valid_signature or capacity_wh <= 0:
            self.interrupt(device_sn)
            return
        previous = self._observations.get(device_sn)
        if discharge_w <= 0:
            if previous is not None and previous.signature != signature:
                self.forget(device_sn)
            else:
                self.interrupt(device_sn)
            return
        if (previous is None or previous.signature != signature
                or previous.capacity_wh != capacity_wh):
            self._observations[device_sn] = Observation(
                signature, capacity_wh, now, now, discharge_w)
            return
        if not 0 <= now - previous.last_seen <= MAX_OBSERVATION_GAP_S:
            if not previous.suspect:
                self._observations[device_sn] = Observation(
                    signature, capacity_wh, now, now, discharge_w)
            else:
                previous.last_seen = now
                previous.discharge_w = discharge_w
            return
        previous.discharge_wh += (
            min(previous.discharge_w, discharge_w) * (now - previous.last_seen) / 3600)
        previous.last_seen = now
        previous.discharge_w = discharge_w
        if (now - previous.unchanged_since >= MIN_UNCHANGED_S
                and previous.discharge_wh >= previous.capacity_wh * MIN_DISCHARGE_FRACTION):
            previous.suspect = True

    def health(self, device_sn: str, now: float) -> MeasurementHealth:
        observation = self._observations.get(device_sn)
        if observation is None:
            return {"suspect": False, "unchanged_s": 0,
                    "expected_discharge_wh": 0, "recovery_attempts": 0}
        unchanged = max(0.0, now - observation.unchanged_since)
        return {
            "suspect": observation.suspect,
            "unchanged_s": round(unchanged, 1),
            "expected_discharge_wh": round(observation.discharge_wh, 1),
            "recovery_attempts": observation.recovery_attempts,
        }

    def recovery_due(self, now: float) -> list[str]:
        due = []
        for device_sn, observation in self._observations.items():
            cooldown = min(
                RECOVERY_COOLDOWN_S * 2 ** min(max(0, observation.recovery_attempts - 1), 3),
                MAX_RECOVERY_COOLDOWN_S,
            )
            if (self.health(device_sn, now)["suspect"]
                    and (not observation.recovery_attempts
                         or now - observation.last_recovery_at >= cooldown)):
                due.append(device_sn)
        return due

    def mark_recovery(self, device_sns: list[str], now: float) -> None:
        for device_sn in device_sns:
            observation = self._observations.get(device_sn)
            if observation is not None:
                observation.recovery_attempts += 1
                observation.last_recovery_at = now
