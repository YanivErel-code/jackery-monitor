"""EnergyDB: schema migration + solar tracking + history round-trip."""
from __future__ import annotations

import sqlite3
import time

import pytest

from energy_db import EnergyDB


@pytest.fixture()
def db(tmp_path):
    return EnergyDB(str(tmp_path / "energy.db"))


def test_record_and_history_includes_solar(db):
    sn = "TEST-001"
    db.upsert_device(sn, "Test 5000", 13, "Explorer 5000 Plus")
    # Two readings 5 min apart, 600 W solar in, 200 W load.
    t0 = time.time() - 600
    db.record(sn, t0, input_w=600, output_w=200, battery_pct=80, solar_w=600)
    db.record(sn, t0 + 300, input_w=600, output_w=200, battery_pct=80, solar_w=600)
    rows = db.history(sn, hours=24, bucket_s=60)
    assert rows
    # Total solar Wh ≈ 600W * 5min = 50 Wh; allow some slack for trapezoid.
    total_solar = sum(r["solar_wh"] for r in rows)
    assert 40 < total_solar < 60
    assert any(r["solar_w"] > 0 for r in rows)


def test_record_works_without_solar_arg_for_back_compat(db):
    sn = "TEST-002"
    db.upsert_device(sn, "Old call site", 22, "Explorer 5000 Plus")
    t0 = time.time() - 600
    db.record(sn, t0, input_w=400, output_w=100, battery_pct=60)
    db.record(sn, t0 + 300, input_w=400, output_w=100, battery_pct=60)
    rows = db.history(sn, hours=24, bucket_s=60)
    assert rows
    # Solar defaulted to 0 → all solar columns zero.
    assert all(r["solar_wh"] == 0 for r in rows)
    assert all(r["solar_w"] == 0 for r in rows)


def test_capacity_override_round_trip(db):
    sn = "TEST-CAP"
    db.upsert_device(sn, "Test 5000+B5000", 13, "Explorer 5000 Plus")
    assert db.get_capacity_override(sn) is None
    assert db.set_capacity_override(sn, 10080) is True
    assert db.get_capacity_override(sn) == 10080
    # Override surfaces in list_devices()
    devs = db.list_devices()
    me = next(d for d in devs if d["device_sn"] == sn)
    assert me["capacity_wh_override"] == 10080
    # Clear via None
    assert db.set_capacity_override(sn, None) is True
    assert db.get_capacity_override(sn) is None


def test_capacity_override_rejects_out_of_range(db):
    sn = "TEST-CAP-RANGE"
    db.upsert_device(sn, "Tester", 13, "Explorer 5000 Plus")
    assert db.set_capacity_override(sn, 100) is False     # too small
    assert db.set_capacity_override(sn, 500_000) is False  # too large
    assert db.set_capacity_override(sn, "abc") is False    # not a number
    assert db.get_capacity_override(sn) is None


def test_migration_adds_columns_to_pre_v0_1_db(tmp_path):
    """A DB created without solar_wh/last_solar_w should ALTER on open."""
    path = str(tmp_path / "old.db")
    bucket_recent = int((time.time() - 600) // 60) * 60  # within last 24h
    # Simulate the old schema (no solar columns).
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE devices (device_sn TEXT PRIMARY KEY, name TEXT,
                            model_code INTEGER, model_name TEXT,
                            first_seen INTEGER, last_seen INTEGER);
      CREATE TABLE samples (device_sn TEXT NOT NULL, bucket INTEGER NOT NULL,
                            input_wh REAL NOT NULL DEFAULT 0,
                            output_wh REAL NOT NULL DEFAULT 0,
                            last_input_w INTEGER, last_output_w INTEGER,
                            last_battery_pct INTEGER,
                            sample_count INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (device_sn, bucket));
    """)
    con.execute("INSERT INTO samples VALUES (?, ?, 10, 5, 100, 50, 70, 1)",
                ("SN1", bucket_recent))
    con.commit()
    con.close()

    # Opening EnergyDB on this path should trigger the ALTER TABLE migration.
    db = EnergyDB(path)
    cols = sqlite3.connect(path).execute("PRAGMA table_info(samples)").fetchall()
    col_names = {c[1] for c in cols}
    assert "solar_wh" in col_names
    assert "last_solar_w" in col_names

    # Old row should still be there and history() should not crash on it.
    rows = db.history("SN1", hours=24, bucket_s=60)
    assert any(r["input_wh"] == 10 for r in rows)


def test_history_computes_plug_on_frac_from_decisions(db):
    # The forecaster nets EV charging out of the load profile using the
    # controller's plug-state log (NOT the flaky emeter-derived
    # diverted_wh). history() must surface, per bucket, the fraction of
    # decisions where the plug was ON.
    sn = "TEST-PLUG"
    db.upsert_device(sn, "Test 5000", 13, "Explorer 5000 Plus")
    base = (int(time.time()) - 7200) // 3600 * 3600 + 1800  # mid-bucket
    db.record(sn, base, input_w=0, output_w=1880, battery_pct=50, solar_w=0)
    db.record(sn, base + 60, input_w=0, output_w=1880, battery_pct=50, solar_w=0)
    # 3 ON decisions + 1 OFF, all inside the same 1h bucket -> frac 0.75.
    for i, state in enumerate(["on", "on", "on", "off"]):
        db.record_solar_charge_decision(
            sn,
            {"decided_at": base + i * 30, "mode": "active", "action": "skip",
             "plug_state_before": state, "current_soc_pct": 50},
            executed=False,
        )
    rows = db.history(sn, hours=24, bucket_s=3600)
    bkt = (base // 3600) * 3600
    row = next(r for r in rows if r["ts"] == bkt)
    assert row["solar_charge_plug_on_frac"] == pytest.approx(0.75)


def test_history_omits_plug_on_frac_when_no_decisions(db):
    # Buckets with no decision coverage must not carry the field — the
    # forecaster then falls back to raw output / recorded diverted_wh.
    sn = "TEST-NODEC"
    db.upsert_device(sn, "Test 5000", 13, "Explorer 5000 Plus")
    base = (int(time.time()) - 7200) // 3600 * 3600 + 1800
    db.record(sn, base, input_w=0, output_w=300, battery_pct=50, solar_w=0)
    db.record(sn, base + 60, input_w=0, output_w=300, battery_pct=50, solar_w=0)
    rows = db.history(sn, hours=24, bucket_s=3600)
    assert all("solar_charge_plug_on_frac" not in r for r in rows)
