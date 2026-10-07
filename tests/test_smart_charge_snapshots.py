"""Exact decision inputs survive migration, remain device-scoped, and expire."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import advisor_routes
import automation_tables
from energy_db import EnergyDB

NOW = 1_790_000_000


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_tables.time, "time", lambda: NOW)
    return EnergyDB(str(tmp_path / "energy.db"))


def plan(at=NOW):
    return {"decided_at": at, "mode": "test", "action": "off", "reason": "test"}


def test_exact_inputs_are_device_scoped_and_history_stays_lightweight(db):
    db.record_smart_charge_decision("A", plan(), False, input_snapshot={"capacity_wh": 30240})
    db.record_smart_charge_decision("B", plan(), False, input_snapshot={"capacity_wh": 3024})
    assert db.smart_charge_decision_inputs("A", NOW) == {"capacity_wh": 30240}
    assert db.smart_charge_decision_inputs("B", NOW) == {"capacity_wh": 3024}
    assert db.smart_charge_decision_inputs("C", NOW) is None
    assert "input_snapshot_json" not in db.list_smart_charge_decisions("A")[0]
    assert "input_snapshot_json" not in db.smart_charge_decision("A", NOW)
    assert db.smart_charge_decision("C", NOW) is None


def test_old_decision_is_accessible_after_latest_200_ticks(db):
    old = NOW - 2 * 86400
    db.record_smart_charge_decision("A", plan(old), False, input_snapshot={"old": True})
    with db._conn() as c:
        c.executemany("INSERT INTO smart_charge_decisions(decided_at,device_sn,mode,action) "
                      "VALUES(?,'A','test','off')", [(NOW - i,) for i in range(201)])
    assert all(row["decided_at"] != old for row in db.list_smart_charge_decisions("A", limit=200))
    assert db.smart_charge_decision("A", old)["decided_at"] == old
    assert db.smart_charge_decision_inputs("A", old) == {"old": True}


def test_migration_is_idempotent_and_preserves_unknown_legacy_inputs(db):
    db.record_smart_charge_decision("A", plan(), False)
    with db._conn() as c:
        c.execute("DROP INDEX idx_sc_input_retention")
        c.execute("ALTER TABLE smart_charge_decisions DROP COLUMN input_snapshot_json")
    for _ in range(2):
        migrated = EnergyDB(db.path)
        assert migrated.list_smart_charge_decisions("A")[0]["reason"] == "test"
        assert migrated.smart_charge_decision_inputs("A", NOW) is None


def test_retention_removes_old_inputs_but_preserves_decisions(db):
    old = NOW - 31 * 86400
    db.record_smart_charge_decision("A", plan(old), False, input_snapshot={"old": True})
    db.record_smart_charge_decision("A", plan(), False, input_snapshot={"new": True})
    assert db.smart_charge_decision_inputs("A", old) is None
    assert db.smart_charge_decision_inputs("A", NOW) == {"new": True}
    assert len(db.list_smart_charge_decisions("A")) == 2


@pytest.mark.parametrize("snapshot", [{"bad": float("nan")}, {"oversize": "x" * (129 * 1024)}])
def test_invalid_snapshot_does_not_erase_action_audit(db, snapshot):
    db.record_smart_charge_decision("A", plan(), True, input_snapshot=snapshot)
    assert db.list_smart_charge_decisions("A")[0]["executed"] is True
    assert db.smart_charge_decision_inputs("A", NOW) is None


async def test_advisor_reads_only_requested_devices_exact_inputs(db):
    db.record_smart_charge_decision("A", plan(), False, input_snapshot={"capacity_wh": 30240})
    db.record_smart_charge_decision("B", plan(), False, input_snapshot={"capacity_wh": 3024})
    helpers = advisor_routes.AdvisorHelpers(
        total_capacity_wh=lambda *a: 30240, capacity_hints=lambda *a: (5040, 5040),
        system_soc_pct=lambda pct, *a: pct,
    )
    query = advisor_routes._make_advisor_query_fn(SimpleNamespace(energy=db), helpers, "A")
    from datetime import datetime, timezone
    result = await query("query_decision_inputs", {
        "decided_at_iso": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
    })
    assert result["inputs_saved"]
    assert result["input_snapshot"]["capacity_wh"] == 30240
    legacy = await query("query_decision_inputs", {"decided_at_iso": "2020-01-01T00:00:00Z"})
    assert legacy["inputs_saved"] is False
    assert legacy["input_snapshot"] is None
    assert "error" in await query("query_decision_inputs", {"decided_at_iso": "invalid"})
