"""Unit tests for backoff.LoopBackoff."""

from __future__ import annotations

from backoff import LoopBackoff


def test_clean_run_uses_base_interval():
    b = LoopBackoff(max_s=300)
    assert b.next_sleep(60) == 60


def test_failure_doubles_until_cap():
    b = LoopBackoff(max_s=300)
    b.record_failure()
    assert b.next_sleep(60) == 120
    b.record_failure()
    assert b.next_sleep(60) == 240
    # Next doubling would be 480; capped at 300.
    b.record_failure()
    assert b.next_sleep(60) == 300


def test_reset_returns_to_base():
    b = LoopBackoff(max_s=300)
    for _ in range(4):
        b.record_failure()
    assert b.next_sleep(60) == 300
    b.reset()
    assert b.next_sleep(60) == 60


def test_failure_counter_is_capped():
    # Without the cap, 2**fails would overflow for very long-running
    # processes stuck against a permanent failure.
    b = LoopBackoff(max_s=10_000)
    for _ in range(1000):
        b.record_failure()
    # Sleep is bounded by max_s regardless of how many failures.
    assert b.next_sleep(60) == 10_000


def test_base_s_is_re_read_each_call():
    # Settings can change between iterations; next_sleep takes base_s
    # as an argument so the latest value is honored.
    b = LoopBackoff(max_s=10_000)
    assert b.next_sleep(30) == 30
    assert b.next_sleep(120) == 120
