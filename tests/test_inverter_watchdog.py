"""State-machine tests for inverter_watchdog.evaluate().

Pure functions, no asyncio. Each test drives the state machine through
explicit `now_ts` so we don't depend on real clock progression.
"""
from __future__ import annotations

import inverter_watchdog


def _fresh():
    return inverter_watchdog.WatchdogState()


# ---------- AC=ON (idle) ----------
def test_ac_on_returns_idle():
    s = _fresh()
    assert inverter_watchdog.evaluate(s, True, now_ts=1000.0) == "idle"
    assert s.consecutive_attempts == 0
    assert s.error_message is None


def test_ac_on_clears_mid_recovery_state():
    """If AC recovered during a retry sequence, reset everything."""
    s = _fresh()
    s.consecutive_attempts = 3
    s.last_attempt_ts = 950.0
    s.error_message = "stale message that should clear"
    assert inverter_watchdog.evaluate(s, True, now_ts=1000.0) == "idle"
    assert s.consecutive_attempts == 0
    assert s.error_message is None
    # last_attempt_ts is deliberately PRESERVED as a pacing floor so an
    # arm/clear/re-arm flap can't fire faster than the retry interval.
    assert s.last_attempt_ts == 950.0


# ---------- First AC=OFF observation ----------
def test_first_off_fires_immediately():
    """First OFF observation fires the retry on the same tick — no
    initial wait. (Better recovery latency.)"""
    s = _fresh()
    action = inverter_watchdog.evaluate(s, False, now_ts=1000.0)
    assert action == "retry"
    assert s.consecutive_attempts == 1
    assert s.last_attempt_ts == 1000.0


# ---------- Hold between retries ----------
def test_holds_during_retry_interval():
    """Within retry_interval_s of the last attempt, return 'waiting'."""
    s = _fresh()
    inverter_watchdog.evaluate(s, False, now_ts=1000.0)
    # 5s later, still within the 10s default interval.
    action = inverter_watchdog.evaluate(s, False, now_ts=1005.0)
    assert action == "waiting"
    assert s.consecutive_attempts == 1


def test_fires_again_after_interval():
    """Exactly at retry_interval_s, the next attempt fires."""
    s = _fresh()
    inverter_watchdog.evaluate(s, False, now_ts=1000.0)
    action = inverter_watchdog.evaluate(s, False, now_ts=1010.0)
    assert action == "retry"
    assert s.consecutive_attempts == 2


# ---------- Max attempts → error ----------
def test_latches_error_after_max_attempts():
    """5 attempts at 10s intervals → 50s total → error latched."""
    s = _fresh()
    t = 1000.0
    for expected_attempt in range(1, 6):
        action = inverter_watchdog.evaluate(s, False, now_ts=t)
        assert action == "retry"
        assert s.consecutive_attempts == expected_attempt
        t += 10.0
    # 6th call (50s after first) should latch the error.
    action = inverter_watchdog.evaluate(s, False, now_ts=t)
    assert action == "error"
    assert s.error_message is not None
    # Don't assert exact text — message is copy-edited often. Just check
    # it mentions the attempt count from DEFAULT_MAX_ATTEMPTS.
    assert str(inverter_watchdog.DEFAULT_MAX_ATTEMPTS) in s.error_message


def test_error_badge_latched_but_slow_retries_continue():
    """The 2026-07-14 case: after a dead-battery shutdown the unit
    refuses AC-on for 1min+, so all 5 fast attempts fail. The badge
    latches, but recovery must NEVER stop — slow retries keep firing
    every DEFAULT_SLOW_RETRY_INTERVAL_S until the unit accepts."""
    s = _fresh()
    t = 1000.0
    for _ in range(6):
        inverter_watchdog.evaluate(s, False, now_ts=t)
        t += 10.0
    # Badge latched after the fast phase...
    assert s.error_message is not None
    last_attempt = s.last_attempt_ts
    # ...within the slow interval: hold (reported as "error").
    assert inverter_watchdog.evaluate(s, False, now_ts=last_attempt + 30) == "error"
    # ...past the slow interval: a retry fires, badge stays.
    assert inverter_watchdog.evaluate(
        s, False, now_ts=last_attempt + 61) == "retry"
    assert s.error_message is not None
    # And it keeps going indefinitely (another interval, another retry).
    assert inverter_watchdog.evaluate(
        s, False, now_ts=last_attempt + 61 + 61) == "retry"


