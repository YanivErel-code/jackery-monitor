"""Bounded, short-lived cache for expensive dashboard-only database reads.

Cold live snapshots return immediately; aggregates appear on the next update.
Refreshes share one worker per key and never run on the HTTP/WS event loop.
This cache must not be used for telemetry or charging/control decisions.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-read")


class DashboardCache:
    def __init__(self, ttl_s: float = 30, *, clock=time.monotonic,
                 executor=_workers, max_entries: int = 64):
        self.ttl_s = ttl_s
        self.clock = clock
        self.executor = executor
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._values: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._pending: dict[Hashable, Future] = {}
        self._retry_after: dict[Hashable, float] = {}
        self._generation = 0

    def clear(self) -> None:
        """Old in-flight reads cannot repopulate an invalidated cache."""
        with self._lock:
            self._generation += 1
            old_pending = list(self._pending.values())
            self._values.clear()
            self._pending.clear()
            self._retry_after.clear()
        for future in old_pending:
            future.cancel()

    def get(self, key: Hashable, loader: Callable[[], Any], *, wait=False):
        """Return a copy of a fresh value, or start/share its refresh.

        Live status uses wait=False and may omit cold/expired aggregates.
        Thread-pool HTTP routes can wait=True for the complete read.
        """
        created = False
        with self._lock:
            now = self.clock()
            cached = self._values.get(key)
            if cached and now - cached[0] < self.ttl_s:
                self._values.move_to_end(key)
                return copy.deepcopy(cached[1])
            pending = self._pending.get(key)
            generation = self._generation
            if pending is None:
                if self._retry_after.get(key, 0) > now:
                    if wait:
                        raise RuntimeError("dashboard aggregate temporarily unavailable")
                    return None
                if len(self._pending) >= self.max_entries:
                    if wait:
                        raise RuntimeError("dashboard aggregate queue is full")
                    return None
                pending = self.executor.submit(loader)
                self._pending[key] = pending
                created = True

        # Register outside the lock: add_done_callback executes inline
        # when a fast loader has already completed.
        if created:
            pending.add_done_callback(
                lambda future: self._publish(key, generation, future))
        if not wait:
            return None
        try:
            result = pending.result()
        except CancelledError:
            with self._lock:
                if generation != self._generation:
                    return None
            raise
        # result() can wake before callbacks finish; publish before returning.
        self._publish(key, generation, pending)
        with self._lock:
            if generation != self._generation:
                return None
        return copy.deepcopy(result)

    def _publish(self, key: Hashable, generation: int, future: Future):
        if future.cancelled():
            return
        try:
            value = future.result()
        except Exception as exc:
            with self._lock:
                if generation == self._generation and self._pending.get(key) is future:
                    self._pending.pop(key, None)
                    self._retry_after[key] = self.clock() + 5
                    while len(self._retry_after) > self.max_entries:
                        self._retry_after.pop(next(iter(self._retry_after)))
            log.warning("Dashboard aggregate refresh failed (%s)", type(exc).__name__)
            return
        with self._lock:
            if generation != self._generation or self._pending.get(key) is not future:
                return
            self._pending.pop(key, None)
            self._retry_after.pop(key, None)
            self._values[key] = (self.clock(), value)
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)
