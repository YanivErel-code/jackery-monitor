"""Inverter recovery watchdog.

The Jackery 5000+ inverter trips its AC output (port `oac` → False) when
an overload or thermal event exceeds its hardware protection threshold.
Once tripped, the AC port stays OFF until something commands it back ON
— the inverter does not auto-recover. The user's invariant is "AC stays
ON 100% of the time," so we treat any AC=OFF observation as a trip and
attempt to recover automatically.

This module is the pure-function state machine; the server's poll_loop
drives it via `evaluate()` after each successful telemetry read, and
issues the AC-on MQTT command when `evaluate()` returns "retry".

State machine (per device):

    AC observed ON                    → reset everything → "idle"
    AC observed OFF, user OFF <60s    → "user_grace"  (no retry)
    AC observed OFF, error latched    → "error"       (no more retries)
    AC observed OFF, attempt 0        → fire #1 immediately → "retry"
    AC observed OFF, ≥10s since last  → fire next attempt → "retry"
    AC observed OFF, <10s since last  → "waiting"
    AC observed OFF, attempts == max  → set error → "error"

After `max_attempts` (default 5) attempts at 10s intervals (~50s total),
the watchdog latches an error_message and stops retrying. The UI shows
this on the AC button; clicking dismisses (resets counter; the next AC=OFF
observation will trigger a fresh sequence).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

# Action returned by evaluate(); callers translate to MQTT toggles + UI.
Action = Literal["idle", "retry", "waiting", "user_grace", "error"]


@dataclass
class WatchdogState:
    """Per-device state. 0/None values mean "no recovery in progress."

    Persisted only in memory — restart resets, which is fine: a restart
    means the dashboard is back online and the user is watching."""
    consecutive_attempts: int = 0
    last_attempt_ts: float = 0.0
    # Stamped by the AC-toggle endpoint when the user explicitly turns
    # AC off via our UI. Suppresses the watchdog for `user_grace_s`
    # afterward so we don't fight an intentional user action.
    last_user_off_ts: float = 0.0
    # Set when we've exhausted max_attempts. UI shows this; clicking
    # dismiss resets the whole state so the next OFF triggers a fresh
    # sequence.
    error_message: str | None = None


DEFAULT_RETRY_INTERVAL_S = 10.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_USER_GRACE_S = 60.0


def evaluate(
    state: WatchdogState,
    ac_on: bool,
    *,
    now_ts: float | None = None,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    user_grace_s: float = DEFAULT_USER_GRACE_S,
) -> Action:
    """Drive the recovery state machine one tick. Mutates `state`.

    Args:
        state: per-device runtime, modified in place.
        ac_on: current AC-port state from the latest telemetry.
        now_ts: clock injection for tests; defaults to time.time().
        retry_interval_s: seconds between retry attempts.
        max_attempts: how many attempts before latching an error.
        user_grace_s: skip watchdog this long after a user-initiated OFF.

    Returns one of the Action strings. Callers should:
        - "retry"       → send AC-on MQTT command
        - "idle"        → AC recovered (or was already ON); nothing to do
        - "waiting"     → mid-recovery, holding for retry interval
        - "user_grace"  → user turned AC off recently; suppressed
        - "error"       → all attempts failed; surface to UI
    """
    now = float(now_ts if now_ts is not None else time.time())
    if ac_on:
        # AC came back (either our retry worked or some external action
        # turned it back on). Clear everything — fresh slate for next trip.
        state.consecutive_attempts = 0
        state.last_attempt_ts = 0.0
        state.error_message = None
        return "idle"
    # AC is off from here on.
    if state.last_user_off_ts and (now - state.last_user_off_ts) < user_grace_s:
        return "user_grace"
    if state.error_message:
        return "error"
    if state.consecutive_attempts >= max_attempts:
        state.error_message = (
            f"AC output remained OFF after {max_attempts} retry attempts "
            f"(~{int(max_attempts * retry_interval_s)}s). The inverter "
            f"may have a stuck fault — check the Jackery in person and "
            f"click to dismiss this error to try again."
        )
        return "error"
    # First attempt: fire immediately on first AC=OFF observation.
    # Subsequent: hold until retry_interval_s has elapsed.
    if state.consecutive_attempts == 0:
        state.consecutive_attempts = 1
        state.last_attempt_ts = now
        return "retry"
    if (now - state.last_attempt_ts) >= retry_interval_s:
        state.consecutive_attempts += 1
        state.last_attempt_ts = now
        return "retry"
    return "waiting"


def record_user_off(state: WatchdogState, now_ts: float | None = None) -> None:
    """Stamp the user-initiated AC-off timestamp. Called from the AC
    toggle endpoint when the user clicks AC OFF in our UI."""
    state.last_user_off_ts = float(now_ts if now_ts is not None else time.time())


def dismiss_error(state: WatchdogState) -> None:
    """Reset the latched error. Next AC=OFF observation starts a fresh
    recovery sequence. Called from the UI's "dismiss" click."""
    state.consecutive_attempts = 0
    state.last_attempt_ts = 0.0
    state.error_message = None


# ---------- Per-device runtime registry ----------
_runtime: dict[str, WatchdogState] = {}
_runtime_lock = threading.Lock()


def get_state(device_sn: str) -> WatchdogState:
    """Return (and lazily create) the per-device watchdog state."""
    with _runtime_lock:
        if device_sn not in _runtime:
            _runtime[device_sn] = WatchdogState()
        return _runtime[device_sn]


def reset_state(device_sn: str | None = None) -> None:
    """Clear cached state. Test hook; also called by callers that
    explicitly want a fresh slate (e.g. dismiss-error endpoint)."""
    with _runtime_lock:
        if device_sn:
            _runtime.pop(device_sn, None)
        else:
            _runtime.clear()


def state_to_dict(state: WatchdogState) -> dict:
    """JSON-friendly snapshot for the UI."""
    return {
        "consecutive_attempts": state.consecutive_attempts,
        "last_attempt_ts": state.last_attempt_ts,
        "last_user_off_ts": state.last_user_off_ts,
        "error_message": state.error_message,
    }
