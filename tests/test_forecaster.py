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


def test_solar_fit_zero_when_device_has_no_panels():
    # A device that's never produced solar — coefficient must be 0, not
    # the default. Otherwise we'd predict phantom solar generation for a
    # battery that has no panels connected.
    base = 1_700_000_000
    weather = [{"ts": base + i * 3600, "ghi_w_m2": 800, "cloud_cover_pct": 0}
               for i in range(20)]
    energy = [{"ts": w["ts"], "solar_w": 0, "output_w": 100, "battery_pct": 60}
              for w in weather]
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert k == 0.0
    assert n == 0


def test_solar_fit_zero_when_only_sensor_noise():
    # Some Jackery devices report a few watts on `ip - acip - cip` even
    # with nothing connected — sensor offset / rounding noise. Tiny
    # readings should NOT count as "this device has panels"; otherwise
    # we'd fabricate a forecast for a battery that has none.
    base = 1_700_000_000
    weather = [{"ts": base + i * 3600, "ghi_w_m2": 800, "cloud_cover_pct": 0}
               for i in range(20)]
    energy = [{"ts": w["ts"], "solar_w": 8, "output_w": 100, "battery_pct": 60}
              for w in weather]  # 8W of noise, well below threshold
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert k == 0.0
    assert n == 0


def test_solar_fit_runs_with_real_small_panel():
    # A 100W portable panel hits ~80W at peak — should still trigger the
    # regression rather than be dismissed as noise.
    base = 1_700_000_000
    weather = [{"ts": base + i * 3600, "ghi_w_m2": 800, "cloud_cover_pct": 0}
               for i in range(20)]
    energy = [{"ts": w["ts"], "solar_w": 80, "output_w": 100, "battery_pct": 60}
              for w in weather]
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert k > 0
    assert n >= forecaster.MIN_FIT_SAMPLES


def test_solar_fit_falls_back_when_too_few_pairs():
    # Only 1 daylight pair — under MIN_FIT_SAMPLES (2). Falls back to the
    # generic DEFAULT_SOLAR_COEFF rather than fitting on a single point.
    base = 1_700_000_000
    weather = [{"ts": base, "ghi_w_m2": 500, "cloud_cover_pct": 0}]
    energy = [{"ts": base, "solar_w": 300, "output_w": 100, "battery_pct": 50}]
    k, n = forecaster.fit_solar_coefficient(energy, weather)
    assert k == forecaster.DEFAULT_SOLAR_COEFF
    assert n == 1


def test_simulate_soc_charges_and_discharges():
    # 5kWh battery, 1h windows: +1000W net for 2h, then -1000W net for 2h.
    # Charging now applies CHARGE_EFFICIENCY (0.90); discharge does not.
    fc = [
        {"ts": 0, "solar_w": 1000, "load_w": 0},
        {"ts": 3600, "solar_w": 1000, "load_w": 0},
        {"ts": 7200, "solar_w": 0, "load_w": 1000},
        {"ts": 10800, "solar_w": 0, "load_w": 1000},
    ]
    out = forecaster.simulate_soc(starting_soc_pct=50.0,
                                  capacity_wh=5000, forecast_hours=fc)
    # +18% per charge hour (1000W * 0.9 / 5000), -20% per discharge hour
    assert out[0]["predicted_soc"] == 68.0
    assert out[1]["predicted_soc"] == 86.0
    assert out[2]["predicted_soc"] == 66.0
    assert out[3]["predicted_soc"] == 46.0


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


def test_expected_load_uses_idle_default_when_no_data():
    # Empty profile → forecast hour gets IDLE_LOAD_W, NOT a global mean.
    # This is the regression test for the 0%-predicted-vs-44%-actual bug.
    profile: dict[tuple[int, int], float] = {}
    load = forecaster.expected_load_w(profile, 1_700_000_000)
    assert load == forecaster.IDLE_LOAD_W


def test_expected_load_falls_back_to_neighbor_hour_not_global_mean():
    # User has heavy daytime activity (avg ~500W) but quiet evenings
    # (~50W). A missing 2am bucket should inherit from neighboring night
    # hours (1am, 3am) — NOT the global mean.
    from datetime import datetime
    profile = {
        # Daytime: high load
        (12, 0): 500.0, (13, 0): 600.0, (14, 0): 550.0,
        # Evening: quiet
        (1, 0): 40.0, (3, 0): 50.0,  # 2am missing
    }
    # Pick a weekday-Tuesday 2am for the lookup
    target = int(datetime(2024, 7, 2, 2, 0, 0).timestamp())
    load = forecaster.expected_load_w(profile, target)
    # Should land at 40 or 50 (one of the night neighbors), NOT 500-ish
    assert load in (40.0, 50.0), f"got {load} — leaked from daytime?"


def test_load_profile_recency_weight_for_variable_buckets():
    # A variable bucket (high IQR/median): old samples around 100W, recent
    # samples around 300W. Median is 200W; recency-weighted should land
    # closer to 300W since recent dominates 70/30.
    from datetime import datetime
    now = time.time()
    # 10 old samples (>3d ago) at 100W, 10 recent (<3d ago) at 300W —
    # all in the same (hour, weekday) bucket via being 7 days apart
    # plus offsets. Use exact-bucket placement for determinism.
    base_old = now - 10 * 86400
    base_new = now - 1 * 86400
    energy = []
    target_hour = datetime.fromtimestamp(base_old).hour
    for i in range(10):
        # Pick samples with the same hour-of-day as base_old: just keep
        # offset to nearest 24h.
        energy.append({"ts": base_old + i * 24 * 3600,
                       "output_w": 100, "solar_w": 0, "battery_pct": 80})
    for i in range(10):
        energy.append({"ts": base_new - i * 24 * 3600,
                       "output_w": 300, "solar_w": 0, "battery_pct": 80})
    profile = forecaster.fit_load_profile(energy, now_ts=now)
    # Find any bucket from this synthetic data
    matching = [v for k, v in profile.items() if k[0] == target_hour]
    assert matching, "no bucket created for synthetic data"
    val = matching[0]
    # Recency-weighted should pull above the plain median (200W)
    assert val > 200.0, f"recency weighting didn't kick in: {val}"


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
