"""User-confirmed battery-side AC input and a shared 1,750 W supply."""
import pytest

import forecaster
import smart_charge
from energy_db import EnergyDB
from smart_charge_schedule import build_schedule


def test_ac_efficiency_does_not_subtract_bypassed_load():
    history = [dict(ts=h*3600, system_soc=30+h*3, input_wh=1000,
                    output_wh=500, ac_input_wh=1000, ac_charge_complete=True)
               for h in range(6)]
    efficiency, count = forecaster.fit_charge_efficiency(
        history, 30000, ac_input_excludes_load=True)
    assert efficiency == pytest.approx(0.9)
    assert count == 5
    history[2]['ac_charge_complete'] = False
    assert forecaster.fit_charge_efficiency(
        history, 30000, ac_input_excludes_load=True, min_windows=1)[1] == 4


def test_ac_bypass_simulation_keeps_solar_separate_and_avoids_battery_load():
    hours = [dict(ts=3600, duration_h=1, solar_w=100, load_w=700,
                  ac_bypass_active=True, ac_battery_charge_w=1000)]
    trace = forecaster.simulate_soc(30, 10000, hours, charge_efficiency=0.9)
    assert trace[0]['predicted_soc'] == 39.9
    assert trace[0]['solar_w'] == 100
    assert trace[0]['net_w'] == 1100


@pytest.mark.parametrize('output,expected_soc', [(500, 39), (1500, 32.25), (1800, 12)])
def test_shared_supply_caps_battery_charging_and_blocks_excessive_load(output, expected_soc):
    data = dict(charge_efficiency=0.9, ac_input_excludes_load=True, ac_supply_limit_w=1750,
                forecast=[dict(ts=3600, duration_h=1, solar_w=0, load_w=output,
                               output_w=output)])
    result = build_schedule(forecast=data, current_soc_pct=30, capacity_wh=10000,
                            now=0, sunrise=3600, target=50, reserve=10, power_w=1000,
                            budget_seconds=3600, cost_plan={'type': 'flat', 'rate_per_kwh': 0.25},
                            tz_offset_seconds=0, charge_not_before_ts=None, unavailable=False,
                            margin_seconds=0)
    assert result['scheduled_sunrise_soc_pct'] == pytest.approx(expected_soc, abs=0.06)
    assert bool(result['planned_intervals']) == (output < 1750)


def test_history_identifies_complete_ac_hours_without_changing_energy(tmp_path):
    db = EnergyDB(str(tmp_path/'energy.db'))
    with db._conn() as c:
        c.executemany('INSERT INTO samples(device_sn,bucket,input_wh,output_wh,last_ac_input_w) '
                      'VALUES(?,?,?,?,?)', [('A', minute*60, 10, 5, 600) for minute in range(120)])
        c.execute("UPDATE samples SET last_ac_input_w=0 WHERE device_sn='A' AND bucket=3600")
    rows = db.history('A', hours=2, bucket_s=3600, until_ts=7200)
    assert rows[0]['ac_charge_complete']
    assert not rows[1]['ac_charge_complete']
    assert [r['input_wh'] for r in rows] == [600, 600]
    assert [r['output_wh'] for r in rows] == [300, 300]


def test_metering_config_is_per_device_and_does_not_change_other_defaults():
    cfg = smart_charge._validate_config(dict(ac_input_excludes_load=True, ac_supply_limit_w=1750))
    assert cfg['ac_input_excludes_load'] is True
    assert cfg['ac_supply_limit_w'] == 1750
    assert smart_charge._validate_config({})['ac_input_excludes_load'] is False


def test_existing_dashboard_saves_preserve_metering_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(smart_charge, 'CONFIG_PATH', str(tmp_path/'smart.json'))
    smart_charge.set_config(dict(mode='active', ac_input_excludes_load=True,
                                 ac_supply_limit_w=1750), 'A')
    saved = smart_charge.set_config(dict(mode='test', target_sunrise_soc_pct=30), 'A')
    assert saved['ac_input_excludes_load'] is True
    assert saved['ac_supply_limit_w'] == 1750
    assert smart_charge.get_config('B')['ac_input_excludes_load'] is False
