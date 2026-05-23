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
    assert s.last_attempt_ts == 0.0


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


def test_error_stays_latched():
    """Once latched, subsequent ticks keep returning 'error' until AC
    recovers or dismiss() is called."""
    s = _fresh()
    t = 1000.0
    for _ in range(6):
        inverter_watchdog.evaluate(s, False, now_ts=t)
        t += 10.0
    # Latched.
    assert s.error_message is not None
    # 5 minutes pass — still error.
    assert inverter_watchdog.evaluate(s, False, now_ts=t + 300) == "error"


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
