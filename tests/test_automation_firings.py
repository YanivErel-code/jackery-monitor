"""automation_firings persistence + ON-interval pairing.

Covers the per-rule audit log shipped 2026-05-05: every successful
edge-triggered firing writes a row, the per-rule history endpoint
filters those rows, and the ON-interval pairing walks the firings
chronologically to compute total ON-time on a Kasa plug.
"""
from __future__ import annotations

import time

import pytest

from energy_db import EnergyDB


@pytest.fixture()
def db(tmp_path):
    return EnergyDB(str(tmp_path / "energy.db"))


def test_record_and_list_automation_firings(db):
    rid = db.record_automation_fire(
        rule_id="r1", rule_name="low-batt off",
        action="off", kasa_host="192.168.1.10",
        jackery_sn="SN-A", soc_at_fire=18.0,
        operator="<", threshold=20.0,
    )
    assert rid is not None
    rows = db.list_automation_firings(rule_id="r1", days=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["rule_id"] == "r1"
    assert r["rule_name"] == "low-batt off"
    assert r["action"] == "off"
    assert r["kasa_host"] == "192.168.1.10"
    assert r["jackery_sn"] == "SN-A"
    assert r["soc_at_fire"] == pytest.approx(18.0)
    assert r["operator"] == "<"
    assert r["threshold"] == pytest.approx(20.0)


def test_list_automation_firings_filters_by_host(db):
    db.record_automation_fire(
        rule_id="r1", rule_name="A", action="on",
        kasa_host="HOST-A", jackery_sn=None, soc_at_fire=None,
    )
    db.record_automation_fire(
        rule_id="r2", rule_name="B", action="off",
        kasa_host="HOST-B", jackery_sn=None, soc_at_fire=None,
    )
    a = db.list_automation_firings(kasa_host="HOST-A")
    b = db.list_automation_firings(kasa_host="HOST-B")
    assert {r["rule_id"] for r in a} == {"r1"}
    assert {r["rule_id"] for r in b} == {"r2"}


def test_record_automation_fire_rejects_invalid_input(db):
    assert db.record_automation_fire(
        rule_id="", rule_name="x", action="on",
        kasa_host="HOST", jackery_sn=None, soc_at_fire=None,
    ) is None
    assert db.record_automation_fire(
        rule_id="r1", rule_name="x", action="",
        kasa_host="HOST", jackery_sn=None, soc_at_fire=None,
    ) is None
    assert db.record_automation_fire(
        rule_id="r1", rule_name="x", action="on",
        kasa_host="", jackery_sn=None, soc_at_fire=None,
    ) is None


def test_on_intervals_pairs_consecutive_on_off(db):
    """Two simple ON->OFF cycles. Each pair becomes one interval."""
    base = int(time.time()) - 3600
    # ON at base, OFF at base+1800 (30min interval)
    db.record_automation_fire(
        rule_id="r1", rule_name="on-rule", action="on",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=15.0,
        fired_at=base,
    )
    db.record_automation_fire(
        rule_id="r2", rule_name="off-rule", action="off",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=80.0,
        fired_at=base + 1800,
    )
    intervals = db.automation_on_intervals("PLUG", days=1)
    assert len(intervals) == 1
    i = intervals[0]
    assert i["on_at"] == base
    assert i["off_at"] == base + 1800
    assert i["duration_s"] == 1800
    assert i["opened_by_rule_id"] == "r1"
    assert i["closed_by_rule_id"] == "r2"
    assert i["open"] is False


def test_on_intervals_ignores_repeated_on(db):
    """Rule fires ON twice without an OFF in between → single interval
    starting at the first ON. The second ON is dropped (state
    already on)."""
    base = int(time.time()) - 7200
    for ts in (base, base + 600, base + 1800):
        db.record_automation_fire(
            rule_id="r1", rule_name="on", action="on",
            kasa_host="PLUG", jackery_sn=None, soc_at_fire=10.0,
            fired_at=ts,
        )
    db.record_automation_fire(
        rule_id="r2", rule_name="off", action="off",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=80.0,
        fired_at=base + 3600,
    )
    intervals = db.automation_on_intervals("PLUG", days=1)
    assert len(intervals) == 1
    assert intervals[0]["on_at"] == base
    assert intervals[0]["off_at"] == base + 3600


def test_on_intervals_drops_off_without_on(db):
    """OFF firing with no preceding ON (e.g. plug was already off when
    the rule fired, or our log started mid-stream) is dropped silently."""
    base = int(time.time()) - 1800
    db.record_automation_fire(
        rule_id="r1", rule_name="off", action="off",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=80.0,
        fired_at=base,
    )
    intervals = db.automation_on_intervals("PLUG", days=1)
    assert intervals == []


def test_on_intervals_open_interval_for_currently_on_plug(db):
    """An ON firing with no closing OFF is reported as an open interval
    so the UI can show 'currently ON for Xh'."""
    on_at = int(time.time()) - 1800  # 30 min ago
    db.record_automation_fire(
        rule_id="r1", rule_name="on", action="on",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=10.0,
        fired_at=on_at,
    )
    intervals = db.automation_on_intervals("PLUG", days=1)
    assert len(intervals) == 1
    i = intervals[0]
    assert i["open"] is True
    assert i["off_at"] is None
    # Duration is roughly the elapsed time since on_at.
    assert 1700 <= i["duration_s"] <= 1900


def test_on_intervals_pairs_across_rules_on_same_plug(db):
    """Rule A turns ON; rule B turns OFF; both target the same plug.
    The interval is still ONE on-time stretch — that's what the
    user actually experienced on the plug, regardless of which
    complementary rule closed it."""
    base = int(time.time()) - 3600
    db.record_automation_fire(
        rule_id="ruleA", rule_name="A turns on", action="on",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=15.0,
        fired_at=base,
    )
    db.record_automation_fire(
        rule_id="ruleB", rule_name="B turns off", action="off",
        kasa_host="PLUG", jackery_sn=None, soc_at_fire=85.0,
        fired_at=base + 2400,
    )
    intervals = db.automation_on_intervals("PLUG", days=1)
    assert len(intervals) == 1
    i = intervals[0]
    assert i["opened_by_rule_id"] == "ruleA"
    assert i["closed_by_rule_id"] == "ruleB"
    assert i["duration_s"] == 2400
