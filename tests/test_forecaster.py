"""Forecaster: solar regression + load profile + SOC simulation."""
from __future__ import annotations

import time

import forecaster


def test_battery_capacity_known_and_unknown():
    assert forecaster.battery_capacity_wh(13) == 5040
    assert forecaster.battery_capacity_wh(22) == 5040
    assert forecaster.battery_capacity_wh(99) == forecaster.DEFAULT_BATTERY_CAPACITY_WH
    assert forecaster.battery_capacity_wh(None) == forecaster.DEFAULT_BATTERY_CAPACITY_WH


def test_solar_fit_recovers_known_coefficient():
    # Synthetic data: solar_w = 0.5 * ghi exactly. Fit should return ~0.5.
    base = 1_700_000_000
    weather = [
        {"ts": base + i * 3600, "ghi_w_m2": 200 + i * 10, "cloud_cover_pct": 0}
        for i in range(20)
    ]
    energy = [
        {"ts": w["ts"], "solar_w": int(0.5 * w["ghi_w_m2"]),
         "output_w": 100, "battery_pct": 80}
        for w in weather
    ]
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert n >= forecaster.MIN_FIT_SAMPLES
    assert abs(k - 0.5) < 0.05


def test_solar_fit_falls_back_when_too_few_pairs():
    # Only 3 daylight pairs — well under MIN_FIT_SAMPLES (8).
    base = 1_700_000_000
    weather = [{"ts": base + i * 3600, "ghi_w_m2": 500, "cloud_cover_pct": 0}
               for i in range(3)]
    energy = [{"ts": w["ts"], "solar_w": 300, "output_w": 100, "battery_pct": 50}
              for w in weather]
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert k == forecaster.DEFAULT_SOLAR_COEFF
    assert n == 3


def test_simulate_soc_charges_and_discharges():
    # 5kWh battery, 1h windows: +1000W net for 2h, then -1000W net for 2h.
    fc = [
        {"ts": 0, "solar_w": 1000, "load_w": 0},
        {"ts": 3600, "solar_w": 1000, "load_w": 0},
        {"ts": 7200, "solar_w": 0, "load_w": 1000},
        {"ts": 10800, "solar_w": 0, "load_w": 1000},
    ]
    out = forecaster.simulate_soc(starting_soc_pct=50.0,
                                  capacity_wh=5000, forecast_hours=fc)
    # +20% over 2h, then -20% over 2h → 50 → 70 → 70 (clamp irrelevant) → 50
    assert out[0]["predicted_soc"] == 70.0
    assert out[1]["predicted_soc"] == 90.0
    assert out[2]["predicted_soc"] == 70.0
    assert out[3]["predicted_soc"] == 50.0


def test_load_profile_clips_outliers():
    # 99 normal samples around 100W, plus one 4900W spike. All land in
    # the same (hour, weekday) bucket because the timestamps are exactly
    # 7 days apart. Without outlier clipping the bucket would predict
    # ~100W *or* 4900W depending on which sample lands in the median;
    # with the 95-percentile cap the 4900W gets clipped before bucketing
    # so the predicted load stays ~100W.
    from datetime import datetime
    base = 1_700_000_000
    energy = [
        {"ts": base + i * 7 * 24 * 3600, "output_w": 100, "solar_w": 0,
         "battery_pct": 80}
        for i in range(99)
    ]
    energy.append({"ts": base + 99 * 7 * 24 * 3600, "output_w": 4900,
                   "solar_w": 0, "battery_pct": 80})
    profile = forecaster.fit_load_profile(energy)
    d = datetime.fromtimestamp(base)
    key = (d.hour, 1 if d.weekday() >= 5 else 0)
    bucket = profile[key]
    assert bucket < 200, f"outlier leaked: bucket={bucket}"


def test_simulate_soc_clamps_at_bounds():
    fc = [{"ts": 0, "solar_w": 0, "load_w": 5000}]   # would drop SOC below 0
    out = forecaster.simulate_soc(starting_soc_pct=10.0,
                                  capacity_wh=5000, forecast_hours=fc)
    assert out[0]["predicted_soc"] == 0.0

    fc = [{"ts": 0, "solar_w": 5000, "load_w": 0}]   # would push above 100
    out = forecaster.simulate_soc(starting_soc_pct=95.0,
                                  capacity_wh=5000, forecast_hours=fc)
    assert out[0]["predicted_soc"] == 100.0


def test_build_forecast_glues_pieces_together():
    # 14 days of strong, consistent solar; ask for forecast starting "now"
    now = int(time.time())
    weather = []
    for i in range(-14 * 24, 24 * 5):  # past 14 days through next 5 days, hourly
        ts = now + i * 3600
        # 8am-4pm peaks at 800 W/m², zero at night
        hour_of_day = (ts // 3600) % 24
        ghi = 800 if 8 <= hour_of_day <= 16 else 0
        weather.append({"ts": ts, "ghi_w_m2": ghi, "cloud_cover_pct": 0})
    energy = [{"ts": w["ts"], "solar_w": int(0.4 * w["ghi_w_m2"]),
               "output_w": 100, "battery_pct": 60}
              for w in weather if w["ts"] < now]
    res = forecaster.build_forecast(
        energy_history=energy,
        weather_hourly=weather,
        starting_soc_pct=50.0,
        capacity_wh=5040,
        now_ts=now,
        horizon_hours=48,
    )
    assert res["capacity_wh"] == 5040
    assert len(res["forecast"]) > 0
    assert all("predicted_soc" in h for h in res["forecast"])
    # With strong solar > load, peak SOC should land above the start.
    peak = max(h["predicted_soc"] for h in res["forecast"])
    assert peak >= 50.0
