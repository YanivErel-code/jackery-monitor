"""Historical aggregates must not hold up fresh live telemetry."""
import asyncio
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def dashboard(isolated_data, monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    import server
    server = importlib.reload(server)
    server.state.device = server.DeviceInfo(
        name="Test", address="mock", device_sn="TEST", model_code=13,
        rssi=0, device_type="portable",
    )
    server.state.last_status = {"battery_percent": 73}
    return server


def test_first_live_snapshot_does_not_wait_for_energy_scan(dashboard, monkeypatch):
    started, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    threads = []

    def slow_totals(self, sn):
        threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=2)
        return {"today": {"output_wh": 123}}

    monkeypatch.setattr(type(dashboard.state.energy), "totals", slow_totals)
    monkeypatch.setattr(dashboard, "_decorate_totals_with_savings",
                        lambda totals, sn, **kwargs: totals)
    try:
        snapshot = dashboard.serialize_status()
        assert snapshot["telemetry"]["battery_percent"] == 73
        assert snapshot["energy"] is None
        assert started.wait(timeout=2)
        assert caller not in threads
    finally:
        release.set()


def test_readonly_reader_has_independent_lock_and_cannot_write(tmp_path):
    import energy_db
    db = energy_db.EnergyDB(str(tmp_path / "energy.db"))
    reader = db.readonly_reader()
    assert reader._lock is not db._lock
    assert reader.totals("TEST") == db.totals("TEST")
    import sqlite3
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with reader._conn() as conn:
            conn.execute("INSERT INTO devices (device_sn, name) VALUES ('TEST', 'Test')")


def test_readonly_wal_snapshot_does_not_block_telemetry_writer(tmp_path):
    import energy_db
    db = energy_db.EnergyDB(str(tmp_path / "energy.db"))
    reader = db.readonly_reader()

    def write_sample():
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO samples (device_sn,bucket,solar_wh) VALUES ('TEST',100,25)",
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with reader._conn() as conn:
            conn.execute("BEGIN")
            assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
            pool.submit(write_sample).result(timeout=2)
            # The reader stays on its coherent snapshot; writer has committed.
            assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    assert reader.totals("TEST")["lifetime"]["solar_wh"] == 25


def test_today_solar_query_does_not_scan_lifetime(tmp_path, monkeypatch):
    import energy_db
    db = energy_db.EnergyDB(str(tmp_path / "energy.db"))
    now = 1_780_000_000
    monkeypatch.setattr(energy_db.time, "time", lambda: now)
    today = energy_db._start_of_day(now)
    with db._conn() as conn:
        conn.executemany(
            "INSERT INTO samples (device_sn,bucket,solar_wh) VALUES (?,?,?)",
            [("TEST", today - 60, 9000), ("TEST", today, 40),
             ("TEST", today + 60, 60), ("OTHER", today, 500)],
        )
    assert db.today_solar_wh("TEST") == 100
    assert db.today_solar_wh("MISSING") == 0


def switch_account_to_home(server):
    server.state.account_generation += 1
    server.state.dashboard_cache.clear()
    server.state.device = None
    server.state.last_status = None
    server.state.last_cloud_meta = {"api_family": "home", "read_only": True}


def test_delayed_status_does_not_mix_old_telemetry_with_new_account(dashboard, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    dashboard.state.last_cloud_meta = {
        "selected_device_id": "A",
        "devices": [{"device_id": "B", "device_sn": "SECOND", "model_code": 13}],
        "devices_telemetry": {"SECOND": {"telemetry": {"battery_percent": 42}}},
    }

    def history(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return []

    monkeypatch.setattr(dashboard, "_view_history", history)
    with ThreadPoolExecutor(max_workers=1) as pool:
        rendering = pool.submit(dashboard.serialize_status, "B")
        assert entered.wait(timeout=2)
        switch_account_to_home(dashboard)
        release.set()
        result = rendering.result(timeout=2)
    assert result["telemetry"] is None
    assert result["device"] is None
    assert result["energy"] is None


@pytest.mark.parametrize("blocked_stage", ["history", "fit"])
async def test_forecast_does_not_persist_after_account_switch(
    dashboard, monkeypatch, blocked_stage,
):
    entered, release = threading.Event(), threading.Event()
    persisted = []
    monkeypatch.setattr(dashboard.device_location, "get", lambda: {"latitude": 37, "longitude": -122})
    monkeypatch.setattr(dashboard, "_capacity_hints", lambda sn: (None, None))
    monkeypatch.setattr(dashboard.weather_client, "fetch_irradiance",
                        AsyncMock(return_value={"hourly": []}))

    def history(self, *args, **kwargs):
        if blocked_stage == "history":
            entered.set()
            assert release.wait(timeout=2)
        return []

    def fit(**kwargs):
        if blocked_stage == "fit":
            entered.set()
            assert release.wait(timeout=2)
        return {"ready": True, "forecast": [{"ts": 100, "predicted_soc": 50}]}

    monkeypatch.setattr(type(dashboard.state.energy), "history", history)
    monkeypatch.setattr(dashboard.forecaster, "build_forecast", fit)
    monkeypatch.setattr(dashboard.state.energy, "record_forecast",
                        lambda *args, **kwargs: persisted.append(args))
    task = asyncio.create_task(dashboard._build_and_record_forecast("TEST"))
    try:
        assert await asyncio.to_thread(entered.wait, timeout=2)
        switch_account_to_home(dashboard)
    finally:
        release.set()
    result = await task
    assert not result.get("ready")
    assert persisted == []


def test_forecast_writer_rechecks_permission_inside_connection(tmp_path):
    import energy_db
    db = energy_db.EnergyDB(str(tmp_path / "energy.db"))
    result = db.record_forecast(
        "TEST", 100, [{"ts": 3600, "predicted_soc": 50}], should_record=lambda: False,
    )
    assert result == 0
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()[0] == 0
