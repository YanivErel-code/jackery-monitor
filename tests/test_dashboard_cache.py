from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

from dashboard_cache import DashboardCache


class QueueExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, loader):
        future = Future()
        self.tasks.append((future, loader))
        return future

    def finish(self, index=0):
        future, loader = self.tasks[index]
        if future.cancelled():
            return
        try:
            future.set_result(loader())
        except Exception as exc:
            future.set_exception(exc)


def test_cold_reads_share_one_worker_and_copy_results():
    pool = QueueExecutor()
    cache = DashboardCache(executor=pool)
    def loader():
        return {"today": {"solar_wh": 10}}
    assert cache.get("a", loader) is None
    assert cache.get("a", loader) is None
    assert len(pool.tasks) == 1
    pool.finish()
    first = cache.get("a", loader)
    first["today"]["solar_wh"] = 999
    assert cache.get("a", loader)["today"]["solar_wh"] == 10


def test_ttl_expiry_refreshes_without_treating_old_value_as_current():
    pool = QueueExecutor()
    now = [100]
    cache = DashboardCache(ttl_s=30, clock=lambda: now[0], executor=pool)
    cache.get("a", lambda: 10)
    pool.finish()
    now[0] = 129
    assert cache.get("a", lambda: 20) == 10
    now[0] = 130
    assert cache.get("a", lambda: 20) is None
    assert cache.get("a", lambda: 30) is None
    assert len(pool.tasks) == 2
    pool.finish(1)
    assert cache.get("a", lambda: 30) == 20


def test_invalidation_discards_old_account_or_tariff_read():
    pool = QueueExecutor()
    cache = DashboardCache(executor=pool)
    cache.get("a", lambda: "old")
    cache.clear()
    assert pool.tasks[0][0].cancelled()
    cache.get("a", lambda: "new")
    pool.finish(0)
    assert cache.get("a", lambda: "wrong") is None
    pool.finish(1)
    assert cache.get("a", lambda: "wrong") == "new"


def test_read_failure_is_retried_after_backoff():
    pool = QueueExecutor()
    now = [100]
    cache = DashboardCache(clock=lambda: now[0], executor=pool)

    def fail():
        raise OSError("read unavailable")

    cache.get("a", fail)
    pool.finish()
    assert cache.get("a", lambda: 10) is None
    assert len(pool.tasks) == 1
    now[0] = 105
    cache.get("a", lambda: 10)
    pool.finish(1)
    assert cache.get("a", lambda: 20) == 10


def test_waiting_reader_does_not_publish_after_invalidation():
    entered, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        cache = DashboardCache(executor=pool)

        def load():
            entered.set()
            assert release.wait(timeout=2)
            return "old"

        waiting = pool.submit(lambda: cache.get("a", load, wait=True))
        assert entered.wait(timeout=2)
        cache.clear()
        release.set()
        assert waiting.result(timeout=2) is None
        assert cache.get("a", lambda: "new", wait=True) == "new"


def test_completed_and_pending_keys_are_bounded():
    pool = QueueExecutor()
    cache = DashboardCache(executor=pool, max_entries=2)
    for key in ("a", "b", "c"):
        cache.get(key, lambda: 1)
    assert len(pool.tasks) == 2
    pool.finish(0)
    pool.finish(1)
    cache.get("c", lambda: 3)
    pool.finish(2)
    assert len(cache._values) == 2
    assert "a" not in cache._values