def test_ac_recovery_clears_latched_error():
    """If AC comes back ON (e.g. manual fix), drop the error."""
    s = _fresh()
    s.error_message = "old error"
    s.consecutive_attempts = 5
    assert inverter_watchdog.evaluate(s, True, now_ts=2000.0) == "idle"
    assert s.error_message is None
    assert s.consecutive_attempts == 0


# ---------- User OFF grace ----------
def test_user_off_grace_suppresses_retry():
    """User intentionally turned AC off via the UI — don't fight them."""
    s = _fresh()
    inverter_watchdog.record_user_off(s, now_ts=1000.0)
    assert inverter_watchdog.evaluate(s, False, now_ts=1030.0) == "user_grace"
    assert s.consecutive_attempts == 0  # no retry fired


def test_user_off_grace_expires():
    """After 60s the watchdog assumes the user moved on and re-engages."""
    s = _fresh()
    inverter_watchdog.record_user_off(s, now_ts=1000.0)
    # Just inside grace → suppressed.
    assert inverter_watchdog.evaluate(s, False, now_ts=1059.0) == "user_grace"
    # Just outside → first retry fires.
    assert inverter_watchdog.evaluate(s, False, now_ts=1061.0) == "retry"
    assert s.consecutive_attempts == 1


def test_user_grace_overrides_in_flight_retries():
    """If the user clicks AC OFF while retries are already in flight,
    suspend them — the user's action wins until grace expires. Attempt
    counter is preserved so retries resume from where they left off."""
    s = _fresh()
    inverter_watchdog.evaluate(s, False, now_ts=1000.0)
    inverter_watchdog.evaluate(s, False, now_ts=1010.0)
    assert s.consecutive_attempts == 2  # mid-recovery
    inverter_watchdog.record_user_off(s, now_ts=1015.0)
    assert inverter_watchdog.evaluate(s, False, now_ts=1020.0) == "user_grace"
    # Attempt counter untouched while in grace.
    assert s.consecutive_attempts == 2


def test_grace_resumes_from_existing_attempt_count():
    """After grace expires, the next retry continues the sequence (not
    a fresh start). If we'd already done 2 attempts, the next one is #3
    — only `dismiss_error()` or AC=ON resets the counter."""
    s = _fresh()
    inverter_watchdog.evaluate(s, False, now_ts=1000.0)
    inverter_watchdog.evaluate(s, False, now_ts=1010.0)
    assert s.consecutive_attempts == 2
    inverter_watchdog.record_user_off(s, now_ts=1015.0)
    # Past the 60s grace window AND past the 10s retry interval.
    action = inverter_watchdog.evaluate(s, False, now_ts=1080.0)
    assert action == "retry"
    assert s.consecutive_attempts == 3  # continues, doesn't restart


# ---------- Dismiss ----------
def test_dismiss_resets_state():
    """User clicked dismiss after the error latched. Next AC=OFF
    observation must start a fresh retry sequence."""
    s = _fresh()
    s.consecutive_attempts = 5
    s.last_attempt_ts = 1500.0
    s.error_message = "exhausted"
    inverter_watchdog.dismiss_error(s)
    assert s.error_message is None
    assert s.consecutive_attempts == 0
    # And immediately retry-able.
    assert inverter_watchdog.evaluate(s, False, now_ts=2000.0) == "retry"
    assert s.consecutive_attempts == 1


