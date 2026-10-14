"""A rolling forecast must never rewrite the sunset-to-sunrise baseline."""

import asyncio
import importlib
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from energy_db import EnergyDB


@pytest.fixture
def night():
    midnight = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    return {
        "date": datetime.fromtimestamp(midnight, timezone.utc).strftime("%Y-%m-%d"),
        "sunset": midnight + 18 * 3600,
        "sunrise": midnight + 31 * 3600,
    }


@pytest.fixture
def db(tmp_path):
    return EnergyDB(str(tmp_path / "energy.db"))


def write_prediction(db, night, prediction=30, **kwargs):
    db.upsert_daily_summary(
        device_sn="TEST-A", local_date=night["date"],
        sunset_ts=night["sunset"], sunrise_ts=night["sunrise"],
        predicted_sunrise_soc_pct=prediction, **kwargs,
    )


@pytest.mark.parametrize("offset,captured", [(-1, False), (0, True),
                                             (899, True), (901, False),
                                             (3 * 3600, False)])
def test_snapshot_only_captured_near_sunset(db, night, monkeypatch, offset, captured):
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + offset)
    write_prediction(db, night)
    row = db.list_daily_summary("TEST-A")[0]
    assert row["sunset_forecast_soc_pct"] == (30 if captured else None)
    assert row["sunset_forecast_made_at"] == (
        night["sunset"] + offset if captured else None)
    assert row["sunset_forecast_sunrise_ts"] == (night["sunrise"] if captured else None)


def test_snapshot_survives_rolling_updates_and_restart(db, night, monkeypatch):
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 60)
    write_prediction(db, night, prediction=0)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 300)
    write_prediction(db, night, prediction=45)
    reopened = EnergyDB(db.path)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunrise"] - 3600)
    shifted = {**night, "sunrise": night["sunrise"] + 3600}
    write_prediction(reopened, shifted, prediction=52)
    row = reopened.list_daily_summary("TEST-A")[0]
    assert row["predicted_sunrise_soc_pct"] == 52
    assert row["sunset_forecast_soc_pct"] == 0
    assert row["sunset_forecast_made_at"] == night["sunset"] + 60
    assert row["sunset_forecast_sunrise_ts"] == night["sunrise"]
    assert row["sunset_forecast_error_pp"] is None


def test_actual_backfill_does_not_capture_an_old_prediction(db, night, monkeypatch):
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] - 3600)
    write_prediction(db, night)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 60)
    db.upsert_daily_summary(
        device_sn="TEST-A", local_date=night["date"],
        sunset_ts=None, sunrise_ts=None, actual_sunset_soc_pct=60,
    )
    assert db.list_daily_summary("TEST-A")[0]["sunset_forecast_soc_pct"] is None
    assert db.sunset_forecast_due("TEST-A") is True
    assert db.sunset_forecast_due("TEST-B") is False


def test_snapshot_backfill_uses_frozen_target_and_full_observation_window(db, night, monkeypatch):
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 60)
    write_prediction(db, night)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 2 * 3600)
    write_prediction(db, {**night, "sunrise": night["sunrise"] + 3600}, prediction=40)
    for target, soc in [(night["sunrise"], 35), (night["sunrise"] + 3600, 42)]:
        for offset in [-60, 60]:
            db.record("TEST-A", target + offset, input_w=0, output_w=0,
                      battery_pct=soc, solar_w=0)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunrise"] + 60)
    db.backfill_daily_actuals("TEST-A")
    assert db.list_daily_summary("TEST-A")[0]["sunset_forecast_actual_soc_pct"] is None
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunrise"] + 2 * 3600)
    db.backfill_daily_actuals("TEST-A")
    row = db.list_daily_summary("TEST-A")[0]
    assert row["actual_sunrise_soc_pct"] == 42
    assert row["sunset_forecast_actual_soc_pct"] == 35
    assert row["sunset_forecast_error_pp"] == 5
    assert db.backfill_daily_actuals("TEST-A") == 0
    assert db.sunset_forecast_due("TEST-A") is False


def test_snapshot_actual_is_capacity_weighted_and_missing_telemetry_stays_unknown(
    db, night, monkeypatch,
):
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunset"] + 60)
    write_prediction(db, night)
    monkeypatch.setattr("energy_db.time.time", lambda: night["sunrise"] + 1800)
    db.backfill_daily_actuals("TEST-A", main_capacity_wh=1000, pack_capacity_wh=2000)
    assert db.list_daily_summary("TEST-A")[0]["sunset_forecast_actual_soc_pct"] is None
    for offset in [-60, 60]:
        db.record("TEST-A", night["sunrise"] + offset, input_w=0, output_w=0,
                  battery_pct=30, solar_w=0)
    db.record_battery_packs("TEST-A", [{"deviceSn": "PACK-A", "rb": 60}],
                           ts=night["sunrise"])
    db.backfill_daily_actuals("TEST-A", main_capacity_wh=1000, pack_capacity_wh=2000)
    row = db.list_daily_summary("TEST-A")[0]
    assert row["sunset_forecast_actual_soc_pct"] == 50
    assert row["sunset_forecast_error_pp"] == 20


