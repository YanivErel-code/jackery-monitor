"""Dead-man rescue watchdog: fire grid charging when the unit dies low.

Incident 2026-07-14: the pack ran to empty overnight. The "battery under
5% -> AC on" automation rule never fired while it mattered because (a)
it evaluated the capacity-weighted SYSTEM SOC, which still read ~12%
when the inverter shut itself down, and (b) the moment the unit died its
telemetry froze at that >threshold value — the rule spent the UPS
window staring at a stale number. Every piece of infrastructure (NAS,
router, Kasa plug) stayed up on the UPS; only the trigger logic failed.

This watchdog implements the user's spec directly:
  - Watch the MAIN unit's battery_percent (the number on the device and
    the dashboard), not system SOC.
  - Fresh reading <= ARM_MAIN_SOC_PCT  -> fire the rescue plug NOW.
  - After the main has been seen <= ARM_MAIN_SOC_PCT: if the unit stops
    responding for STALE_FIRE_S, assume it died low and fire the rescue
    plug BLIND — the plug is wall-powered and the network is on the UPS,
    so the toggle works even with the unit dark.
  - A successful fire latches; a fresh reading >= CLEAR_SOC_PCT re-arms.
    Failed toggles do NOT latch (retry every poll tick), mirroring the
    automation engine's failed-action-keeps-the-edge behavior.

The decision core is pure (no I/O) so it's unit-testable; the server's
poll loop owns the Kasa call.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# Fire immediately when a FRESH main-unit reading is at/below this.
ARM_MAIN_SOC_PCT = 5.0
# After arming, this much telemetry silence means "unit died low" -> fire.
STALE_FIRE_S = 60.0
# A fresh reading at/above this clears the latch (matches the user's
# "off after 20%" companion automation rule).
CLEAR_SOC_PCT = 20.0


@dataclass
class RescueState:
    last_fresh_ts: float = 0.0        # newest telemetry timestamp seen
    last_fresh_main: float | None = None  # main SOC at that timestamp
    fired: bool = False               # latched after a successful fire
    # Set by the runner for observability (/api debug), not by decide().
    last_fire_reason: str | None = None
    last_fire_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "last_fresh_ts": self.last_fresh_ts,
            "last_fresh_main": self.last_fresh_main,
            "fired": self.fired,
            "last_fire_reason": self.last_fire_reason,
            "last_fire_ts": self.last_fire_ts,
        }


def decide(now: float, telemetry_ts: float | None,
           main_soc_pct: float | None, st: RescueState) -> str | None:
    """Pure decision: should the rescue plug fire this tick?

    Returns "live" (fresh reading <= arm threshold), "deadman" (armed +
    unit silent >= STALE_FIRE_S), or None. Mutates `st` bookkeeping but
    NOT the `fired` latch — the caller latches only after the Kasa
    toggle actually succeeds, so failures retry next tick.
    """
    if telemetry_ts is None:
        return None  # never heard from this device — nothing to arm on

    # Record the newest reading we've seen (the bridge serves cached
    # entries with frozen ts once the unit dies, so ts only moves while
    # the unit is actually responding).
    if telemetry_ts > st.last_fresh_ts and main_soc_pct is not None:
        st.last_fresh_ts = telemetry_ts
        st.last_fresh_main = float(main_soc_pct)

    silent_s = now - st.last_fresh_ts
    fresh = silent_s < STALE_FIRE_S

    if fresh:
        if st.fired and st.last_fresh_main is not None \
                and st.last_fresh_main >= CLEAR_SOC_PCT:
            st.fired = False  # recovered — re-arm for the next incident
        if (not st.fired and st.last_fresh_main is not None
                and st.last_fresh_main <= ARM_MAIN_SOC_PCT):
            return "live"
        return None

    # Unit silent. Fire only if the LAST thing it told us was "main at/
    # below the arm threshold" — i.e. it went dark while critically low.
    if (not st.fired and st.last_fresh_main is not None
            and st.last_fresh_main <= ARM_MAIN_SOC_PCT
            and silent_s >= STALE_FIRE_S):
        return "deadman"
    return None


def mark_fired(st: RescueState, reason: str, now: float | None = None) -> None:
    """Latch after a SUCCESSFUL Kasa toggle."""
    st.fired = True
    st.last_fire_reason = reason
    st.last_fire_ts = float(now if now is not None else time.time())
