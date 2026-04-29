"""Cost / savings: plan validation, TOU rate lookup, savings math."""
from __future__ import annotations

import importlib

import pytest


def _fresh_cost(tmp_path, monkeypatch):
    """Reload cost.py with COST_PATH pointing into tmp_path."""
    monkeypatch.setenv("JACKERY_COST_FILE", str(tmp_path / "cost.json"))
    import cost
    importlib.reload(cost)
    return cost


def test_get_plan_returns_default_when_unset(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = cost.get_plan()
    assert plan["type"] == "flat"
    assert plan["rate_per_kwh"] == 0.30


def test_set_plan_round_trip_flat(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    saved = cost.set_plan({"type": "flat", "rate_per_kwh": 0.42, "currency": "USD"})
    assert saved is not None
    got = cost.get_plan()
    assert got["rate_per_kwh"] == 0.42
    assert got["currency"] == "USD"


def test_set_plan_round_trip_tou(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {
        "type": "tou",
        "currency": "USD",
        "tou_rates": [
            {"start_hour": 16, "end_hour": 21, "rate": 0.61, "label": "peak"},
            {"start_hour": 0, "end_hour": 16, "rate": 0.31, "label": "off-peak"},
        ],
    }
    assert cost.set_plan(plan) is not None
    got = cost.get_plan()
    assert got["type"] == "tou"
    assert len(got["tou_rates"]) == 2


@pytest.mark.parametrize("bad_plan", [
    {"type": "wat"},
    {"type": "flat", "rate_per_kwh": -1},
    {"type": "flat", "rate_per_kwh": 99},
    {"type": "tou", "tou_rates": []},
    {"type": "tou", "tou_rates": [{"start_hour": -1, "end_hour": 5, "rate": 0.3}]},
    {"type": "tou", "tou_rates": [{"start_hour": 0, "end_hour": 24, "rate": 99}]},
    "not a dict",
])
def test_set_plan_rejects_invalid(tmp_path, monkeypatch, bad_plan):
    cost = _fresh_cost(tmp_path, monkeypatch)
    assert cost.set_plan(bad_plan) is None


def test_rate_at_flat(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {"type": "flat", "rate_per_kwh": 0.40, "currency": "USD"}
    assert cost.rate_at(plan, 1_700_000_000) == 0.40


def test_rate_at_tou_picks_correct_slot(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {
        "type": "tou", "currency": "USD",
        "tou_rates": [
            {"start_hour": 16, "end_hour": 21, "rate": 0.61, "label": "peak"},
            {"start_hour": 0, "end_hour": 16, "rate": 0.31, "label": "off-peak"},
            {"start_hour": 21, "end_hour": 24, "rate": 0.31, "label": "off-peak"},
        ],
    }
    # 17:00 UTC → peak slot
    ts_5pm_utc = 1_700_000_000 - (1_700_000_000 % 86400) + 17 * 3600
    assert cost.rate_at(plan, ts_5pm_utc, tz_offset_seconds=0) == 0.61
    # 10:00 UTC → off-peak slot
    ts_10am_utc = ts_5pm_utc - 7 * 3600
    assert cost.rate_at(plan, ts_10am_utc, tz_offset_seconds=0) == 0.31


def test_rate_at_tou_with_timezone_shift(tmp_path, monkeypatch):
    """A 17:00 PDT timestamp should pick peak when tz_offset=-7h.
    Without the offset shift, it'd pick the wrong slot."""
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {
        "type": "tou", "currency": "USD",
        "tou_rates": [
            {"start_hour": 16, "end_hour": 21, "rate": 0.61, "label": "peak"},
            {"start_hour": 0, "end_hour": 16, "rate": 0.31, "label": "off-peak"},
        ],
    }
    # UTC 00:00 of a chosen day = 17:00 of the previous day in PDT (-7).
    # So an UTC ts at 00:00 should land in PDT 17:00 = peak.
    ts_midnight_utc = 1_700_000_000 - (1_700_000_000 % 86400)
    assert cost.rate_at(plan, ts_midnight_utc, tz_offset_seconds=-7 * 3600) == 0.61


def test_rate_at_tou_wraparound_slot(tmp_path, monkeypatch):
    """A slot like 22-06 wraps midnight; both 23:00 and 03:00 should match."""
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {
        "type": "tou", "currency": "USD",
        "tou_rates": [
            {"start_hour": 22, "end_hour": 6, "rate": 0.20, "label": "super-off-peak"},
            {"start_hour": 6, "end_hour": 22, "rate": 0.50, "label": "day"},
        ],
    }
    base = 1_700_000_000 - (1_700_000_000 % 86400)
    assert cost.rate_at(plan, base + 23 * 3600, 0) == 0.20  # 23:00
    assert cost.rate_at(plan, base + 3 * 3600, 0) == 0.20   # 03:00
    assert cost.rate_at(plan, base + 12 * 3600, 0) == 0.50  # 12:00


def test_compute_savings_flat(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {"type": "flat", "rate_per_kwh": 0.30, "currency": "USD"}
    history = [
        # Hour 1: 2 kWh out, 1 kWh in (all solar)
        {"ts": 1_700_000_000, "solar_wh": 1000, "ac_input_wh": 0,
         "input_wh": 1000, "output_wh": 2000},
        # Hour 2: 1 kWh out, 0.5 solar + 0.5 from grid
        {"ts": 1_700_003_600, "solar_wh": 500, "ac_input_wh": 500,
         "input_wh": 1000, "output_wh": 1000},
    ]
    out = cost.compute_savings(history, plan)
    # saved (= output * rate):  3.0 kWh * $0.30 = $0.90
    # grid_cost (= ac * rate):  0.5 kWh * $0.30 = $0.15
    # net:                      $0.75
    assert out["solar_savings"] == 0.90
    assert out["grid_cost"] == 0.15
    assert out["net_savings"] == 0.75
    assert out["output_kwh"] == 3.0
    assert out["solar_kwh"] == 1.5
    assert out["grid_kwh"] == 0.5


def test_compute_savings_credits_battery_use_at_active_rate(tmp_path, monkeypatch):
    """1 kWh out at peak (4-9pm) saves more than 1 kWh out at off-peak —
    every Wh from the battery displaces a Wh you'd have bought at the
    rate active when you use it. Storage decouples charging time from
    consumption time, and the savings credit follows consumption."""
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {
        "type": "tou", "currency": "USD",
        "tou_rates": [
            {"start_hour": 16, "end_hour": 21, "rate": 0.60, "label": "peak"},
            {"start_hour": 0, "end_hour": 16, "rate": 0.30, "label": "off-peak"},
            {"start_hour": 21, "end_hour": 24, "rate": 0.30, "label": "off-peak"},
        ],
    }
    base = 1_700_000_000 - (1_700_000_000 % 86400)
    history = [
        # 17:00 UTC, 1 kWh out — peak rate ($0.60)
        {"ts": base + 17 * 3600, "solar_wh": 0, "ac_input_wh": 0,
         "input_wh": 0, "output_wh": 1000},
        # 10:00 UTC, 1 kWh out — off-peak rate ($0.30)
        {"ts": base + 10 * 3600, "solar_wh": 0, "ac_input_wh": 0,
         "input_wh": 0, "output_wh": 1000},
    ]
    out = cost.compute_savings(history, plan, tz_offset_seconds=0)
    assert out["solar_savings"] == 0.90  # 1 * 0.60 + 1 * 0.30


def test_compute_savings_handles_storage_drain(tmp_path, monkeypatch):
    """Drawing from previously-stored solar (no input today) still credits
    savings — bug repro for "6 kWh out + 1.7 kWh in only saved $0.52"."""
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {"type": "flat", "rate_per_kwh": 0.31, "currency": "USD"}
    history = [
        {"ts": 1_700_000_000, "output_wh": 6000, "input_wh": 1700,
         "ac_input_wh": 0, "solar_wh": 1700},
    ]
    out = cost.compute_savings(history, plan)
    # 6 kWh out * $0.31 = $1.86 saved, 0 grid, $1.86 net
    assert out["solar_savings"] == 1.86
    assert out["grid_cost"] == 0.0
    assert out["net_savings"] == 1.86


def test_compute_savings_falls_back_to_inferred_grid_for_old_rows(tmp_path, monkeypatch):
    """Pre-migration samples won't have ac_input_wh. Fall back to the
    inferred (input - solar) grid estimate so lifetime totals don't
    silently drop pre-migration grid charging."""
    cost = _fresh_cost(tmp_path, monkeypatch)
    plan = {"type": "flat", "rate_per_kwh": 0.30, "currency": "USD"}
    # Old-format row: ac_input_wh missing entirely.
    history = [
        {"ts": 1_700_000_000, "output_wh": 0, "input_wh": 1000, "solar_wh": 200},
    ]
    out = cost.compute_savings(history, plan)
    # input 1.0 - solar 0.2 = 0.8 kWh inferred grid * $0.30 = $0.24
    assert out["grid_cost"] == 0.24


def test_list_presets_returns_id_and_label(tmp_path, monkeypatch):
    cost = _fresh_cost(tmp_path, monkeypatch)
    presets = cost.list_presets()
    assert all("id" in p and "label" in p and "plan" in p for p in presets)
    pge = next((p for p in presets if p["id"] == "pge-ev2a"), None)
    assert pge is not None
    assert pge["plan"]["type"] == "tou"