# ---------- Registry ----------
def test_registry_returns_same_state_per_sn():
    inverter_watchdog.reset_state()
    a1 = inverter_watchdog.get_state("SN-A")
    a2 = inverter_watchdog.get_state("SN-A")
    b = inverter_watchdog.get_state("SN-B")
    assert a1 is a2
    assert a1 is not b


def test_state_to_dict_shape():
    s = _fresh()
    s.consecutive_attempts = 2
    s.error_message = "hi"
    d = inverter_watchdog.state_to_dict(s)
    assert d["consecutive_attempts"] == 2
    assert d["error_message"] == "hi"
    assert "last_attempt_ts" in d
    assert "last_user_off_ts" in d


# ---------- Hardware-trip detection (oac stays ON, output collapses) ----------
# Detection is OPT-IN: collapse_floor_w=0 (default) disables it. The
# debounce counts DISTINCT telemetry frames (sample_ts dedup) spanning at
# least COLLAPSE_MIN_DURATION_S, after an abrupt drop from >= baseline.
FLOOR = 20.0


def _ev(s, ac, out, t, ts=None, floor=FLOOR):
    return inverter_watchdog.evaluate(
        s, ac, out, sample_ts=(t if ts is None else ts),
        collapse_floor_w=floor, now_ts=t)


def test_hw_trip_disabled_by_default():
    """floor=0 -> the collapse detector never arms, whatever output does."""
    s = _fresh()
    t = 1000.0
    inverter_watchdog.evaluate(s, True, 900.0, sample_ts=t, now_ts=t)
    for i in range(1, 8):
        a = inverter_watchdog.evaluate(
            s, True, 0.0, sample_ts=t + i * 2, now_ts=t + i * 2)
        assert a == "idle"


def test_hw_trip_fires_cycle_on_full_signature():
    """2026-07-21 replay: 1795W -> 0W with oac stuck ON. Three distinct
    low samples spanning >= 6s after an abrupt drop -> 'cycle'."""
    s = _fresh()
    t = 1000.0
    assert _ev(s, True, 1795.0, t) == "idle"
    assert _ev(s, True, 0.0, t + 2) == "idle"     # sample 1 (streak start)
    assert _ev(s, True, 0.0, t + 4) == "idle"     # sample 2, 4s < 6s span
    assert _ev(s, True, 0.0, t + 8) == "cycle"    # sample 3, 8s span >= 6s
    assert s.hw_trip_active and s.episode_cycles == 1
    # Pacing: within the 10s interval -> waiting.
    assert _ev(s, True, 0.0, t + 10) == "waiting"
    assert _ev(s, True, 0.0, t + 19) == "cycle"   # 2nd (and last) cycle
    assert s.episode_cycles == 2


def test_hw_trip_episode_cap_then_error_no_more_cycles():
    """After MAX_CYCLES_PER_EPISODE the badge latches and NO further
    cycles fire until genuine recovery — a false positive costs at most
    two brief cuts, never a burst."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    _ev(s, True, 0.0, t + 2)
    _ev(s, True, 0.0, t + 4)
    assert _ev(s, True, 0.0, t + 8) == "cycle"
    assert _ev(s, True, 0.0, t + 19) == "cycle"
    # Cap reached: error latches; even far in the future, no cycling.
    assert _ev(s, True, 0.0, t + 30) == "error"
    assert "hardware trip" in (s.error_message or "").lower()         or "cycled ac" in (s.error_message or "").lower()
    assert _ev(s, True, 0.0, t + 1000) == "error"
    assert s.episode_cycles == 2


def test_hw_trip_badge_survives_long_outage_and_clears_on_recovery():
    """Reviewer-found critical: the episode latch must NOT time out while
    output is still collapsed (the old baseline-expiry reset silently
    wiped the badge mid-outage). It clears only on genuine recovery."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    for dt in (2, 4, 8, 19):
        _ev(s, True, 0.0, t + dt)
    _ev(s, True, 0.0, t + 30)                      # error latched
    # 20 minutes later, STILL collapsed: badge + latch intact.
    assert _ev(s, True, 0.0, t + 1200) == "error"
    assert s.hw_trip_active and s.error_message
    # Output genuinely returns -> full recovery, badge cleared.
    assert _ev(s, True, 850.0, t + 1300) == "idle"
    assert not s.hw_trip_active and s.error_message is None
    assert s.episode_cycles == 0


