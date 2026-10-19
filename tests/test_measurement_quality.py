from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from energy_db import EnergyDB
from frozen_telemetry import FrozenTelemetryDetector
from tests.test_frozen_telemetry import bridge, frozen_bridge
from tests.test_soc_provenance import monitor


@pytest.fixture
def quality_db(tmp_path):
    database = EnergyDB(str(tmp_path / "quality.db"))
    with database._conn() as connection:
        connection.executemany(
            "INSERT INTO samples(device_sn,bucket,output_wh,last_battery_pct) VALUES(?,?,?,?)",
            [("MAIN", 540, 8, 50), ("MAIN", 600, 8, 50), ("MAIN", 660, 8, 50),
             ("MAIN", 720, 8, 49), ("OTHER", 600, 2, 70)],
        )
        connection.execute(
            "INSERT INTO battery_packs(ts,parent_sn,pack_sn,soc_pct) VALUES(?,?,?,?)",
            (650, "MAIN", "PACK", 50),
        )
        connection.execute(
            "INSERT INTO smart_charge_decisions(decided_at,device_sn,mode,action) VALUES(?,?,?,?)",
            (650, "MAIN", "test", "on"),
        )
    return database


def test_quarantine_preserves_originals_other_devices_and_controller_audit(quality_db):
    result = quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    assert result["archived_rows"] == 3
    with quality_db._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM battery_packs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM smart_charge_decisions").fetchone()[0] == 1
        archive = connection.execute(
            "SELECT row_json FROM measurement_quarantine WHERE table_name='samples'"
        ).fetchall()
        assert sorted(json.loads(row[0])["output_wh"] for row in archive) == [8, 8]
    assert quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")["archived_rows"] == 0
    assert quality_db.totals("MAIN")["lifetime"]["output_wh"] == 16


def test_restore_is_selective_and_conflicts_roll_back(quality_db):
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    with quality_db._conn() as connection:
        connection.execute("INSERT INTO samples(device_sn,bucket,output_wh) VALUES(?,?,?)", ("MAIN", 780, 9))
    assert quality_db.restore_measurements("MAIN", 600)["restored_rows"] == 3
    assert quality_db.restore_measurements("MAIN", 600)["restored_rows"] == 0
    assert quality_db.totals("MAIN")["lifetime"]["output_wh"] == 41
    with quality_db._conn() as connection:
        connection.execute("UPDATE samples SET output_wh=99 WHERE device_sn='MAIN' AND bucket=600")
    with pytest.raises(ValueError, match="changed"):
        quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    assert quality_db.totals("MAIN")["lifetime"]["output_wh"] == 132


def test_quarantine_blocks_late_writers_and_does_not_integrate_the_unknown_gap(quality_db):
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    quality_db.record("MAIN", 600, 0, 600, 50)
    quality_db.record("MAIN", 660, 0, 600, 50)
    assert "MAIN" not in quality_db._last
    assert quality_db.record_battery_packs("MAIN", [{"deviceSn": "PACK", "rb": 50}], 650) == 0
    assert quality_db.record_forecast("MAIN", 650, [{"ts": 3600, "predicted_soc": 30}]) == 0
    quality_db.record("MAIN", 780, 0, 600, 49)
    quality_db.record("MAIN", 840, 0, 600, 49)
    with quality_db._conn() as connection:
        rows = connection.execute("SELECT bucket,output_wh FROM samples WHERE device_sn='MAIN' ORDER BY bucket").fetchall()
    assert [tuple(row) for row in rows] == [(540, 8), (720, 8), (840, 10)]


def test_unknown_actual_windows_and_invalid_forecasts_cannot_be_scored(quality_db):
    quality_db.record_forecast("MAIN", -7200, [{"ts": 0, "predicted_soc": 30}])
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    assert quality_db.system_soc_at("MAIN", 650) is None
    assert quality_db.system_soc_at("MAIN", 0) is None
    assert quality_db.prediction_accuracy("MAIN", max_age_days=100000) == []


