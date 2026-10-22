from __future__ import annotations

import pytest

from charge_rate import DEFAULT_CHARGE_W, learn_charge_rate

NOW = 1_790_000_000 // 60 * 60


def readings(power=950, *, count=6, end=NOW, ac=True):
    return [
        {
            "ts": end - (count - index) * 60,
            "input_w": power,
            "ac_input_w": power if ac else 0,
            "solar_w": 0,
            "battery_pct": 40,
            "soc_source_ts_min": end - (count - index) * 60 + 10,
            "soc_source_ts_max": end - (count - index) * 60 + 40,
            "soc_source_known_buckets": 1,
        }
        for index in range(count)
    ]


def test_default_is_1000_until_six_consecutive_reliable_minutes_exist():
    assert DEFAULT_CHARGE_W == 1000
    for history in ([], readings(count=5)):
        result = learn_charge_rate(history, now_ts=NOW)
        assert result["value"] == 1000
        assert result["source"] == "default"


def test_learns_sustained_ac_rate_and_keeps_provenance():
    result = learn_charge_rate(readings(), now_ts=NOW)
    assert result["value"] == 950
    assert result["source"] == "fit"
    assert result["n_samples"] == 6
    assert result["last_sample_ts"] == NOW - 60
    assert len(result["samples"]) == 6


def test_new_sustained_rate_replaces_old_rate_in_either_direction():
    old = readings(1400, end=NOW - 3600)
    new = readings(900)
    assert learn_charge_rate([*old, *new], now_ts=NOW)["value"] == 900
    assert (
        learn_charge_rate([*new, *readings(1600, end=NOW + 600)], now_ts=NOW + 600)["value"] == 1600
    )


def test_a_single_power_spike_or_partial_minute_does_not_inflate_the_fit():
    for value in (5000, 200):
        history = readings()
        history[-1]["ac_input_w"] = value
        history[-1]["input_w"] = value
        assert learn_charge_rate(history, now_ts=NOW)["value"] == 950


@pytest.mark.parametrize(
    "field,value",
    [
        ("battery_pct", 96),
        ("battery_pct", None),
        ("solar_w", 500),
        ("ac_input_w", float("nan")),
        ("input_w", float("inf")),
        ("ac_input_w", True),
        ("soc_source_ts_min", NOW - 9999),
        ("soc_source_ts_max", NOW + 9999),
        ("soc_source_known_buckets", 0),
        ("measurement_suspect", True),
    ],
)
def test_invalid_tapering_solar_or_stale_readings_do_not_train(field, value):
    history = readings()
    for row in history:
        row[field] = value
    assert learn_charge_rate(history, now_ts=NOW)["source"] == "default"


def test_gaps_duplicate_minutes_old_or_future_samples_cannot_fill_the_window():
    history = readings(count=5)
    assert learn_charge_rate([*history, history[-1]], now_ts=NOW)["source"] == "default"
    assert learn_charge_rate(readings(end=NOW - 15 * 86400), now_ts=NOW)["source"] == "default"
    assert learn_charge_rate(readings(end=NOW + 3600), now_ts=NOW)["source"] == "default"
    history = readings()
    history[2]["ts"] -= 60
    assert learn_charge_rate(history, now_ts=NOW)["source"] == "default"


def test_broken_ac_classification_falls_back_only_to_dark_local_hours():
    night_end = NOW // 86400 * 86400 + 2 * 3600
    assert (
        learn_charge_rate(readings(ac=False, end=night_end), now_ts=night_end, tz_offset_seconds=0)[
            "value"
        ]
        == 950
    )
    day_end = night_end + 10 * 3600
    assert (
        learn_charge_rate(readings(ac=False, end=day_end), now_ts=day_end, tz_offset_seconds=0)[
            "source"
        ]
        == "default"
    )
    assert learn_charge_rate(readings(ac=False), now_ts=NOW)["source"] == "default"


def test_a_longer_session_retains_its_learned_rate_after_charging_stops():
    history = readings(count=30, end=NOW - 3600)
    history += readings(0, count=10)
    assert learn_charge_rate(history, now_ts=NOW)["value"] == 950
