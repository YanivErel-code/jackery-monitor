"""Exponential backoff helper for the long-running poll loops.

Same idea as the per-device retry on the kasa reconciler, but at the
loop-level: when the loop body fails, sleep longer before retrying so
a downstream outage (bridge down, network blip, db locked) doesn't
turn into a tight retry storm. Resets the moment work succeeds.

Usage:

    backoff = LoopBackoff(max_s=300)
    while True:
        base_s = ...  # could change per iteration (e.g. user setting)
        try:
            await do_work()
            backoff.reset()
        except Exception:
            backoff.record_failure()
            log.exception("loop iteration failed")
        await asyncio.sleep(backoff.next_sleep(base_s))
"""

from __future__ import annotations


class LoopBackoff:
    # Cap the failure counter so 2**fails doesn't overflow for
    # processes that run for weeks with a stuck dependency.
    _FAILS_CAP = 8

    def __init__(self, max_s: float) -> None:
        self.max_s = max_s
        self.fails = 0

    def reset(self) -> None:
        self.fails = 0

    def record_failure(self) -> None:
        self.fails = min(self.fails + 1, self._FAILS_CAP)

    def next_sleep(self, base_s: float) -> float:
        """Sleep for `base_s` after a clean run; double each consecutive
        failure up to `max_s`. base_s is passed in (rather than stored)
        so callers re-reading a settings value see it on the next tick."""
        return min(base_s * (2 ** self.fails), self.max_s)
