"""Detect energy-inconsistent, unchanged measurements independently of receipts."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

MIN_UNCHANGED_S = 1800
MAX_OBSERVATION_GAP_S = 180
MIN_DISCHARGE_FRACTION = 0.02
RECOVERY_COOLDOWN_S = 1800
MAX_RECOVERY_COOLDOWN_S = 14400
log = logging.getLogger(__name__)


class MeasurementHealth(TypedDict):
    suspect: bool
    unchanged_s: float
    expected_discharge_wh: float
    recovery_attempts: int
    unchanged_since: float
    persistence_error: NotRequired[str]


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
    def __init__(self, *, state_path: Path | None = None) -> None:
        self._observations: dict[str, Observation] = {}
        self.state_path = state_path
        self._last_saved = float("-inf")
        self.persistence_error: str | None = None
        self._load_failed = False
        self._load()

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            if self.state_path.stat().st_size > 1024 * 1024:
                raise ValueError("measurement state exceeds size limit")
            document = json.loads(self.state_path.read_text())
            if document.get("version") != 1 or len(document["observations"]) > 128:
                raise ValueError("invalid measurement state version or device count")
            if not isinstance(document.get("rebuilding", False), bool):
                raise ValueError("invalid saved measurement-state status")
            observations = {}
            for device_sn, values in document["observations"].items():
                observation = Observation(**{**values, "signature": tuple(values["signature"])})
                numbers = (observation.capacity_wh, observation.unchanged_since,
                           observation.last_seen, observation.discharge_w, observation.discharge_wh,
                           observation.last_recovery_at)
                if (not device_sn or not observation.signature or observation.capacity_wh <= 0
                        or observation.unchanged_since > observation.last_seen
                        or observation.discharge_wh < 0 or observation.recovery_attempts < 0
                        or not isinstance(observation.recovery_attempts, int)
                        or isinstance(observation.recovery_attempts, bool)
                        or not isinstance(observation.suspect, bool)
                        or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                   and math.isfinite(value) for value in numbers)
                        or not all((isinstance(value, str) and bool(value))
                                   or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                       and math.isfinite(value)) for value in observation.signature)):
                    raise ValueError("invalid measurement observation")
                observations[device_sn] = observation
            self._observations = observations
            self._load_failed = document.get("rebuilding", False)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.persistence_error = "Saved measurement health could not be loaded"
            self._load_failed = True
            log.warning("Measurement state unavailable (%s)", type(error).__name__)

    def flush(self, *, force: bool = True) -> None:
        if self.state_path is None or (not force and time.monotonic() - self._last_saved < 60):
            return
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state_path.parent,
                                             prefix=self.state_path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "rebuilding": self._load_failed, "observations": {
                    device_sn: asdict(observation) for device_sn, observation in self._observations.items()
                }}, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            self._last_saved = time.monotonic()
            self.persistence_error = None
        except (OSError, ValueError, TypeError) as error:
            self._last_saved = time.monotonic()
            self.persistence_error = "Measurement health could not be saved"
            log.warning("Measurement state persistence failed (%s)", type(error).__name__)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def forget(self, device_sn: str | None = None) -> None:
        if device_sn is None:
            self._observations.clear()
            self._load_failed = False
        else:
            self._observations.pop(device_sn, None)
        self.flush()

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
        if previous is None and self._load_failed:
            self._observations[device_sn] = Observation(
                signature, capacity_wh, now, now, discharge_w, suspect=True)
            self.flush()
            return
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
            self.flush(force=previous is not None and previous.suspect)
            return
        if not 0 <= now - previous.last_seen <= MAX_OBSERVATION_GAP_S:
            if not previous.suspect:
                self._observations[device_sn] = Observation(
                    signature, capacity_wh, now, now, discharge_w)
            else:
                previous.last_seen = now
                previous.discharge_w = discharge_w
            self.flush(force=previous.suspect)
            return
        previous.discharge_wh += (
            min(previous.discharge_w, discharge_w) * (now - previous.last_seen) / 3600)
        previous.last_seen = now
        previous.discharge_w = discharge_w
        was_suspect = previous.suspect
        if (now - previous.unchanged_since >= MIN_UNCHANGED_S
                and previous.discharge_wh >= previous.capacity_wh * MIN_DISCHARGE_FRACTION):
            previous.suspect = True
        self.flush(force=previous.suspect != was_suspect)

    def health(self, device_sn: str, now: float) -> MeasurementHealth:
        observation = self._observations.get(device_sn)
        if observation is None:
            return {"suspect": self._load_failed, "unchanged_s": 0,
                    "expected_discharge_wh": 0, "recovery_attempts": 0, "unchanged_since": now}
        unchanged = max(0.0, now - observation.unchanged_since)
        health: MeasurementHealth = {
            "suspect": observation.suspect,
            "unchanged_s": round(unchanged, 1),
            "expected_discharge_wh": round(observation.discharge_wh, 1),
            "recovery_attempts": observation.recovery_attempts,
            "unchanged_since": observation.unchanged_since,
        }
        if self.persistence_error:
            health["persistence_error"] = self.persistence_error
        return health

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
        self.flush()