def test_daily_cleanup_preserves_valid_sunset_forecast_and_leaves_sunrise_unknown(quality_db):
    with quality_db._conn() as connection:
        connection.execute(
            "INSERT INTO daily_solar_summary(date,device_sn,sunset_ts,sunrise_ts,"
            "predicted_sunset_soc_pct,actual_sunset_soc_pct,predicted_sunrise_soc_pct,"
            "actual_sunrise_soc_pct,updated_at,predictions_made_at,sunset_forecast_soc_pct,"
            "sunset_forecast_made_at,sunset_forecast_sunrise_ts,sunset_forecast_actual_soc_pct) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-18", "MAIN", -7200, 650, 60, 60, 50, 50, 680, 640, 30, -7200, 650, 50),
        )
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="frozen cloud")
    row = quality_db.list_daily_summary("MAIN", days=100000)[0]
    assert row["predicted_sunrise_soc_pct"] is None
    assert row["actual_sunrise_soc_pct"] is None
    assert row["sunset_forecast_actual_soc_pct"] is None
    assert row["sunset_forecast_soc_pct"] == 30
    assert row["actual_sunset_soc_pct"] == 60
    assert row["predicted_sunset_soc_pct"] == 60
    quality_db.backfill_daily_actuals("MAIN", days=100000)
    assert quality_db.list_daily_summary("MAIN", days=100000)[0]["actual_sunrise_soc_pct"] is None
    quality_db.restore_measurements("MAIN", 600)
    assert quality_db.list_daily_summary("MAIN", days=100000)[0]["actual_sunrise_soc_pct"] == 50


def test_confirmed_detector_state_survives_restart_and_atomic_save(tmp_path):
    path = tmp_path / "health.json"
    detector = FrozenTelemetryDetector(state_path=path)
    for minute in range(71):
        detector.observe("MAIN", signature=(88, 0, 551), discharge_w=551,
                         capacity_wh=30240, now=10000 + minute * 60)
    restored = FrozenTelemetryDetector(state_path=path)
    assert restored.health("MAIN", 14200)["suspect"]
    assert restored.health("MAIN", 14200)["unchanged_since"] == 10000
    assert path.stat().st_mode & 0o777 == 0o600
    restored.forget()
    assert not FrozenTelemetryDetector(state_path=path).health("MAIN", 14200)["suspect"]


def test_restore_does_not_bypass_an_overlapping_incident(quality_db):
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="first")
    quality_db.quarantine_measurements("MAIN", 660, 780, reason="second")
    with pytest.raises(ValueError, match="overlapping"):
        quality_db.restore_measurements("MAIN", 600)


def test_sql_failure_rolls_back_incident_archive_and_already_deleted_rows(quality_db):
    with quality_db._conn() as connection:
        connection.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON battery_packs "
                           "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        quality_db.quarantine_measurements("MAIN", 600, 720, reason="test")
    with quality_db._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM measurement_incidents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM measurement_quarantine").fetchone()[0] == 0


def test_backfill_cannot_race_quarantine_and_reinsert_a_stale_snapshot_actual(quality_db, monkeypatch):
    with quality_db._conn() as connection:
        connection.execute(
            "INSERT INTO daily_solar_summary(date,device_sn,sunrise_ts,updated_at,"
            "sunset_forecast_sunrise_ts) VALUES(?,?,?,?,?)", ("2026-10-18", "MAIN", 650, 680, 650),
        )
    quality_db.quarantine_measurements("MAIN", 600, 720, reason="test")
    monkeypatch.setattr(quality_db, "system_soc_at", lambda *args, **kwargs: 50)
    quality_db.backfill_daily_actuals("MAIN", days=100000)
    row = quality_db.list_daily_summary("MAIN", days=100000)[0]
    assert row["actual_sunrise_soc_pct"] is None
    assert row["sunset_forecast_actual_soc_pct"] is None


def test_selecting_a_device_does_not_erase_confirmed_measurement_health(bridge, monkeypatch):
    frozen_bridge(bridge, monkeypatch)
    bridge._clear_device_caches("main", forget_measurements=False)
    assert bridge.state.frozen_telemetry.health("main", 14200)["suspect"]


def test_unconfirmed_saved_evidence_cannot_bridge_a_restart_observation_gap(tmp_path):
    path = tmp_path / "health.json"
    detector = FrozenTelemetryDetector(state_path=path)
    for minute in range(31):
        detector.observe("MAIN", signature=(88, 0, 551), discharge_w=551,
                         capacity_wh=30240, now=10000 + minute * 60)
    detector.flush()
    restored = FrozenTelemetryDetector(state_path=path)
    restored.observe("MAIN", signature=(88, 0, 551), discharge_w=551, capacity_wh=30240, now=14000)
    assert not restored.health("MAIN", 14000)["suspect"]
    assert restored.health("MAIN", 14000)["unchanged_since"] == 14000


