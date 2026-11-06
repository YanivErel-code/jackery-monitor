"""Same-day calibration must not learn from future, curtailed or stale hours."""
from copy import deepcopy

import pytest

from solar_calibration import fit_intraday_solar_scale

DAY = 1_790_000_000 // 86400 * 86400


def fixture(ratio=1.2):
    history, weather = [], []
    for hour in (8, 9):
        ts = DAY + hour * 3600
        history.append({'ts': ts, 'history_complete': True, 'solar_wh': 2000 * ratio,
                        'system_soc': 45, 'battery_pct': 50, 'ac_input_w': 0,
                        'soc_source_known_buckets': 60, 'soc_source_ts_min': ts + 30,
                        'soc_source_ts_max': ts + 3570})
        weather.append({'ts': ts + 3600, 'ghi_w_m2': 500, 'cloud_cover_pct': 90})
    return history, weather


def fitted(history, weather, now=DAY + 11 * 3600):
    return fit_intraday_solar_scale(history, weather, 4, {}, now_ts=now)


def test_consistent_measured_energy_only_applies_half_the_bias():
    history, weather = fixture()
    result = fitted(history, weather)
    assert result['scale'] == pytest.approx(1.1)
    assert result['eligible_hours'] == 2
    assert result['source'] == 'intraday_observations'
    # GHI already reflects clouds; cloud percentage must not attenuate it again.
    for w in weather:
        w['cloud_cover_pct'] = 0
    assert fitted(history, weather)['scale'] == result['scale']


@pytest.mark.parametrize(('field', 'value'), [
    ('history_complete', False), ('soc_fit_excluded', True), ('ac_input_w', 200),
    ('system_soc', 85), ('battery_pct', 90), ('soc_source_known_buckets', 20),
    ('soc_source_known_buckets', 59),
    ('soc_source_ts_min', DAY), ('solar_wh', float('nan')), ('ac_input_w', None),
])
def test_one_usable_hour_is_insufficient(field, value):
    history, weather = fixture()
    history[1][field] = value
    assert fitted(history, weather)['scale'] == 1


def test_future_and_other_day_evidence_cannot_change_current_projection():
    history, weather = fixture()
    history[1]['ts'] += 86400
    assert fitted(history, weather)['scale'] == 1
    history, weather = fixture()
    assert fitted(history, weather, now=DAY + 9 * 3600 + 1800)['scale'] == 1


def test_correction_expires_and_inconsistent_weather_does_not_create_a_trend():
    history, weather = fixture()
    assert fitted(history, weather, now=DAY + 13 * 3600)['reason'] == 'stale_observations'
    history[1]['solar_wh'] = 1200
    assert fitted(history, weather)['scale'] == 1


@pytest.mark.parametrize(('ratio', 'expected'), [(1.8, 1.2), (0.5, 0.8), (1, 1)])
def test_correction_is_bounded_and_does_not_adjust_an_unbiased_model(ratio, expected):
    history, weather = fixture(ratio)
    assert fitted(history, weather)['scale'] == expected


def test_duplicate_hour_cannot_supply_independent_evidence():
    history, weather = fixture()
    history.append(deepcopy(history[0]))
    assert fitted(history, weather)['reason'] == 'duplicate_history'
