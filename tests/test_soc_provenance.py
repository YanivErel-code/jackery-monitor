"""Source receipt times must survive monitor polling and SQLite round trips."""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from energy_db import EnergyDB


def test_soc_source_does_not_advance_with_observation(tmp_path):
    db = EnergyDB(str(tmp_path / "energy.db"))
    db.record("MAIN", 600, 0, 525, 25, source_ts=590,
              soc_source_ts=580, soc_source="mqtt")
    db.record("MAIN", 660, 0, 525, 25, source_ts=655,
              soc_source_ts=580, soc_source="mqtt")
    with db._conn() as c:
        row = dict(c.execute("SELECT * FROM samples").fetchone())
    assert row["last_source_ts"] == 655
    assert row["last_soc_source_ts"] == 580
    assert row["last_soc_source"] == "mqtt"
    assert row["output_wh"] == pytest.approx(8.75)


def test_history_exposes_receipt_range_without_inventing_legacy_times(tmp_path, monkeypatch):
    import energy_db
    monkeypatch.setattr(energy_db.time, "time", lambda: 720)
    db = EnergyDB(str(tmp_path / "energy.db"))
    db.record("MAIN", 600, 0, 525, 25)
    db.record("MAIN", 660, 0, 525, 25, soc_source_ts=580, soc_source="mqtt")
    row = db.history("MAIN", hours=1, bucket_s=60)[0]
    assert row["soc_source_ts_min"] == 580
    assert row["soc_source_ts_max"] == 580
    assert row["soc_source_known_buckets"] == 1


@pytest.mark.parametrize("received", [None, "invalid", float("nan"), -1])
async def test_unknown_pack_receipt_is_not_fresh(monitor, monkeypatch, received):
    async def rpc(method, **kwargs):
        return {"packs": [{"deviceSn": "PACK", "rb": 20}],
                "fetched_at": received, "source": "mqtt", "stale": False}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.energy.latest_battery_packs("MAIN") == []
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"] is True


def test_pack_source_round_trip_and_legacy_unknown(tmp_path):
    db = EnergyDB(str(tmp_path / "energy.db"))
    pack = {"deviceSn": "PACK", "rb": 20, "ip": 0, "op": 100}
    db.record_battery_packs("MAIN", [pack], 660,
                            source_ts=640.5, source="http")
    row = db.latest_battery_packs("MAIN")[0]
    assert row["ts"] == 660
    assert row["source_ts"] == 640.5
    assert row["source"] == "http"
    db.record_battery_packs("LEGACY", [pack], 700)
    assert db.latest_battery_packs("LEGACY")[0]["source_ts"] is None


