"""Inverter recovery watchdog.

The Jackery 5000+ inverter trips its AC output when an overload or
thermal event exceeds its hardware protection threshold. Once tripped,
the AC port stays OFF until something commands it back ON — the inverter
does not auto-recover. The user's invariant is "AC stays ON 100% of the
time," so we treat any AC=OFF observation as a trip and attempt to
recover automatically.

This module is the pure-function state machine; the server's poll_loop
drives it via `evaluate()` after each successful telemetry read, and
issues the AC-on MQTT command on "retry" (or an off→on cycle on
"cycle").

Two trip signatures, learned the hard way:

1. PORT-OFF trip (`oac` → False). The original design. Recovery: plain
   AC-on. After `max_attempts` (default 5) attempts at 10s intervals the
   watchdog latches an error badge but does NOT stop — it keeps retrying
   at the slow interval (60s) forever. The 2026-07-14 dead-battery
   incident showed the unit refuses AC-on for 1min+ while booting on
   grid power; a give-up design left the house dark.

2. HARDWARE trip (2026-07-21): an overload shutdown that does NOT flip
   `oac` — the unit kept reporting the AC port ON while output sat at
   0W. Detection is OPT-IN via `collapse_floor_w` (0 = disabled; the
   operator sets it below their known 24/7 base load, e.g. 100W on a rig
   whose house floor is ~450W): after output was recently at/above
   OUTPUT_BASELINE_W, if COLLAPSE_MIN_SAMPLES *distinct* fresh telemetry
   samples (deduped by sample_ts) sit at/below the floor for at least
   COLLAPSE_MIN_DURATION_S, and the drop from baseline to floor was
   abrupt (within ABRUPT_DROP_MAX_S), a hardware-trip episode latches.
   Recovery: "cycle" (off → on; plain on is a no-op when the port claims
   on), capped at MAX_CYCLES_PER_EPISODE per episode — a false positive
   costs at most two brief cuts, never a burst. The episode latch clears
   ONLY on genuine recovery (fresh output above the floor); it does NOT
   time out while output is still collapsed, so the badge can never be
   silently wiped mid-outage.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

# Action returned by evaluate(); callers translate to MQTT toggles + UI.
# "cycle" = hardware-trip recovery: the port CLAIMS on (oac=1) but output
# collapsed, so a plain AC-on is a no-op — the caller must toggle
# off -> on to reset the inverter.
Action = Literal["idle", "retry", "cycle", "waiting", "user_grace", "error"]


@dataclass
class WatchdogState:
    """Per-device state. 0/None values mean "no recovery in progress."

    Persisted only in memory — restart resets, which is fine: a restart
    means the dashboard is back online and the user is watching."""
    consecutive_attempts: int = 0
    # Pacing clock. Deliberately PRESERVED across idle resets (only
    # dismiss_error zeroes it): a flapping load that repeatedly arms and
    # clears the collapse detector is still rate-limited to one fire per
    # retry interval instead of an unpaced storm of power cuts.
    last_attempt_ts: float = 0.0
    # Stamped by the AC-toggle endpoint when the user explicitly turns
    # AC off via our UI. Suppresses the watchdog for `user_grace_s`
    # afterward so we don't fight an intentional user action.
    last_user_off_ts: float = 0.0
    # Set when we've exhausted max_attempts (port-off path) or the cycle
    # cap (hardware-trip path). UI shows this; clicking dismiss resets.
    error_message: str | None = None
    # ---- hardware-trip (output-collapse) detection ----
    last_high_output_ts: float = 0.0   # newest fresh sample >= baseline
    first_low_ts: float = 0.0          # start of the current low streak
    low_samples: int = 0               # DISTINCT fresh samples in streak
    last_sample_ts: float = 0.0        # dedup: bridge serves cached frames
    hw_trip_active: bool = False       # episode latch — see module doc
    episode_cycles: int = 0            # off->on cycles fired this episode


DEFAULT_RETRY_INTERVAL_S = 10.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_USER_GRACE_S = 60.0
# After max_attempts fast retries, keep trying at this cadence forever
# (post-shutdown the unit refuses AC-on for 1min+ while it boots).
DEFAULT_SLOW_RETRY_INTERVAL_S = 60.0

# Hardware-trip signature gates (see module docstring). The floor itself
# is per-rig config (settings: inverter_trip_recovery_min_w; 0 disables).
OUTPUT_BASELINE_W = 300.0        # "loads were genuinely running"
COLLAPSE_MIN_SAMPLES = 3         # distinct fresh samples at/below floor
COLLAPSE_MIN_DURATION_S = 6.0    # wall-clock span of the low streak
ABRUPT_DROP_MAX_S = 15.0         # baseline -> floor must happen this fast
MAX_CYCLES_PER_EPISODE = 2       # then latch the badge and stop cycling


def _track_collapse(state: WatchdogState, ac_on: bool,
                    output_w: float | None, sample_ts: float | None,
                    collapse_floor_w: float, now: float) -> None:
    """Update the collapse detector from one telemetry frame. Only
    DISTINCT frames count (sample_ts dedup — the bridge re-serves cached
    frames every poll tick, and a single glitched 0W frame must never
    satisfy the whole debounce)."""
    if collapse_floor_w <= 0 or output_w is None:
        return
    if sample_ts is not None:
        if sample_ts == state.last_sample_ts:
            return  # same cached frame — not a new observation
        state.last_sample_ts = sample_ts
    if not ac_on:
        # The port-off path owns recovery; a streak accumulated while AC
        # was off must not fire a gratuitous cycle right after recovery.
        state.first_low_ts = 0.0
        state.low_samples = 0
        return
    if output_w >= OUTPUT_BASELINE_W:
        state.last_high_output_ts = now
        state.first_low_ts = 0.0
        state.low_samples = 0
        if state.hw_trip_active:
            state.hw_trip_active = False   # genuine recovery
            state.episode_cycles = 0
    elif output_w <= collapse_floor_w:
        if state.first_low_ts == 0.0:
            state.first_low_ts = now
        state.low_samples += 1
    else:
        # Mid-range: the house demonstrably has power.
        state.first_low_ts = 0.0
        state.low_samples = 0
        if state.hw_trip_active:
            state.hw_trip_active = False
            state.episode_cycles = 0
    # Arm a new episode only on the full signature.
    if (not state.hw_trip_active and ac_on
            and state.low_samples >= COLLAPSE_MIN_SAMPLES
            and state.first_low_ts > 0
            and (now - state.first_low_ts) >= COLLAPSE_MIN_DURATION_S
            and state.last_high_output_ts > 0
            and (state.first_low_ts - state.last_high_output_ts)
            <= ABRUPT_DROP_MAX_S):
        state.hw_trip_active = True
        state.episode_cycles = 0


def evaluate(
    state: WatchdogState,
    ac_on: bool,
    output_w: float | None = None,
    *,
    sample_ts: float | None = None,
    collapse_floor_w: float = 0.0,
    now_ts: float | None = None,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    user_grace_s: float = DEFAULT_USER_GRACE_S,
    slow_retry_interval_s: float = DEFAULT_SLOW_RETRY_INTERVAL_S,
) -> Action:
    """Drive the recovery state machine one tick. Mutates `state`.

    Args:
        state: per-device runtime, modified in place.
        ac_on: current AC-port state from the latest telemetry.
        output_w: live output power (hardware-trip detection input).
        sample_ts: the telemetry frame's own timestamp — used to dedup
            re-served cached frames so the collapse debounce counts
            distinct observations, not poll ticks.
        collapse_floor_w: hardware-trip detection floor. 0 (default)
            DISABLES detection; operators set it below their known 24/7
            base load (settings: inverter_trip_recovery_min_w).
        now_ts: clock injection for tests; defaults to time.time().
        retry_interval_s: seconds between retry attempts.
        max_attempts: fast attempts before latching the error badge.
        user_grace_s: skip watchdog this long after a user-initiated OFF.
        slow_retry_interval_s: retry cadence AFTER the error badge is
            latched on the port-off path — that recovery never stops.

    Returns one of the Action strings. Callers should:
        - "retry"       → send AC-on MQTT command
        - "cycle"       → hardware trip: send AC-off, pause, AC-on
        - "idle"        → AC healthy; nothing to do
        - "waiting"     → mid-recovery, holding for retry interval
        - "user_grace"  → user turned AC off recently; suppressed
        - "error"       → badge latched (port-off path keeps slow
                          retries; hardware-trip path stops after the
                          cycle cap until output genuinely recovers)
    """
    now = float(now_ts if now_ts is not None else time.time())
    grace_active = bool(state.last_user_off_ts
                        and (now - state.last_user_off_ts) < user_grace_s)

    if grace_active:
        # Don't accumulate collapse evidence off the user's own OFF.
        state.first_low_ts = 0.0
        state.low_samples = 0
    else:
        _track_collapse(state, ac_on, output_w, sample_ts,
                        collapse_floor_w, now)

    effective_on = ac_on and not state.hw_trip_active
    if effective_on:
        # AC healthy (either our recovery worked or an external action
        # fixed it). Clear the recovery machinery — but keep
        # last_attempt_ts as a pacing floor (see WatchdogState) and the
        # collapse bookkeeping (it self-maintains in _track_collapse).
        state.consecutive_attempts = 0
        state.error_message = None
        return "idle"
    if grace_active:
        return "user_grace"
    # Hardware-trip episode: hard cap on off->on cycles. A genuine trip
    # either resets on the first cycle or two; anything beyond that is
    # more likely a false positive or a unit that needs eyes on it —
    # repeating unconfirmed 2s outages multiplies harm without adding
    # recovery power. The episode (and badge) clears only on genuine
    # output recovery, or hands off to the plain path if oac goes False.
    if ac_on and state.hw_trip_active \
            and state.episode_cycles >= MAX_CYCLES_PER_EPISODE:
        if not state.error_message:
            state.error_message = (
                f"Output collapsed while the AC port still reports ON — "
                f"hardware trip suspected. Cycled AC off/on "
                f"{MAX_CYCLES_PER_EPISODE}x without output returning; "
                f"stopped to avoid repeated power cuts. Check the unit. "
                f"Recovery resumes automatically when output returns "
                f"(or the port reports OFF)."
            )
        return "error"
    if state.error_message:
        # Port-off path: badge latched but never give up — keep nudging
        # at the slow cadence (dead-battery reboots refuse AC-on for
        # 1min+). On the hardware-trip path this branch is only reachable
        # below the cycle cap.
        if (now - state.last_attempt_ts) >= slow_retry_interval_s:
            state.consecutive_attempts += 1
            state.last_attempt_ts = now
            if ac_on:
                state.episode_cycles += 1
                return "cycle"
            return "retry"
        return "error"
    if state.consecutive_attempts >= max_attempts:
        state.error_message = (
            f"AC output remained OFF after {max_attempts} retry attempts "
            f"(~{int(max_attempts * retry_interval_s)}s). Still retrying "
            f"every {int(slow_retry_interval_s)}s — after a dead-battery "
            f"shutdown the unit can take a minute or more to accept "
            f"AC-on. Dismiss to reset the fast-retry sequence."
        )
        return "error"
    # Fire when the pacing interval allows. Note attempts==0 no longer
    # bypasses pacing: last_attempt_ts survives resets, so an arm/clear/
    # re-arm flap can't fire faster than retry_interval_s.
    fire: Action = "cycle" if ac_on else "retry"
    if (now - state.last_attempt_ts) >= retry_interval_s:
        state.consecutive_attempts += 1
        state.last_attempt_ts = now
        if fire == "cycle":
            state.episode_cycles += 1
        return fire
    return "waiting"


def record_user_off(state: WatchdogState, now_ts: float | None = None) -> None:
    """Stamp the user-initiated AC-off timestamp. Called from the AC
    toggle endpoint when the user clicks AC OFF in our UI."""
    state.last_user_off_ts = float(now_ts if now_ts is not None else time.time())


def dismiss_error(state: WatchdogState) -> None:
    """Reset the latched error. Next trip observation starts a fresh
    recovery sequence. Called from the UI's "dismiss" click."""
    state.consecutive_attempts = 0
    state.last_attempt_ts = 0.0
    state.error_message = None
    state.hw_trip_active = False
    state.episode_cycles = 0
    state.first_low_ts = 0.0
    state.low_samples = 0


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
        "hw_trip_active": state.hw_trip_active,
        "episode_cycles": state.episode_cycles,
        "low_samples": state.low_samples,
        "last_high_output_ts": state.last_high_output_ts,
    }
