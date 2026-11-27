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
    # The stored path is the validated one: it ends on the reported sunrise level.
    assert result['scheduled_trajectory'][-1] == {
        'ts': 3600, 'soc': result['scheduled_sunrise_soc_pct']}


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


def test_grid_purchases_include_home_use_carried_while_charging(tmp_path, monkeypatch):
    """On a bypass device the AC input reading is the battery's share only; the
    grid also carries the home for every minute the unit is taking AC."""
    import time

    import cost
    db = EnergyDB(str(tmp_path/'energy.db'))
    start = int(time.time()) // 3600 * 3600 - 7200
    with db._conn() as c:
        # One hour charging at 900 W with the home drawing 480 W, then one hour
        # on battery alone. Wh per minute: 15 into the battery, 8 to the home.
        c.executemany('INSERT INTO samples(device_sn,bucket,input_wh,output_wh,ac_input_wh,'
                      'last_ac_input_w,last_output_w) VALUES(?,?,?,?,?,?,?)',
                      [('A', start + m*60, 15, 8, 15, 900, 480) for m in range(60)]
                      + [('A', start + 3600 + m*60, 0, 8, 0, 0, 480) for m in range(60)])
    rows = db.history('A', hours=3, bucket_s=3600)
    assert [(r['ac_input_wh'], r['grid_wh'], r['grid_w']) for r in rows] == [(900, 900, 900), (0, 0, 0)]
    monkeypatch.setattr(EnergyDB, 'grid_carries_home', staticmethod(lambda sn: sn == 'A'))
    rows = db.history('A', hours=3, bucket_s=3600)
    # The battery-side reading the forecaster learns from is untouched.
    assert [(r['ac_input_wh'], r['grid_wh'], r['grid_w']) for r in rows] == [(900, 1380, 1380), (0, 0, 0)]
    assert db.totals('A')['lifetime']['grid_wh'] == 1380
    assert db.readonly_reader().totals('A')['lifetime']['grid_wh'] == 1380
    assert sum(d['grid_kwh'] for d in db.daily_rollup('A', days=2)) == pytest.approx(1.38)
    money = cost.compute_savings(rows, {'type': 'flat', 'rate_per_kwh': 0.25, 'currency': 'USD'})
    # 0.96 kWh of home use valued at the rate, less everything bought from the grid.
    assert (money['grid_kwh'], money['grid_cost'], money['net_savings']) == (1.38, 0.34, pytest.approx(-0.10))


def _minute(ts, ac, home, soc=50):
    return dict(ts=ts, ac_input_w=ac, output_w=home, battery_pct=soc,
                soc_source_ts_min=ts, soc_source_ts_max=ts + 30)


def test_outlet_supply_is_learned_when_charging_plus_home_sits_at_one_level():
    import charge_rate
    now = 1_000_000 * 60
    # The battery takes what the home leaves of a 1,400 W supply.
    homes = [420, 450, 480, 520, 580, 700]
    rows = [_minute(now - (m + 2) * 60, 1400 - homes[m % 6] + (m % 3 - 1) * 5, homes[m % 6])
            for m in range(120)]
    supply = charge_rate.learn_outlet_supply(rows, now_ts=now)
    assert supply['source'] == 'fit' and supply['n_samples'] == 120
    assert supply['value'] == pytest.approx(1400, abs=5)
    assert supply['charge_cap_w'] == pytest.approx(980, abs=10)


def test_outlet_supply_is_not_learned_from_too_little_or_from_an_unlimited_charger():
    import charge_rate
    now = 1_000_000 * 60
    few = [_minute(now - (m + 2) * 60, 950, 450) for m in range(30)]
    assert charge_rate.learn_outlet_supply(few, now_ts=now)['value'] is None
    # A steady 950 W whatever the home draws: the outlet is not what limits it.
    homes = [300, 500, 700, 900]
    free = [_minute(now - (m + 2) * 60, 950, homes[m % 4]) for m in range(120)]
    assert charge_rate.learn_outlet_supply(free, now_ts=now)['value'] is None
    # Tapering near full and stale readings teach nothing either.
    full = [_minute(now - (m + 2) * 60, 950, 450, soc=95) for m in range(120)]
    assert charge_rate.learn_outlet_supply(full, now_ts=now)['n_samples'] == 0


def test_last_interval_is_trimmed_so_the_plan_does_not_overshoot_the_target():
    hours = [dict(ts=(h + 1) * 3600, duration_h=1, solar_w=0, load_w=500, output_w=500) for h in range(8)]
    common = dict(current_soc_pct=60, capacity_wh=30000, now=0, sunrise=8 * 3600, target=50, reserve=10,
                  power_w=1000, budget_seconds=8 * 3600, cost_plan={'type': 'flat', 'rate_per_kwh': 0.25},
                  tz_offset_seconds=0, charge_not_before_ts=None, unavailable=False, margin_seconds=0)
    data = dict(charge_efficiency=0.9, ac_input_excludes_load=True, ac_supply_limit_w=1400, forecast=hours)
    result = build_schedule(forecast=data, **common)
    # Without charging: 60% - 8 h x 500 W = 46.7%. Each charging hour adds 900 W x 0.9
    # stored plus 500 W not drained, 4.37 points, so one whole hour would land on 51.0%.
    assert 51.0 <= result['scheduled_sunrise_soc_pct'] <= 51.4
    assert result['plan_feasible']
    # A lower target needs less: the trim follows it instead of buying the whole hour.
    lower = build_schedule(forecast=data, **{**common, 'target': 48})
    lower_seconds = sum(i['end'] - i['start'] for i in lower['planned_intervals'])
    assert 0 < lower_seconds < 3600 and lower_seconds % 300 == 0
    assert 49.0 <= lower['scheduled_sunrise_soc_pct'] <= 49.4