def test_migration_preserves_unknown_historical_source(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE battery_packs (ts INTEGER, parent_sn TEXT, "
              "pack_sn TEXT, device_order INTEGER, soc_pct REAL, input_w REAL, "
              "output_w REAL, internal_temp_c REAL, error_code INTEGER, "
              "PRIMARY KEY(ts,parent_sn,pack_sn))")
    c.execute("INSERT INTO battery_packs VALUES (100,'MAIN','PACK',0,20,0,100,NULL,0)")
    c.commit()
    c.close()
    for _ in range(2):
        db = EnergyDB(path)
        row = db.latest_battery_packs("MAIN")[0]
        assert row["source_ts"] is None
        assert row["source"] is None
        assert row["soc_pct"] == 20


@pytest.fixture()
def monitor(isolated_data, monkeypatch):
    monkeypatch.setenv("BACKEND", "mock")
    import energy_db
    monkeypatch.setattr(energy_db, "EnergyDB",
                        lambda: EnergyDB(str(isolated_data / "test-energy.db")))
    import server
    module = importlib.reload(server)
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    return module


async def test_repeated_pack_cache_is_not_a_new_snapshot(monitor, monkeypatch):
    async def rpc(method, **kwargs):
        return {"packs": [{"deviceSn": "PACK", "rb": 20}],
                "fetched_at": 995.5, "source": "mqtt", "stale": False}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    await monitor._refresh_packs_for("MAIN", 1400)
    rows = monitor.state.energy.latest_battery_packs("MAIN")
    assert rows[0]["ts"] == 1000
    assert rows[0]["source_ts"] == 995.5
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["fetched_at"] == 995.5


async def test_stale_pack_response_is_not_persisted(monitor, monkeypatch):
    monitor.state.battery_packs_by_sn["MAIN"] = [{"deviceSn": "PACK", "rb": 25}]
    async def rpc(method, **kwargs):
        return {"packs": [{"deviceSn": "PACK", "rb": 20}],
                "fetched_at": 500, "source": "mqtt", "stale": True}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.energy.latest_battery_packs("MAIN") == []
    assert monitor.state.battery_packs_by_sn["MAIN"][0]["rb"] == 25
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"] is True


async def test_empty_pack_blip_keeps_original_receipt(monitor, monkeypatch):
    monitor.state.battery_packs_by_sn["MAIN"] = [{"deviceSn": "PACK", "rb": 25}]
    monitor.state.battery_pack_meta_by_sn["MAIN"] = {
        "fetched_at": 900, "source": "mqtt", "stale": False,
    }
    async def rpc(method, **kwargs):
        return {"packs": [], "fetched_at": 990, "source": "http", "stale": False}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["fetched_at"] == 900
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"] is True
    assert monitor.state.energy.latest_battery_packs("MAIN") == []


async def test_empty_pack_response_cannot_retimestamp_db_reseed(monitor, monkeypatch):
    monitor.state.energy.record_battery_packs(
        "MAIN", [{"deviceSn": "PACK", "rb": 25}], 700,
        source_ts=690, source="mqtt")
    async def rpc(method, **kwargs):
        return {"packs": [], "fetched_at": 990, "source": "http", "stale": False}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.battery_packs_by_sn["MAIN"][0]["rb"] == 25
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["fetched_at"] == 690
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["stale"] is True


async def test_delayed_monitor_response_cannot_replace_newer_pack(monitor, monkeypatch):
    monitor.state.battery_packs_by_sn["MAIN"] = [{"deviceSn": "PACK", "rb": 25}]
    monitor.state.battery_pack_meta_by_sn["MAIN"] = {
        "fetched_at": 995, "source": "mqtt", "stale": False,
    }
    async def rpc(method, **kwargs):
        return {"packs": [{"deviceSn": "PACK", "rb": 20}],
                "fetched_at": 990, "source": "http", "stale": False}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    await monitor._refresh_packs_for("MAIN", 1000)
    assert monitor.state.battery_packs_by_sn["MAIN"][0]["rb"] == 25
    assert monitor.state.battery_pack_meta_by_sn["MAIN"]["fetched_at"] == 995


async def test_pack_api_preserves_cache_receipt_time(monitor, monkeypatch):
    monitor.state.battery_packs_by_sn["MAIN"] = [{"deviceSn": "PACK", "rb": 20}]
    monitor.state.last_packs_ts_by_sn["MAIN"] = 1000
    monitor.state.battery_pack_meta_by_sn["MAIN"] = {
        "fetched_at": 990, "source": "mqtt", "stale": False,
    }
    monkeypatch.setattr(monitor.time, "time", lambda: 1001)
    out = await monitor.api_devices_battery_packs("MAIN")
    assert out["fetched_at"] == 990
    assert out["age_s"] == 11
    assert out["source"] == "mqtt"


async def test_pack_api_failed_refresh_does_not_retimestamp(monitor, monkeypatch):
    async def rpc(method, **kwargs):
        return {"packs": [{"deviceSn": "PACK", "rb": 20}],
                "fetched_at": 500, "source": "mqtt", "stale": True,
                "error": "upstream unavailable"}
    monkeypatch.setattr(monitor.state.client, "_rpc", rpc, raising=False)
    monkeypatch.setattr(monitor.time, "time", lambda: 1000)
    out = await monitor.api_devices_battery_packs("MAIN", fresh=True)
    assert out["fetched_at"] == 500
    assert out["stale"] is True
    assert monitor.state.energy.latest_battery_packs("MAIN") == []
