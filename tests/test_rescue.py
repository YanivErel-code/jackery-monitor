"""Dead-man rescue watchdog decision logic.

Regression for 2026-07-14: unit died overnight with frozen telemetry
still reading above the automation rule's threshold; the rule spent the
UPS window staring at a stale number and never fired. The watchdog must
fire when a unit goes silent after being seen critically low on the
MAIN unit's own battery_percent.
"""
from __future__ import annotations

import rescue

T0 = 1_700_000_000.0


def _st():
    return rescue.RescueState()


def test_fires_live_on_fresh_low_reading():
    st = _st()
    assert rescue.decide(T0, T0, 4.0, st) == "live"


def test_no_fire_fresh_above_threshold():
    st = _st()
    assert rescue.decide(T0, T0, 6.0, st) is None


def test_deadman_fires_after_60s_silence_when_armed():
    """The incident: last fresh reading main=3%, then the unit dies.
    59s of silence -> hold; 60s -> fire blind."""
    st = _st()
    # Fresh low reading arrives (would fire live; caller latches only on
    # successful toggle — simulate the toggle FAILING, so no latch).
    assert rescue.decide(T0, T0, 3.0, st) == "live"
    # Unit goes dark: ts frozen at T0. Before the stale window: the
    # fresh-path still fires (retry of the failed live toggle).
    assert rescue.decide(T0 + 59, T0, 3.0, st) == "live"
    # Past the stale window: dead-man path.
    assert rescue.decide(T0 + 60, T0, 3.0, st) == "deadman"


def test_deadman_does_not_fire_when_last_reading_was_healthy():
    """Unit dies at 8% main — above the 5% arm threshold. Silence alone
    must NOT fire (per spec: only 'after the main reached under 5%')."""
    st = _st()
    assert rescue.decide(T0, T0, 8.0, st) is None
    assert rescue.decide(T0 + 3600, T0, 8.0, st) is None


def test_latch_prevents_refire_and_recovery_rearms():
    st = _st()
    assert rescue.decide(T0, T0, 4.0, st) == "live"
    rescue.mark_fired(st, "live", T0)          # toggle succeeded
    # Still low + fresh: latched, no re-fire spam.
    assert rescue.decide(T0 + 10, T0 + 10, 4.0, st) is None
    # Silence while latched: no dead-man double-fire either.
    assert rescue.decide(T0 + 200, T0 + 10, 4.0, st) is None
    # Recovery: fresh reading at/above CLEAR_SOC_PCT re-arms.
    assert rescue.decide(T0 + 300, T0 + 300, 25.0, st) is None
    assert st.fired is False
    # Next incident fires again.
    assert rescue.decide(T0 + 400, T0 + 400, 5.0, st) == "live"


def test_failed_toggle_retries_next_tick():
    """decide() must not latch by itself — a failed Kasa call retries."""
    st = _st()
    assert rescue.decide(T0, T0, 2.0, st) == "live"
    assert rescue.decide(T0 + 2, T0 + 2, 2.0, st) == "live"  # still firing


def test_never_seen_device_is_noop():
    st = _st()
    assert rescue.decide(T0, None, None, st) is None
    assert rescue.decide(T0 + 999, None, None, st) is None


def test_stale_entry_with_none_soc_keeps_armed_value():
    """Bridge cache may serve telemetry dicts without battery_percent
    during degradation; the armed last_fresh_main must survive."""
    st = _st()
    assert rescue.decide(T0, T0, 4.0, st) == "live"
    assert rescue.decide(T0 + 61, T0, None, st) == "deadman"
