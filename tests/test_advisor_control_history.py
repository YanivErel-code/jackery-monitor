"""Advisor evidence for independent control paths and simultaneous pack readings."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import advisor_routes
import claude_advisor
from energy_db import EnergyDB

NOW = 1_791_562_800
START = NOW - 86400
END = START + 3600


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_routes.time, "time", lambda: NOW)
    return EnergyDB(str(tmp_path / "energy.db"))


@pytest.fixture()
def helpers():
    return advisor_routes.AdvisorHelpers(
        total_capacity_wh=lambda *args: 30240,
        capacity_hints=lambda *args: (5040, 5040),
        system_soc_pct=lambda pct, *args: pct,
    )


def record_controls(db, device_sn="A", timestamp=START):
    db.record_smart_charge_decision(device_sn, {
        "decided_at": timestamp, "mode": "test", "action": "on",
        "reason": "charging now", "current_soc_pct": 11,
        "target_sunrise_soc_pct": 32, "sunrise_ts": NOW + 3600,
    }, False, input_snapshot={"private_snapshot": True})
    db.record_solar_charge_decision(device_sn, {
        "decided_at": timestamp, "mode": "active", "action": "skip",
        "reason": "already off; pack-balance window: pack spread 30pp ≥ 25pp trigger",
        "plug_state_before": "off", "solar_w": 0, "load_w": 530,
    }, False)
    db.record_automation_fire(
        rule_id="rescue-watchdog", rule_name="Rescue watchdog (live)",
        action="on", kasa_host="GRID-PLUG", jackery_sn=device_sn,
        soc_at_fire=5, operator="<=", threshold=5, fired_at=timestamp,
    )


def make_query(db, helpers, device_sn="A"):
    return advisor_routes._make_advisor_query_fn(
        SimpleNamespace(energy=db.readonly_reader()), helpers, device_sn,
    )


async def test_test_mode_decisions_do_not_hide_rescue_or_balance_response(db, helpers):
    record_controls(db)
    result = await make_query(db, helpers)("query_control_history", {
        "start_iso": iso(START), "end_iso": iso(END),
    })
    assert "error" not in result, result
    smart = result["smart_charge"]["rows"][0]
    solar = result["solar_charge"]["rows"][0]
    rescue = result["automation_firings"]["rows"][0]
    assert smart["mode"] == "test" and smart["executed"] is False
    assert smart["action"] == "on"
    assert "input_snapshot_json" not in smart
    assert solar["action"] == "skip" and solar["executed"] is False
    assert solar["plug_state_before"] == "off"
    assert "pack-balance window" in solar["reason"]
    assert rescue["rule_id"] == "rescue-watchdog"
    assert rescue["soc_at_fire"] == 5 and rescue["action"] == "on"
    assert rescue["fired_at"] == smart["decided_at"] == iso(START)
    assert "measured AC input" in result["note"]


async def test_control_history_filters_device_and_window_before_limiting(db, helpers):
    record_controls(db)
    record_controls(db, "B")
    record_controls(db, timestamp=START - 1)
    record_controls(db, timestamp=END)
    with db._conn() as connection:
        connection.executemany(
            "INSERT INTO solar_charge_decisions(decided_at,device_sn,mode,action) "
            "VALUES(?,'A','active','off')",
            [(END + offset,) for offset in range(1, 601)],
        )
    result = await make_query(db, helpers)("query_control_history", {
        "start_iso": iso(START), "end_iso": iso(END),
    })
    assert "error" not in result, result
    for key in ("smart_charge", "solar_charge", "automation_firings"):
        assert result[key]["row_count"] == result[key]["returned_rows"] == 1
        assert result[key]["truncated"] is False
    assert result["automation_firings"]["rows"][0]["jackery_sn"] == "A"


async def test_control_history_reports_total_and_truncation_per_stream(db, helpers, monkeypatch):
    monkeypatch.setattr(advisor_routes, "_MAX_TOOL_ROWS", 2)
    for offset in range(3):
        record_controls(db, timestamp=START + offset)
    result = await make_query(db, helpers)("query_control_history", {
        "start_iso": iso(START), "end_iso": iso(END),
    })
    assert "error" not in result, result
    for key in ("smart_charge", "solar_charge", "automation_firings"):
        assert result[key]["row_count"] == 3
        assert result[key]["returned_rows"] == len(result[key]["rows"]) == 2
        assert result[key]["truncated"] is True
    assert result["solar_charge"]["rows"][0]["decided_at"] == iso(START)


async def test_pack_history_keeps_simultaneous_snapshots_and_unknown_provenance(db, helpers, monkeypatch):
    monkeypatch.setattr(advisor_routes, "_MAX_TOOL_ROWS", 2)
    for offset, source in enumerate((None, "http", "mqtt")):
        db.record_battery_packs("A", [
            {"deviceSn": "PACK-LOW", "deviceOrder": 0, "rb": 13 - offset * 4,
             "op": 99, "ec": 0, "it": 878},
            {"deviceSn": "PACK-HIGH", "deviceOrder": 0, "rb": 35 - offset,
             "op": 112, "ec": 0, "it": 593},
        ], ts=START + offset * 300,
            source_ts=START + offset * 300 - 10 if source else None, source=source)
    db.record_battery_packs("B", [{"deviceSn": "OTHER", "rb": 90}], ts=START)
    result = await make_query(db, helpers)("query_battery_pack_history", {
        "start_iso": iso(START), "end_iso": iso(END),
    })
    assert "error" not in result, result
    assert result["row_count"] == 3 and result["returned_rows"] == 2
    assert result["truncated"] is True
    for snapshot in result["rows"]:
        assert len(snapshot["packs"]) == 2
        assert {pack["pack_sn"] for pack in snapshot["packs"]} == {"PACK-LOW", "PACK-HIGH"}
        assert all("internal_temp_c" not in pack for pack in snapshot["packs"])
    assert all(pack["source_ts"] is None and pack["source"] is None
               for pack in result["rows"][0]["packs"])
    assert result["rows"][1]["ts"] == iso(START + 300)
    assert all(pack["source"] == "http" for pack in result["rows"][1]["packs"])
    assert "receipt" in result["note"]


@pytest.mark.parametrize("tool", ["query_control_history", "query_battery_pack_history"])
@pytest.mark.parametrize("args", [
    {},
    {"start_iso": "invalid", "end_iso": iso(END)},
    {"start_iso": iso(END), "end_iso": iso(START)},
    {"start_iso": iso(START), "end_iso": iso(START)},
    {"start_iso": iso(START), "end_iso": iso(START + 46 * 86400)},
])
async def test_history_tools_reject_invalid_windows(db, helpers, tool, args):
    result = await make_query(db, helpers)(tool, args)
    assert "error" in result
    assert "unknown tool" not in result["error"]


async def test_starter_bundle_surfaces_independent_control_paths(db, helpers):
    record_controls(db)
    state = SimpleNamespace(
        energy=db.readonly_reader(), device=None, last_status=None,
        battery_packs_by_sn={},
    )
    bundle = await advisor_routes._build_advisor_bundle(state, helpers, "A")
    assert "recent_control_history" in bundle
    assert bundle["solar_charge_config"]["mode"]
    rendered = claude_advisor._format_starter_bundle(bundle)
    assert "Rescue watchdog (live)" in rendered
    assert "pack-balance window" in rendered
    assert '"executed":false' in rendered
    assert "query_control_history" in rendered
    assert "query_battery_pack_history" in rendered


def test_history_tools_are_exposed_to_both_advisor_providers():
    anthropic_names = {tool["name"] for tool in claude_advisor.QUERY_TOOLS}
    openai_names = {tool["name"] for tool in claude_advisor._to_openai_tools()}
    for name in ("query_control_history", "query_battery_pack_history"):
        assert name in anthropic_names
        assert name in openai_names