def test_migration_does_not_fabricate_historical_snapshots(tmp_path, night):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE daily_solar_summary (
            date TEXT, device_sn TEXT, sunset_ts INTEGER, sunrise_ts INTEGER,
            predicted_sunset_soc_pct REAL, actual_sunset_soc_pct REAL,
            predicted_sunrise_soc_pct REAL, actual_sunrise_soc_pct REAL,
            updated_at INTEGER, predictions_made_at INTEGER,
            PRIMARY KEY (date, device_sn))""")
        connection.execute(
            "INSERT INTO daily_solar_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (night["date"], "TEST-A", night["sunset"], night["sunrise"],
             60, 61, 40, 42, night["sunrise"], night["sunrise"] - 300),
        )
    row = EnergyDB(path).list_daily_summary("TEST-A")[0]
    assert row["predicted_sunrise_soc_pct"] == 40
    assert row["sunset_forecast_soc_pct"] is None
    assert row["sunset_forecast_made_at"] is None
    assert row["sunset_forecast_error_pp"] is None


@pytest.fixture
def daily_server(isolated_data, monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    for name in ("crypto_util", "settings", "automation", "energy_db", "location"):
        importlib.reload(importlib.import_module(name))
    import server
    importlib.reload(server)
    monkeypatch.setattr(server, "_capacity_hints", lambda sn: (None, None))
    return server


@pytest.mark.parametrize("tz_offset", [0, -25200, 19800])
async def test_overnight_updates_stay_on_sunset_date(
    daily_server, night, monkeypatch, tz_offset,
):
    server = daily_server
    sunset = night["sunset"] - tz_offset
    sunrise = night["sunrise"] - tz_offset
    monkeypatch.setattr(server.time, "time", lambda: sunset - 3600)
    await server._update_daily_summary("TEST-A", {"forecast": [
        {"ts": sunset, "solar_w": 10, "predicted_soc": 60},
        {"ts": sunset + 3600, "solar_w": 0, "predicted_soc": 58},
        {"ts": sunrise, "solar_w": 0, "predicted_soc": 30},
        {"ts": sunrise + 3600, "solar_w": 10, "predicted_soc": 32},
    ]}, tz_offset)
    for now, prediction in [(sunset + 60, 31), (sunset + 7 * 3600, 45)]:
        monkeypatch.setattr(server.time, "time", lambda now=now: now)
        await server._update_daily_summary("TEST-A", {"forecast": [
            {"ts": now + 3600, "solar_w": 0, "predicted_soc": 55},
            {"ts": sunrise, "solar_w": 0, "predicted_soc": prediction},
            {"ts": sunrise + 3600, "solar_w": 10, "predicted_soc": 46},
        ]}, tz_offset)
    rows = server.state.energy.list_daily_summary("TEST-A")
    assert len(rows) == 1
    assert rows[0]["date"] == night["date"]
    assert rows[0]["predicted_sunrise_soc_pct"] == 45
    assert rows[0]["sunset_forecast_soc_pct"] == 31
    assert server.state.energy.list_daily_summary("TEST-B") == []


async def test_background_recorder_builds_only_due_sunset_snapshots(
    daily_server, night, monkeypatch,
):
    server = daily_server
    server.state.last_cloud_meta = {"devices": [
        {"device_sn": "TEST-A"}, {"device_sn": "TEST-B"}]}
    monkeypatch.setattr(server.device_location, "get", lambda: {"latitude": 0, "longitude": 0})
    monkeypatch.setattr(server.time, "time", lambda: night["sunset"] - 3600)
    write_prediction(server.state.energy, night)
    monkeypatch.setattr(server.time, "time", lambda: night["sunset"] + 60)
    build = AsyncMock(return_value={"ready": True, "forecast": [{}]})
    monkeypatch.setattr(server, "_build_and_record_forecast", build)
    await server._record_forecasts_for_all_devices(sunset_only=True)
    build.assert_awaited_once_with("TEST-A")


async def test_recorder_keeps_hourly_builds_with_five_minute_sunset_checks(
    daily_server, monkeypatch,
):
    server = daily_server
    clock = [0]
    calls = []
    monkeypatch.setattr(server.time, "time", lambda: clock[0])

    async def record(*, sunset_only=False):
        calls.append((clock[0], sunset_only))

    async def sleep(seconds):
        clock[0] += seconds
        if len(calls) >= 13:
            raise asyncio.CancelledError

    monkeypatch.setattr(server, "_record_forecasts_for_all_devices", record)
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await server.forecast_recorder_loop()
    regular = [ts for ts, sunset_only in calls if not sunset_only]
    assert regular == [90, 3690]
    assert len([ts for ts, sunset_only in calls if sunset_only]) == 11


@pytest.mark.parametrize("ready", [False, True])
async def test_dashboard_forecast_records_daily_summary_only_when_ready(
    daily_server, night, monkeypatch, ready,
):
    server = daily_server
    server.state.device = server.DeviceInfo(
        name="Test", address="mock", device_sn="TEST-A", model_code=13,
        rssi=0, device_type="portable",
    )
    server.state.last_status = {"battery_percent": 60}
    monkeypatch.setattr(server.device_location, "get", lambda: {"latitude": 0, "longitude": 0})
    monkeypatch.setattr(server.weather_client, "fetch_irradiance", AsyncMock(return_value={
        "hourly": [], "utc_offset_seconds": 0,
    }))
    monkeypatch.setattr(server.device_location, "get_tz_offset", lambda: 0)
    monkeypatch.setattr(server.time, "time", lambda: night["sunset"])
    monkeypatch.setattr(server.forecaster, "build_forecast", lambda **kwargs: {
        "ready": ready,
        "forecast": [
            {"ts": night["sunset"], "solar_w": 10, "predicted_soc": 60},
            {"ts": night["sunset"] + 3600, "solar_w": 0, "predicted_soc": 58},
            {"ts": night["sunrise"], "solar_w": 0, "predicted_soc": 30},
            {"ts": night["sunrise"] + 3600, "solar_w": 10, "predicted_soc": 32},
        ],
    })
    await server._build_and_record_forecast("TEST-A")
    rows = server.state.energy.list_daily_summary("TEST-A")
    assert len(rows) == (1 if ready else 0)
    if ready:
        assert rows[0]["sunset_forecast_soc_pct"] == 30