def test_corrupt_saved_state_requires_measurement_progress_instead_of_claiming_health(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("not valid json")
    detector = FrozenTelemetryDetector(state_path=path)
    assert detector.health("MAIN", 10000)["suspect"]
    detector.observe("MAIN", signature=(88, 0, 551), discharge_w=551, capacity_wh=30240, now=10000)
    assert detector.health("MAIN", 10000)["suspect"]
    detector.observe("MAIN", signature=(87, 0, 551), discharge_w=551, capacity_wh=30240, now=10060)
    assert not detector.health("MAIN", 10060)["suspect"]


def test_frozen_leadup_is_quarantined_and_live_caches_are_invalidated(monitor):
    monitor.state.device = monitor.DeviceInfo("Test", "mock", 0, 13, "MAIN", "portable")
    monitor.state.energy.record("MAIN", 540, 0, 551, 88)
    monitor.state.energy.record("MAIN", 600, 0, 551, 88)
    monitor.state.history.append({"ts": 600})
    monitor._param_fit_cache[("MAIN", "_history")] = (999, [])
    monitor.state.last_cloud_meta = {"devices_telemetry": {"MAIN": {
        "telemetry": {"measurement_suspect": True},
        "measurement_health": {"suspect": True, "unchanged_since": 600}}}}
    monitor._sync_measurement_quality(1000)
    assert not monitor.state.history
    assert ("MAIN", "_history") not in monitor._param_fit_cache
    assert monitor.state.energy.totals("MAIN")["lifetime"]["output_wh"] == 0
    with monitor.state.energy._conn() as connection:
        assert tuple(connection.execute("SELECT start_ts,end_ts FROM measurement_incidents").fetchone()) == (600, 1020)


async def test_suspect_pack_snapshots_are_not_persisted(monitor, monkeypatch):
    monitor.state.last_cloud_meta = {"devices_telemetry": {"MAIN": {
        "telemetry": {"battery_percent": 88, "measurement_suspect": True}}}}
    rpc = AsyncMock(return_value={"packs": [{"deviceSn": "PACK", "rb": 69}],
                                  "fetched_at": 999, "source": "http", "stale": False})
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.energy.latest_battery_packs("MAIN") == []
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"]


async def test_bridge_pack_rpc_marks_frozen_measurements_stale(bridge, monkeypatch):
    frozen_bridge(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "_annotate_pack_upgrades", AsyncMock())
    result = await bridge._get_battery_packs("main")
    assert result["stale"]
    assert result["measurement_suspect"]


async def test_legacy_rules_skip_suspect_soc_but_keep_healthy_other_device(monitor, monkeypatch):
    telemetry = {"battery_percent": 88, "measurement_suspect": True,
                 "input_power_w": 0, "output_power_w": 551}
    cloud = {"state": "connected", "age_s": 1, "frozen_devices": ["MAIN"],
             "devices": [{"device_sn": "MAIN", "model_code": 13}, {"device_sn": "OTHER", "model_code": 19}],
             "devices_telemetry": {"MAIN": {"telemetry": telemetry, "ts": 1000,
                                            "measurement_health": {"suspect": True, "unchanged_since": 0}},
                                    "OTHER": {"telemetry": {"battery_percent": 70,
                                                             "input_power_w": 0, "output_power_w": 10}, "ts": 1000}}}
    device = monitor.DeviceInfo("Test", "mock", 0, 13, "MAIN", "portable")
    monitor.state.device = device
    monitor.state.client = SimpleNamespace(is_connected=True, device_info=device,
                                           poll=AsyncMock(return_value={**telemetry, "_cloud": cloud}),
                                           last_status={"cloud": cloud})
    evaluate = AsyncMock(return_value=[])
    monkeypatch.setattr(monitor.state.automation, "evaluate", evaluate)
    monkeypatch.setattr(monitor, "_refresh_packs_for", AsyncMock())

    async def end_loop(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor.asyncio, "sleep", end_loop)
    with pytest.raises(asyncio.CancelledError):
        await monitor.poll_loop()
    evaluate.assert_awaited_once()
    assert evaluate.call_args.args[0] == {"OTHER": 70}