def test_hw_trip_stale_frame_does_not_accumulate():
    """Reviewer-found: the bridge re-serves cached frames every poll
    tick. The SAME sample_ts re-evaluated must count once, not three
    times — a single glitched 0W frame can never satisfy the debounce."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    frozen = t + 2
    for i in range(6):  # same frame re-served for 12s
        a = inverter_watchdog.evaluate(
            s, True, 0.0, sample_ts=frozen, collapse_floor_w=FLOOR,
            now_ts=t + 2 + i * 2)
        assert a == "idle"
    assert s.low_samples == 1


def test_hw_trip_no_accumulation_while_ac_off():
    """Reviewer-found: a streak accumulated during a normal oac=0 outage
    must not fire a gratuitous cycle right after AC recovers."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    # Port goes OFF (plain trip) — low output readings while off must
    # not count toward the collapse streak.
    inverter_watchdog.evaluate(s, False, 0.0, sample_ts=t + 2,
                               collapse_floor_w=FLOOR, now_ts=t + 2)
    inverter_watchdog.evaluate(s, False, 0.0, sample_ts=t + 4,
                               collapse_floor_w=FLOOR, now_ts=t + 4)
    assert s.low_samples == 0
    # AC restored; first ON frames still read 0W briefly (op lag) — must
    # need a FRESH full streak, so no instant cycle.
    a = _ev(s, True, 0.0, t + 20)
    assert a == "idle"


def test_hw_trip_needs_abrupt_drop():
    """A gradual ramp-down (duty-cycle load finishing) must not arm:
    baseline -> floor must happen within ABRUPT_DROP_MAX_S."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    _ev(s, True, 150.0, t + 20)   # mid-range for a while (gradual)
    _ev(s, True, 90.0, t + 30)
    # Now hits the floor — but 30s+ since last >=300W reading.
    _ev(s, True, 0.0, t + 32)
    _ev(s, True, 0.0, t + 34)
    assert _ev(s, True, 0.0, t + 40) == "idle"
    assert not s.hw_trip_active


def test_hw_trip_midrange_resets_streak_and_recovers_episode():
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    _ev(s, True, 0.0, t + 2)
    _ev(s, True, 0.0, t + 4)
    assert _ev(s, True, 0.0, t + 8) == "cycle"
    # Output comes back mid-range (house has power) -> episode over.
    assert _ev(s, True, 120.0, t + 12) == "idle"
    assert not s.hw_trip_active and s.episode_cycles == 0


def test_hw_trip_grace_suppresses_accumulation():
    """User clicked AC OFF in the UI: no collapse evidence accumulates
    off the user's own action."""
    s = _fresh()
    t = 1000.0
    _ev(s, True, 900.0, t)
    inverter_watchdog.record_user_off(s, now_ts=t + 1)
    for dt in (2, 4, 8, 10):
        inverter_watchdog.evaluate(s, True, 0.0, sample_ts=t + dt,
                                   collapse_floor_w=FLOOR, now_ts=t + dt)
    assert s.low_samples == 0 and not s.hw_trip_active


def test_no_output_signal_is_backcompat():
    """output_w=None / floor unset -> ac_on-only behavior unchanged."""
    s = _fresh()
    assert inverter_watchdog.evaluate(s, True, now_ts=1000.0) == "idle"
    assert inverter_watchdog.evaluate(s, False, now_ts=1002.0) == "retry"
