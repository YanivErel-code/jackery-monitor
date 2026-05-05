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


def test_load_profile_caps_runaway_buckets_against_overall_mean():
    # Sparse history regression test: if a single hour-of-day has a few
    # very high samples (e.g. user ran an EV charger one afternoon),
    # the bucket median must NOT claim that's the typical load — that
    # caused the 24h+ predictions to drain to 0% in the wild.
    from datetime import datetime
    base = 1_700_000_000  # arbitrary
    energy = []
    # 96 hours of 200W idle, scattered across all 24 hour-of-day buckets.
    for i in range(96):
        energy.append({"ts": base + i * 3600, "output_w": 200,
                       "solar_w": 0, "battery_pct": 80})
    # Now stack 4 high-load samples all at the same hour-of-day
    # (same weekday too). Without the per-bucket ceiling, the median
    # of [3000, 3000, 3000, 3000] = 3000W would dominate that hour.
    spike_hour_ts = base + 13 * 3600  # hour 13 in the original cycle
    for week in range(4):
        energy.append({"ts": spike_hour_ts + week * 7 * 24 * 3600,
                       "output_w": 3000, "solar_w": 0, "battery_pct": 50})
    profile = forecaster.fit_load_profile(energy)
    d = datetime.fromtimestamp(spike_hour_ts)
    key = (d.hour, 1 if d.weekday() >= 5 else 0)
    bucket = profile.get(key)
    assert bucket is not None, "spike hour bucket missing"
    # Overall mean is roughly (96*200 + 4*3000) / 100 ≈ 312W. Cap is
    # 2x that = ~624W. Bucket should be capped well below 3000W.
    assert bucket < 1000, f"runaway bucket leaked: {bucket}"


def test_load_profile_trimmed_median_kills_outlier_pair():
    # Sparse 11am bucket with 18 samples around 200W and 2 oven samples
    # at 1800W. Raw median over 20 sorted values = mean of indices 9, 10
    # — still 200W. But if the bucket only has 14 samples (12 at 200W
    # + 2 at 1800W) sorted, the raw median lands at index 7 = 200W,
    # already fine. The real risk is the *trim* itself shouldn't yank
    # the value upward when there are too few samples — verify n<5
    # gracefully falls back to raw median, and trim does drop outliers
    # in a fatter bucket.
    from datetime import datetime
    base = 1_700_000_000
    energy = []
    # 18 weekly-aligned samples at 200W on the same (hour, weekday) bucket.
    for i in range(18):
        energy.append({"ts": base + i * 7 * 24 * 3600, "output_w": 200,
                       "solar_w": 0, "battery_pct": 80})
    # 2 oven samples at 1800W on the SAME bucket (different weeks).
    for i in range(2):
        energy.append({"ts": base + (18 + i) * 7 * 24 * 3600,
                       "output_w": 1800, "solar_w": 0, "battery_pct": 80})
    profile = forecaster.fit_load_profile(energy)
    d = datetime.fromtimestamp(base)
    key = (d.hour, 1 if d.weekday() >= 5 else 0)
    bucket = profile.get(key)
    assert bucket is not None
    # With 20 samples and 10% trim, k=2 → drop 2 lowest + 2 highest =
    # drop both ovens. Trimmed median of remaining 16 samples at 200W
    # = 200W. Without trim, the sample-cap at p95 would have already
    # squashed the ovens, but the trim adds a second line of defence
    # for buckets where the spike isn't above global p95 (e.g. mid-tier
    # ovens in an active household).
    assert bucket < 400, f"trimmed median didn't reject ovens: {bucket}"


def test_load_profile_uses_global_p95_as_bucket_ceiling():
    # Replaces the old `2 * overall_mean` ceiling. With many low samples
    # and a few high ones, the new ceiling = global p95, not 2*mean.
    # Build a history where p95 ≠ 2*mean and assert ceiling tracks p95.
    from datetime import datetime
    base = 1_700_000_000
    energy = []
    # 90 weekday samples at 100W spread across 24 different (hour,wkd)
    # buckets so each bucket has < 5 samples (so no recency weighting).
    for i in range(90):
        energy.append({"ts": base + i * 3600, "output_w": 100,
                       "solar_w": 0, "battery_pct": 80})
    # 10 high samples at 1500W all stacked into one bucket (hour 14
    # weekday) at exact 7-day intervals so they share a (hour, weekday)
    # bucket key.
    spike_ts0 = base + 14 * 3600
    for week in range(10):
        energy.append({"ts": spike_ts0 + week * 7 * 24 * 3600,
                       "output_w": 1500, "solar_w": 0, "battery_pct": 50})
    profile = forecaster.fit_load_profile(energy)
    # Overall mean ≈ (90*100 + 10*1500) / 100 = 240W. Old ceiling = 480W.
    # p95 over 100 sorted samples: index 95 = 1500W → ceiling = 1500W.
    # The hour 14 bucket should land near 1500W, NOT clamped down to
    # ~480W like the old 2*mean rule did.
    d = datetime.fromtimestamp(spike_ts0)
    key = (d.hour, 1 if d.weekday() >= 5 else 0)
    bucket = profile.get(key)
    assert bucket is not None
    # Bucket median is 1500 (all 10 samples are 1500). Trim drops 1
    # from each end → still 1500. min(1500, ceiling=1500) = 1500.
    assert 1400 <= bucket <= 1600, (
        f"bucket {bucket} — expected ~1500 (p95 ceiling), "
        "NOT old 2*mean cap of ~480"
    )


def test_inverter_overhead_pct_unbiased_under_quantization():
    # Synthetic: true overhead = 10%. We quantize the SOC reads to 1pp
    # but use 2pp drops (per the new MIN_SOC_DROP gate) so the
    # quantization-induced jitter on each end is ±0.5pp / 2pp = ±25%
    # noise on the implied drain. With per-sample clamp-to-zero (the
    # OLD behaviour), every "too-efficient" window became 0 while
    # "too-lossy" windows kept their full ratio → median biased high.
    # New behaviour: collect signed ratios, take median, and the
    # symmetric noise should cancel out.
    import random
    rng = random.Random(42)
    capacity = 30000
    # True overhead 10%: out_w = 545W → drain = 600W → 2pp/h on 30000Wh.
    # Add ±0.5pp jitter to each soc read to simulate cloud quantization.
    history = []
    base = 1_700_000_000
    for i in range(40):
        ts0 = base + i * 3600 * 3
        # True SOC = 90 - 2*i / 90 - 2*i - 2; jitter integer-rounded.
        soc0_true = 90 - 2 * i
        soc1_true = soc0_true - 2
        soc0 = soc0_true + rng.choice([-1, 0, 0, 1])  # ±1pp on each end
        soc1 = soc1_true + rng.choice([-1, 0, 0, 1])
        # Reject self-inverted windows just like the production gate would.
        if soc0 - soc1 < 2:
            continue
        history.append({"ts": ts0, "battery_pct": soc0, "output_wh": 545,
                        "solar_wh": 0, "ac_input_wh": 0})
        history.append({"ts": ts0 + 3600, "battery_pct": soc1, "output_wh": 545,
                        "solar_wh": 0, "ac_input_wh": 0})
    pct, n = forecaster.fit_inverter_overhead_pct(history, capacity_wh=capacity)
    assert n >= 5
    # Without bias, median over many noisy windows should fall within
    # ±5pp of the truth; the asymmetric clamp would push this to
    # 0.20-0.40 (which is what the live data showed: 0.378).
    assert 0.05 <= pct <= 0.18, (
        f"got {pct}; bias-free median should be near 0.10"
    )


def test_solar_cap_uses_14d_window_not_recent_48h():
    """Regression test for the cap-window bug the daily advisor flagged
    on 2026-05-03: predictions made 5/1 for 5/3 saturated at the
    ac_charge floor (35%) because the recent-48h window only contained
    cloudy days, even though the array had done 3kW within the prior
    fortnight and Open-Meteo correctly forecast bright sun for 5/3.

    Setup: a history where 8 days ago had a clear-sky 3000W peak, but
    the last 48h has only 200W cloudy peaks. Forecast targets a future
    hour with high GHI. The cap should be derived from the 8-day-ago
    peak (x SOLAR_RECENT_CAP_MULT), not the 48h-ago low - so a high
    k*GHI prediction lands without being clamped to a fraction of the
    array's real capability.
    """
    now = int(time.time())
    history = []
    # 16 clean discharge windows so build_forecast doesn't bail with
    # ready=False. Spread between 14 and 5 days ago so they're inside
    # the new 14-day cap window.
    for i in range(16):
        ts = now - (5 * 86400) - i * 3 * 3600
        history.append({
            "ts": ts, "battery_pct": 90,
            "output_w": 600, "output_wh": 600,
            "solar_w": 0, "solar_wh": 0,
            "ac_input_w": 0, "ac_input_wh": 0,
            "input_w": 0, "input_wh": 0,
        })
        history.append({
            "ts": ts + 3600, "battery_pct": 87,
            "output_w": 600, "output_wh": 600,
            "solar_w": 0, "solar_wh": 0,
            "ac_input_w": 0, "ac_input_wh": 0,
            "input_w": 0, "input_wh": 0,
        })

    # The clear-sky day: 8 days ago, peak 3000W with high GHI to fit a
    # solid k coefficient.
    sunny_day_anchor = now - 8 * 86400
    for h in range(8, 17):  # 09:00-16:00 local, 8 hourly buckets
        ghi = 800.0 if 11 <= h <= 14 else 400.0  # noon-ish peak
        solar = 3000.0 if 11 <= h <= 14 else 1500.0
        ts = sunny_day_anchor + h * 3600
        history.append({
            "ts": ts, "battery_pct": 80,
            "output_w": 0, "output_wh": 0,
            "solar_w": solar, "solar_wh": solar,
            "ac_input_w": 0, "ac_input_wh": 0,
            "input_w": solar, "input_wh": solar,
        })

    # Recent 48h: only cloudy / low-solar samples, peak ~200W.
    for h in range(48):
        ts = now - (48 - h) * 3600
        history.append({
            "ts": ts, "battery_pct": 60,
            "output_w": 200, "output_wh": 200,
            "solar_w": 200 if 12 <= h % 24 <= 14 else 50,
            "solar_wh": 200 if 12 <= h % 24 <= 14 else 50,
            "ac_input_w": 0, "ac_input_wh": 0,
            "input_w": 0, "input_wh": 0,
        })

    # Weather: also include the past samples so fit_solar_coefficient has
    # GHI-paired samples for the sunny day, then a future window with
    # high GHI for the prediction.
    weather = []
    # Past weather, hour-aligned to match history timestamps.
    for h in range(8, 17):
        weather.append({
            "ts": sunny_day_anchor + h * 3600,
            "ghi_w_m2": 800.0 if 11 <= h <= 14 else 400.0,
            "cloud_cover_pct": 0,
        })
    # Future weather: high GHI predicted for tomorrow.
    for h in range(24):
        ghi = 900.0 if 11 <= h <= 14 else (400.0 if 8 <= h <= 17 else 0)
        weather.append({
            "ts": now + h * 3600,
            "ghi_w_m2": ghi,
            "cloud_cover_pct": 0,
        })

    res = forecaster.build_forecast(
        energy_history=history,
        weather_hourly=weather,
        starting_soc_pct=60.0,
        capacity_wh=5040,
        now_ts=now,
        horizon_hours=24,
    )
    assert res.get("ready") is True, (
        f"forecast not ready: {res.get('readiness')}"
    )

    # The cap should be derived from the 8-day-ago peak (3000W),
    # not the recent 48h peak (200W). At SOLAR_RECENT_CAP_MULT=2.0
    # the cap is ≥ 6000W — well above k*GHI for the future bright
    # hours, so they should NOT be clamped.
    assert res["solar_recent_peak_w"] >= 2900, (
        f"recent_peak should reflect the 8-day-ago sunny day, "
        f"got {res['solar_recent_peak_w']}"
    )

    # Future noon hours should have non-trivial solar_w (at least
    # 1000W — the regression's prediction unencumbered by a too-tight
    # cap). If the cap were still based on the recent 48h (200W * 1.5
    # = 300W), every future hour would be clamped to 300W and this
    # assertion would fail.
    bright_hours = [h for h in res["forecast"] if h["solar_w"] > 1000]
    assert len(bright_hours) >= 3, (
        f"expected several bright forecast hours; only "
        f"{len(bright_hours)} hours over 1000W. "
        f"solar_cap_w={res.get('solar_cap_w')}, "
        f"forecast solar_w values: {[h['solar_w'] for h in res['forecast']]}"
    )


def test_build_forecast_emits_diagnostic_sources():
    # Verify the new diagnostic keys are present and labelled correctly.
    # With enough clean discharge windows, both fits should run →
    # 'fit'. With no charge history, charge_efficiency falls back →
    # 'default'.
    now = int(time.time())
    history = []
    # Discharge-only history: 15 windows of 2pp drops, 545W out_w.
    for i in range(15):
        ts = now - 60 * 3600 + i * 3600 * 3
        history.append({"ts": ts, "battery_pct": 90 - 2 * i, "output_w": 545,
                        "output_wh": 545, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0,
                        "input_wh": 0, "input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 88 - 2 * i, "output_w": 545,
                        "output_wh": 545, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0,
                        "input_wh": 0, "input_w": 0})
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=50.0, capacity_wh=30000, now_ts=now,
        horizon_hours=24,
    )
    assert res.get("ready") is True
    # New diagnostic keys.
    assert "output_w_p95" in res
    assert isinstance(res["output_w_p95"], (int, float))
    assert res["output_w_p95"] > 0
    assert "inverter_overhead_source" in res
    assert res["inverter_overhead_source"] == "fit"
    assert "charge_efficiency_source" in res
    # No charging windows in this history → fall back to default.
    assert res["charge_efficiency_source"] == "default"


def test_expected_load_uses_idle_default_when_no_data():
    # Empty profile → forecast hour gets IDLE_LOAD_W (the per-hour
    # fallback) inflated by INVERTER_OVERHEAD_PCT (the heat-loss share
    # in DC→AC conversion that doesn't show up in `op` but still drains
    # the battery). Multiplicative model: the heavier the load, the
    # more overhead.
    profile: dict[tuple[int, int], float] = {}
    load = forecaster.expected_load_w(profile, 1_700_000_000)
    expected = forecaster.IDLE_LOAD_W * (1.0 + forecaster.INVERTER_OVERHEAD_PCT)
    assert load == expected


def test_expected_load_falls_back_to_neighbor_hour_not_global_mean():
    # User has heavy daytime activity (avg ~500W) but quiet evenings
    # (~50W). A missing 2am bucket should inherit from neighboring night
    # hours (1am, 3am) — NOT the global mean. The overhead percentage
    # is applied uniformly so it doesn't affect WHICH bucket is
    # selected, only the absolute value.
    from datetime import datetime
    profile = {
        # Daytime: high load
        (12, 0): 500.0, (13, 0): 600.0, (14, 0): 550.0,
        # Evening: quiet
        (1, 0): 40.0, (3, 0): 50.0,  # 2am missing
    }
    target = int(datetime(2024, 7, 2, 2, 0, 0).timestamp())
    load = forecaster.expected_load_w(profile, target)
    pct = forecaster.INVERTER_OVERHEAD_PCT
    expected = {40.0 * (1.0 + pct), 50.0 * (1.0 + pct)}
    assert load in expected, f"got {load} — leaked from daytime?"


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
    # 14 days of strong, consistent solar; ask for forecast starting "now".
    # IDLE_OVERHEAD_W is added to every load lookup, so the synthetic
    # panel needs to comfortably exceed it during the day for the
    # smoke-test charge assertion to hold. With OVERHEAD=200 + 100W
    # output_w = ~300W effective daytime load, a 2.0 W per W/m²
    # coefficient (~1600W peak at ghi=800) clears it with wide margin.
    # The synthetic battery_pct walks down at night so the new
    # forecast_readiness gate sees clean discharge windows.
    now = int(time.time())
    weather = []
    for i in range(-14 * 24, 24 * 5):  # past 14 days through next 5 days, hourly
        ts = now + i * 3600
        # 8am-4pm peaks at 800 W/m², zero at night
        hour_of_day = (ts // 3600) % 24
        ghi = 800 if 8 <= hour_of_day <= 16 else 0
        weather.append({"ts": ts, "ghi_w_m2": ghi, "cloud_cover_pct": 0})

    def _synth_soc(ts: int) -> int:
        # 90% during the day, drops 5pp/hour overnight, recovers at dawn.
        h = (ts // 3600) % 24
        if 8 <= h <= 16:
            return 90
        # Hours away from sunset (16) — drops linearly until dawn.
        hours_after_sunset = (h - 16) % 24
        return max(60, 90 - hours_after_sunset * 5)

    energy = [{"ts": w["ts"], "solar_w": int(2.0 * w["ghi_w_m2"]),
               "solar_wh": int(2.0 * w["ghi_w_m2"]),
               "output_w": 100, "output_wh": 100,
               "ac_input_wh": 0, "ac_input_w": 0,
               "battery_pct": _synth_soc(w["ts"])}
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
    # With strong solar > load+overhead, peak SOC should land above the start.
    peak = max(h["predicted_soc"] for h in res["forecast"])
    assert peak >= 50.0


# ---------- fit_idle_overhead_w ----------

def _discharge_window(ts0: int, soc0: int, soc1: int, out_w: int):
    """Helper: build two adjacent hourly buckets describing a clean
    discharge (no solar, no AC charging) where SOC drops from soc0 to soc1
    while reporting `out_w` average AC output."""
    return [
        {"ts": ts0,         "battery_pct": soc0, "output_wh": out_w,
         "solar_wh": 0, "ac_input_wh": 0},
        {"ts": ts0 + 3600,  "battery_pct": soc1, "output_wh": out_w,
         "solar_wh": 0, "ac_input_wh": 0},
    ]


def test_idle_overhead_returns_default_when_no_data():
    overhead, n = forecaster.fit_idle_overhead_w([], capacity_wh=30000)
    assert overhead == forecaster.DEFAULT_IDLE_OVERHEAD_W
    assert n == 0


def test_idle_overhead_returns_default_when_too_few_windows():
    # Only one qualifying window — below min_windows (5) so we should
    # get the default back. Use a 2pp drop to satisfy the new
    # INVERTER_FIT_MIN_SOC_DROP_PCT gate (1pp windows are noise).
    history = _discharge_window(1_700_000_000, 80, 78, 545)
    overhead, n = forecaster.fit_idle_overhead_w(history, capacity_wh=30000)
    assert overhead == forecaster.DEFAULT_IDLE_OVERHEAD_W
    assert n == 1


def test_inverter_overhead_pct_recovers_known_value():
    # Synthetic: SOC drops 2pp/hour on a 30000 Wh pack = 600 W observed
    # drain. Reported out_w = 545 W → implied pct ≈ (600-545)/545 = 0.101.
    # 2pp drop is required by the new MIN_SOC_DROP gate (1pp windows
    # are dominated by quantization noise).
    history = []
    base = 1_700_000_000
    for i in range(8):
        history.extend(_discharge_window(base + i * 3600 * 3,
                                          80 - 2 * i, 78 - 2 * i, out_w=545))
    pct, n = forecaster.fit_inverter_overhead_pct(history, capacity_wh=30000)
    assert n >= 5
    assert 0.08 <= pct <= 0.12, f"got {pct}, expected ~0.10"


def test_inverter_overhead_pct_skips_solar_polluted_windows():
    # Windows with solar should be excluded — otherwise the fit would
    # see "negative drain" (solar charging counted as load reduction)
    # and skew low. Mix 6 clean windows (10% pct) with 6 solar-polluted.
    # Each window now needs 2pp drop to satisfy the MIN_SOC_DROP gate.
    history = []
    base = 1_700_000_000
    # Clean: 2pp/hour drop + 545W out_w on 30000Wh = ~10% pct
    for i in range(6):
        history.extend(_discharge_window(base + i * 3600 * 3,
                                          80 - 2 * i, 78 - 2 * i, out_w=545))
    # Polluted: same SOC drop + same out_w but with solar present
    for i in range(6):
        ts = base + (100 + i * 3) * 3600
        history.append({"ts": ts, "battery_pct": 70 - 2 * i, "output_wh": 545,
                        "solar_wh": 1500, "ac_input_wh": 0})
        history.append({"ts": ts + 3600, "battery_pct": 68 - 2 * i, "output_wh": 545,
                        "solar_wh": 1500, "ac_input_wh": 0})
    pct, _n = forecaster.fit_inverter_overhead_pct(history, capacity_wh=30000)
    assert 0.08 <= pct <= 0.12, f"got {pct}, expected ~0.10 (solar excluded)"


def test_inverter_overhead_pct_falls_back_when_ratios_are_negative():
    # If reported out_w consistently exceeds the SOC-implied drain (a
    # device whose AC output sensor over-reads, or unaccounted-for
    # charging the fit can't see), every window's ratio is negative.
    # We no longer per-sample-clamp to 0 (that biased the fit upward
    # under quantization noise). The MEDIAN clamps to fallback when
    # negative — caller gets DEFAULT_INVERTER_OVERHEAD_PCT.
    history = []
    base = 1_700_000_000
    for i in range(8):
        # 2pp drop on 30000Wh = 600W observed, but report 1000W out_w.
        # Per-window ratio = (600-1000)/1000 = -0.40 → median is
        # negative → fall back to DEFAULT_INVERTER_OVERHEAD_PCT.
        history.extend(_discharge_window(base + i * 3600 * 3,
                                          80 - 2 * i, 78 - 2 * i, out_w=1000))
    pct, n = forecaster.fit_inverter_overhead_pct(history, capacity_wh=30000)
    assert n >= 5
    assert pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT


def test_fit_drain_model_recovers_parasitic_and_overhead():
    """Synthesize history where the true drain follows
       drain = 300 + load * 1.10
    (300W constant baseline + 10% throughput overhead). The pure-
    percentage model would compute pct = (drain - load) / load which
    explodes for low loads — this hybrid fit recovers both terms."""
    base = 1_700_000_000
    history = []
    # 12 clean discharge windows at varying loads. drain in W; on a
    # 30000Wh capacity, drain*1h corresponds to drain/300 pp drop.
    # Make sure soc_drop >= 2pp.
    for i, load_w in enumerate([200, 400, 600, 800, 1000, 1200,
                                 200, 500, 700, 900, 1100, 1300]):
        true_drain = 300 + load_w * 1.10
        # 1h windows. pp_drop = drain / capacity * 100.
        pp_drop = true_drain / 30000 * 100  # ~2-5pp
        soc0 = 80 - i * 6
        soc1 = round(soc0 - pp_drop, 0)  # cloud quantizes to 1pp
        history.append({"ts": base + i * 3 * 3600,
                        "battery_pct": soc0,
                        "output_wh": load_w,
                        "solar_wh": 0, "ac_input_wh": 0})
        history.append({"ts": base + i * 3 * 3600 + 3600,
                        "battery_pct": int(soc1),
                        "output_wh": load_w,
                        "solar_wh": 0, "ac_input_wh": 0})
    parasitic_w, overhead_pct, n = forecaster.fit_drain_model(
        history, capacity_wh=30000,
    )
    assert n >= 5
    # Quantization noise on 1pp SOC steps loosens the fit; require
    # rough recovery, not exact match.
    assert 200 <= parasitic_w <= 400, f"got parasitic_w={parasitic_w}"
    assert 0.05 <= overhead_pct <= 0.20, f"got pct={overhead_pct}"


def test_fit_drain_model_recovers_parasitic_via_multi_hour_runs():
    """Replicates the user's overnight pattern flagged by advisor on
    2026-05-05T18:57: steady ~462 W load, true parasitic ~385 W.
    Per-pair median was biased low by SOC quantization (true 2.8pp/h
    rounded sometimes to 2pp, undercounting drain). The multi-hour
    clean-discharge run path aggregates noise across the run and
    should recover the real parasitic value."""
    base = 1_700_000_000
    # 8 hour-long clean buckets, simulating an overnight run.
    # True drain: 385 + 462 * 1.10 = 893 W. On 30240 Wh that's
    # 2.95pp/h. After 8 hours the SOC drop is ~24pp, large enough
    # that the ±1pp quantization on each end (±0.5pp / 24pp = 4%
    # noise) doesn't dominate the fit.
    history = []
    soc = 95
    for h in range(9):
        history.append({
            "ts": base + h * 3600,
            "battery_pct": soc,
            "output_wh": 462,
            "solar_wh": 0, "ac_input_wh": 0,
        })
        soc -= 3  # quantize to 3pp/h ≈ true 2.95pp/h
    # Throw in 5 noisy short pairs at the SAME load: alternating 2pp
    # / 3pp drops simulating the boundary jitter that biased the
    # original per-pair median low. With the multi-hour-run fix in
    # place, these don't matter — the run-based median dominates.
    for i in range(5):
        ts = base + (10 + i * 3) * 3600
        drop = 2 if i % 2 == 0 else 3
        history.append({"ts": ts, "battery_pct": 70 - i,
                        "output_wh": 462,
                        "solar_wh": 0, "ac_input_wh": 0})
        history.append({"ts": ts + 3600, "battery_pct": 70 - i - drop,
                        "output_wh": 462,
                        "solar_wh": 0, "ac_input_wh": 0})

    parasitic_w, overhead_pct, _n = forecaster.fit_drain_model(
        history, capacity_wh=30240,
    )
    # Narrow-load fallback fires; overhead pinned at default.
    assert overhead_pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT
    # Multi-hour run should recover ~385 W parasitic (allowing
    # quantization slop). The OLD per-pair median would land near
    # ~100 W on this exact data — that's the bug.
    assert 280 <= parasitic_w <= 450, (
        f"got parasitic_w={parasitic_w}; multi-hour run fit should "
        "recover the real ~385 W baseline, not collapse to the "
        "quantization-biased ~100 W from the old per-pair fit"
    )


def test_fit_drain_model_uses_parasitic_only_fallback_for_narrow_loads():
    """Replicates the user's overnight pattern flagged by the advisor on
    2026-05-05: steady ~470W load, true parasitic ~415W. With loads
    nearly identical across windows, the joint OLS is ill-conditioned
    and the original code collapsed to the (50W, 0.10) prior. The
    parasitic-only fallback should recover ~415W instead."""
    base = 1_700_000_000
    # Continuous 9-hour timeline, SOC drops 3pp/hour at ~470W steady load.
    # True drain = 415 + 470*1.10 = 932W ≈ 3.1pp/h on 30000Wh; quantized
    # to 3pp gives observed drain = 900W → implied parasitic = 900 -
    # 470*1.10 ≈ 383W. Loads alternate 465/475 to make the median
    # non-degenerate but stay well within the 2x narrow-load gate.
    history = []
    soc = 90
    for h in range(9):
        load_w = 465 if h % 2 == 0 else 475
        history.append({
            "ts": base + h * 3600,
            "battery_pct": soc,
            "output_wh": load_w,
            "solar_wh": 0, "ac_input_wh": 0,
        })
        soc -= 3

    parasitic_w, overhead_pct, n = forecaster.fit_drain_model(
        history, capacity_wh=30000,
    )
    assert n >= 5
    # Parasitic-only fallback fires; overhead pinned at default.
    assert overhead_pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT
    # Quantization loosens the fit; require rough recovery near ~383W
    # (the true 415W minus ~32W loss to integer-pp SOC steps).
    assert 350 <= parasitic_w <= 450, f"got parasitic_w={parasitic_w}"


def test_fit_drain_model_outlier_does_not_disable_narrow_fallback():
    """Advisor caught (2026-05-05) that one outlier high-load window
    in a 14d history was pushing max/min above 2x even though 99% of
    windows clustered narrowly — disabling the parasitic-only fallback.
    The percentile-based metric (p90/p10) ignores outliers and
    correctly classifies the device as 'narrow'."""
    base = 1_700_000_000
    # 16 narrow-load buckets at ~470W steady (real overnight pattern)
    # plus 2 outlier buckets at 1500W (one short kettle run during 14d).
    # max/min = 1500/465 = 3.23 → old gate would pick OLS path (collapses).
    # p90/p10 should land near 1.0 → narrow-fallback fires correctly.
    history = []
    soc = 95
    for h in range(16):
        load_w = 465 if h % 2 == 0 else 475
        history.append({
            "ts": base + h * 3600,
            "battery_pct": soc,
            "output_wh": load_w,
            "solar_wh": 0, "ac_input_wh": 0,
        })
        soc -= 3  # 3pp/h drop at ~932W true drain
    # Tack on 2 outlier high-load hours (1500W kettle run).
    for h in range(2):
        history.append({
            "ts": base + (16 + h) * 3600,
            "battery_pct": soc,
            "output_wh": 1500,
            "solar_wh": 0, "ac_input_wh": 0,
        })
        soc -= 6  # 6pp/h drop at higher load

    parasitic_w, overhead_pct, n = forecaster.fit_drain_model(
        history, capacity_wh=30000,
    )
    assert n >= 5
    assert overhead_pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT, \
        "narrow-load fallback should fire despite outlier high-load window"
    assert 350 <= parasitic_w <= 450, f"got parasitic_w={parasitic_w}"


def test_fit_drain_model_falls_back_when_too_few_windows():
    """Single qualifying window is not enough — defaults are returned
    so callers don't blindly trust a one-shot fit."""
    history = _discharge_window(1_700_000_000, 80, 78, out_w=545)
    parasitic_w, overhead_pct, n = forecaster.fit_drain_model(
        history, capacity_wh=30000,
    )
    assert n == 1
    assert parasitic_w == forecaster.DEFAULT_PARASITIC_W
    assert overhead_pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT


def test_fit_drain_model_falls_back_on_implausible_coefficients():
    """If the OLS produces a negative parasitic (e.g. all windows have
    drain < load * 1.0), fall back to defaults rather than emit a value
    that says the battery gains energy at idle."""
    base = 1_700_000_000
    history = []
    # 8 windows with drain consistently BELOW load (sensor over-reads
    # or hidden charging). Makes OLS regress to negative intercept.
    for i, load_w in enumerate([1000, 1200, 1400, 1600, 1800,
                                 1000, 1300, 1500]):
        # Force soc_drop < load * 1h / capacity, so drain < load.
        pp_drop = max(2, int((load_w * 0.6) / 30000 * 100))
        history.append({"ts": base + i * 3 * 3600,
                        "battery_pct": 80,
                        "output_wh": load_w,
                        "solar_wh": 0, "ac_input_wh": 0})
        history.append({"ts": base + i * 3 * 3600 + 3600,
                        "battery_pct": 80 - pp_drop,
                        "output_wh": load_w,
                        "solar_wh": 0, "ac_input_wh": 0})
    parasitic_w, overhead_pct, n = forecaster.fit_drain_model(
        history, capacity_wh=30000,
    )
    assert n >= 5
    assert parasitic_w == forecaster.DEFAULT_PARASITIC_W
    assert overhead_pct == forecaster.DEFAULT_INVERTER_OVERHEAD_PCT


def test_diagnose_idle_windows_classifies_each_rejection():
    """Continuous timeline of 9 hourly buckets, each adjacent pair
    constructed to hit a specific rejection cause (or qualify). Verifies
    the breakdown matches exactly so we catch any drift between
    diagnose_idle_windows and fit_inverter_overhead_pct's gates."""
    base = 1_700_000_000
    h = lambda i, soc, out=545, solar=0, ac=0: {  # noqa: E731
        "ts": base + i * 3600,
        "battery_pct": soc,
        "output_wh": out, "solar_wh": solar, "ac_input_wh": ac,
    }
    history = [
        h(0, 80),                      # pair 0->1: clean 2pp drop -> QUALIFY
        h(1, 78),                      # pair 1->2: clean 2pp drop -> QUALIFY
        h(2, 76),                      # pair 2->3: SOC None on b -> missing_soc
        h(3, None),                    # pair 3->4: SOC None on a -> missing_soc
        h(4, 75),                      # pair 4->5: 1pp drop      -> soc_drop_under_2pp
        h(5, 74, solar=1000),          # pair 5->6: solar on a    -> solar_above_noise
        h(6, 72, ac=2000),             # pair 6->7: ac on a       -> ac_input_above_noise
        h(7, 70, out=30),              # pair 7->8: out_wh=30W    -> out_w_under_50
        h(8, 68, out=30),
    ]
    diag = forecaster.diagnose_idle_windows(history)
    assert diag["total_pairs"] == 8
    assert diag["qualifying_windows"] == 2
    assert diag["needed_windows"] == forecaster.MIN_FORECAST_IDLE_WINDOWS
    assert diag["rejected"] == {
        "missing_soc": 2,
        "soc_drop_under_2pp": 1,
        "solar_above_noise": 1,
        "ac_input_above_noise": 1,
        "dt_out_of_range": 0,
        "out_w_under_50": 1,
    }


def test_diagnose_idle_windows_handles_empty_history():
    diag = forecaster.diagnose_idle_windows([])
    assert diag["total_pairs"] == 0
    assert diag["qualifying_windows"] == 0
    assert all(v == 0 for v in diag["rejected"].values())


def test_inverter_overhead_pct_used_by_build_forecast():
    # End-to-end: a history with a clear 10% overhead should make
    # build_forecast surface inverter_overhead_pct ≈ 0.10 in its result.
    # 2pp drops per window (was 1pp) for the new MIN_SOC_DROP gate.
    now = int(time.time())
    history = []
    # 15 cycles x (2 rows each, 2pp drop each) -> 15 qualifying windows.
    # SOC ramps 90 → 60 over 30 cycles, well above DEPLETED floor.
    for i in range(15):
        ts = now - 60 * 3600 + i * 3600 * 3
        history.append({"ts": ts, "battery_pct": 90 - 2 * i, "output_w": 545,
                        "output_wh": 545, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 88 - 2 * i, "output_w": 545,
                        "output_wh": 545, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=50.0, capacity_wh=30000, now_ts=now,
        horizon_hours=24,
    )
    assert res.get("ready") is True
    assert "inverter_overhead_pct" in res
    assert "inverter_overhead_n_windows" in res
    assert res["inverter_overhead_n_windows"] >= 5
    assert 0.08 <= res["inverter_overhead_pct"] <= 0.12


def test_build_forecast_blocks_when_history_too_short():
    # The readiness gate should refuse to produce a forecast on a fresh
    # install. Caller sees `ready: False` + a `readiness` block with the
    # progress so the UI can show a calibration message.
    now = int(time.time())
    # Only 4 hours of history — well below MIN_FORECAST_HISTORY_HOURS.
    history = [
        {"ts": now - 4 * 3600, "battery_pct": 80, "output_w": 100,
         "output_wh": 100, "solar_wh": 0, "ac_input_wh": 0},
        {"ts": now - 3 * 3600, "battery_pct": 79, "output_w": 100,
         "output_wh": 100, "solar_wh": 0, "ac_input_wh": 0},
    ]
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=80.0, capacity_wh=30000, now_ts=now,
    )
    assert res["ready"] is False
    assert res["forecast"] == []
    r = res["readiness"]
    assert r["reason"] == "calibrating"
    assert r["have_hours"] < r["needed_hours"]


def test_build_forecast_emits_for_low_usage_backup_device():
    """A backup device that's intentionally idle most of the time (e.g.
    a HomePower 3000 used a few times a year) won't accumulate clean
    discharge windows — but should still get a forecast using default
    overhead. The fit's window count is reported via the
    `low_confidence_overhead_fit` flag for UI transparency."""
    now = int(time.time())
    # 6 days of history, mostly idle (1pp drift per hour, no real load).
    # No window will pass the 2pp + 50W out_w gates — exactly the
    # HomePower 3000 stuck-in-calibrating scenario.
    history = []
    soc = 90
    for i in range(-6 * 24, 0):
        ts = now + i * 3600
        history.append({
            "ts": ts, "battery_pct": soc,
            "output_w": 20, "output_wh": 20,  # below MIN_OUT_W=50W
            "solar_wh": 0, "ac_input_wh": 0,
        })
        # Drift down 1pp every 4 hours — never hits the 2pp gate.
        if i % 4 == 0:
            soc = max(70, soc - 1)
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=80.0, capacity_wh=3024, now_ts=now,
    )
    assert res["ready"] is True, "low-usage device should still get a forecast"
    assert len(res["forecast"]) > 0
    r = res["readiness"]
    assert r["reason"] == "ready"
    assert r["low_confidence_overhead_fit"] is True
    # have_idle_windows is reported but doesn't gate readiness anymore.
    assert r["have_idle_windows"] < r["needed_idle_windows"]


# ---------- fit_charge_efficiency ----------

def _charge_window(ts0: int, soc0: int, soc1: int, input_wh: int):
    """Two adjacent hourly buckets describing a clean charging window."""
    return [
        {"ts": ts0, "battery_pct": soc0, "input_wh": input_wh,
         "solar_wh": 0, "ac_input_wh": 0, "output_wh": 0},
        {"ts": ts0 + 3600, "battery_pct": soc1, "input_wh": input_wh,
         "solar_wh": 0, "ac_input_wh": 0, "output_wh": 0},
    ]


def test_charge_efficiency_default_when_no_data():
    eff, n = forecaster.fit_charge_efficiency([], capacity_wh=30000)
    assert eff == forecaster.DEFAULT_CHARGE_EFFICIENCY
    assert n == 0


def test_charge_efficiency_recovers_known_value():
    # 30000 Wh pack. Charge 1pp/hour = 300 Wh stored. With 333 Wh of
    # input that's an efficiency of 300/333 ≈ 0.90.
    history = []
    base = 1_700_000_000
    for i in range(8):
        history.extend(_charge_window(base + i * 3600 * 2,
                                      50 + i, 51 + i, input_wh=333))
    eff, n = forecaster.fit_charge_efficiency(history, capacity_wh=30000)
    assert n >= 5
    assert 0.85 <= eff <= 0.95, f"got {eff}, expected ~0.90"


def test_charge_efficiency_skips_top_balance_regime():
    # Above 95% SOC, charge tapers — using these windows would
    # under-estimate efficiency. Mix some clean windows (gives 0.90)
    # with some top-balance windows (would falsely indicate 0.30) and
    # verify the fit stays near 0.90.
    history = []
    base = 1_700_000_000
    # Clean windows: 50→51%, input 333Wh → 0.90 efficiency
    for i in range(6):
        history.extend(_charge_window(base + i * 3600 * 3,
                                      50 + i, 51 + i, input_wh=333))
    # Top-balance: 96→97% but huge input (BMS throttling) → bogus 0.30
    for i in range(6):
        ts = base + (100 + i) * 3600
        history.extend(_charge_window(ts, 96, 97, input_wh=1000))
    eff, _n = forecaster.fit_charge_efficiency(history, capacity_wh=30000)
    assert 0.85 <= eff <= 0.95, f"got {eff}, expected ~0.90"


def test_charge_efficiency_clamps_implausible_values():
    # If the fit lands outside [0.50, 0.99] it's almost certainly bad
    # data — fall back to the default rather than feeding garbage into
    # the simulator.
    history = []
    base = 1_700_000_000
    for i in range(8):
        # Reports 100 Wh input but SOC jumped 5pp = 1500 Wh "stored" —
        # implies efficiency 15.0 (impossible).
        history.extend(_charge_window(base + i * 3600 * 2,
                                      50 + i * 5, 55 + i * 5, input_wh=100))
    eff, n = forecaster.fit_charge_efficiency(history, capacity_wh=30000)
    assert eff == forecaster.DEFAULT_CHARGE_EFFICIENCY
    assert n >= 5  # we DID find windows, just rejected the median


def test_charge_efficiency_used_by_build_forecast():
    # End-to-end: a history with a clear ~0.85 efficiency makes
    # build_forecast surface charge_efficiency near that value. Need
    # both charging windows (for the efficiency fit) AND discharge
    # windows (for the readiness gate's idle_overhead requirement).
    now = int(time.time())
    history = []
    # Charging windows: 8 of them across 16h. 1pp gain → 300Wh stored,
    # input 353Wh → 0.85 efficiency.
    for i in range(8):
        ts = now - 30 * 3600 + i * 3600 * 2
        history.append({"ts": ts, "battery_pct": 50 + i,
                        "input_wh": 353, "input_w": 353,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 0, "output_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 51 + i,
                        "input_wh": 353, "input_w": 353,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 0, "output_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
    # Discharge windows: 8 more across 24h to satisfy the readiness gate.
    # Need 2pp drops per window (was 1pp) for the new MIN_SOC_DROP gate.
    # 30000Wh x 2pp / 1h = 600W drain; out_w=545 -> ~10% overhead.
    for i in range(8):
        ts = now - 24 * 3600 + i * 3600 * 3
        history.append({"ts": ts, "battery_pct": 80 - 2 * i,
                        "input_wh": 0, "input_w": 0,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 545, "output_wh": 545,
                        "ac_input_wh": 0, "ac_input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 78 - 2 * i,
                        "input_wh": 0, "input_w": 0,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 545, "output_wh": 545,
                        "ac_input_wh": 0, "ac_input_w": 0})
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=50.0, capacity_wh=30000, now_ts=now,
        horizon_hours=24,
    )
    assert res["ready"] is True
    assert "charge_efficiency" in res
    assert "charge_efficiency_n_windows" in res
    assert res["charge_efficiency_n_windows"] >= 5
    assert 0.80 <= res["charge_efficiency"] <= 0.90


# ---------- fit_max_charge_w ----------

def test_max_charge_w_returns_none_with_no_data():
    w, n = forecaster.fit_max_charge_w([])
    assert w is None
    assert n == 0


def test_max_charge_w_filters_idle_samples():
    # Idle/noise samples (input_w < 100W) should NOT influence the fit;
    # only real charging events count.
    base = 1_700_000_000
    history = [
        {"ts": base + i * 600, "input_w": v, "battery_pct": 50}
        for i, v in enumerate([5, 10, 50, 80] * 10)  # 40 idle samples
    ]
    w, n = forecaster.fit_max_charge_w(history)
    assert w is None
    assert n == 0


def test_max_charge_w_recovers_observed_peak():
    # Synthetic charging history with a clear ~1500W steady-state.
    # 95th percentile should land near 1500W (a few brief spikes to
    # 1700 don't dominate). Pass a non-zero tz_offset so the
    # night-band fallback engages (the strict guard rejects tz=0
    # without weather data).
    base = 1_700_000_000  # 2023-11-14 22:13:20 UTC = night in UTC-8
    history = [
        {"ts": base + i * 600, "input_w": v, "ac_input_w": 0, "solar_w": 0,
         "battery_pct": 50}
        for i, v in enumerate([1500, 1500, 1480, 1520, 1500] * 10
                              + [1700, 1650])
    ]
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=-28800)
    assert n >= 6
    assert 1450 <= w <= 1750, f"got {w}, expected ~1500-1700"


def test_max_charge_w_works_for_low_power_users():
    # A 600W standard-charging user should get back ~600W, not the
    # 1500W "fast" default we used to hardcode. Use an explicit PST
    # offset so the night-band engages (UTC-22:13 = PST 14:13 PM,
    # but the test's range spans into UTC night which IS PST night
    # too). Adjust base so all samples are in PST night.
    pst_night_utc = 1_700_028_000  # 2023-11-15 06:00:00 UTC = 22:00 PST 11/14
    history = [
        {"ts": pst_night_utc + i * 600, "input_w": v, "ac_input_w": 0,
         "solar_w": 0, "battery_pct": 50}
        for i, v in enumerate([580, 600, 620, 600, 595, 605, 600, 610, 590, 600] * 3)
    ]
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=-28800)
    assert n >= 6
    assert 580 <= w <= 650, f"got {w}, expected ~600"


def test_max_charge_w_excludes_solar_daytime_input():
    # Regression test for the bug a user reported: fit was returning
    # ~3812W on a setup whose actual AC charge rate is ~1500W. Cause:
    # the function read input_w (= grid + solar + car), so daytime
    # solar production got counted as AC charging.
    pst_daytime_utc = 1_700_071_200  # 2023-11-15 18:00 UTC = 10:00 PST → daytime
    pst_night_utc   = 1_700_028_000  # 2023-11-15 06:00 UTC = 22:00 PST → night
    history = []
    # 20 daytime samples at 3700W solar (PST 10:00 = daytime, no AC).
    for i in range(20):
        history.append({"ts": pst_daytime_utc + i * 60, "input_w": 3700,
                        "ac_input_w": 0, "solar_w": 3700, "battery_pct": 80})
    # 10 night samples at 1500W real AC charging (no solar reading).
    for i in range(10):
        history.append({"ts": pst_night_utc + i * 60, "input_w": 1500,
                        "ac_input_w": 0, "solar_w": 0, "battery_pct": 50})
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=-28800)
    # Daytime samples skipped (PST daytime + no GHI). Night samples
    # pass (PST night + solar_w=0). Result: 10 night-only samples.
    assert n == 10, f"expected 10 night-only samples, got {n}"
    assert 1450 <= w <= 1550, (
        f"got {w}; expected ~1500 (NOT 3700, the solar reading)"
    )


def test_max_charge_w_uses_ac_input_w_when_populated():
    # For users whose cloud `acip` is reliable, the function should
    # prefer it directly — even daytime samples are valid because
    # `ac_input_w` is already classified as grid charging.
    noon = 1_700_049_600  # daytime UTC
    history = [
        {"ts": noon + i * 60, "input_w": 3700, "ac_input_w": 1500,
         "battery_pct": 50}
        for i in range(10)
    ]
    w, n = forecaster.fit_max_charge_w(history)
    assert n == 10
    assert w == 1500.0  # ac_input_w wins, not input_w


def test_max_charge_w_ghi_filter_excludes_solar_regardless_of_clock():
    # Bug repro for the user's "3595W solar mislabeled as AC" report:
    # fit_max_charge_w is called with tz_offset=0 (UTC), but the user
    # is in PDT. UTC 21:00-06:00 (the "night band") happens to be PDT
    # 14:00-23:00 — solar peak afternoon. With weather_hourly carrying
    # GHI per hour, we can exclude solar samples regardless of
    # timezone.
    afternoon_utc = 1_700_049_600   # 12:00 UTC = 04:00 PDT (... but if the user
    # were in a tz where 12:00 UTC is daytime locally, this same hour would have
    # high GHI). What matters: GHI is the source of truth.
    history = []
    weather = []
    # 20 samples at 4000W of solar input, GHI=900 (clearly daytime).
    # Without the GHI filter and with tz_offset=0, these COULD have been
    # counted as "night" depending on UTC hour. With GHI we always skip.
    for i in range(20):
        ts = afternoon_utc + i * 60
        history.append({"ts": ts, "input_w": 4000, "ac_input_w": 0,
                        "solar_w": 4000, "battery_pct": 80})
    weather.append({"ts": afternoon_utc - (afternoon_utc % 3600),
                    "ghi_w_m2": 900, "cloud_cover_pct": 0})
    # 10 samples at 1500W with GHI=0 (real night, real AC).
    night_utc = afternoon_utc + 12 * 3600
    for i in range(10):
        ts = night_utc + i * 60
        history.append({"ts": ts, "input_w": 1500, "ac_input_w": 0,
                        "solar_w": 0, "battery_pct": 50})
    weather.append({"ts": night_utc - (night_utc % 3600),
                    "ghi_w_m2": 0, "cloud_cover_pct": 100})
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=0,
                                        weather_hourly=weather)
    # Solar samples excluded by GHI filter regardless of UTC hour.
    assert n == 10, f"expected 10 dark-GHI samples, got {n}"
    assert 1450 <= w <= 1550, f"got {w}; expected ~1500 (NOT 4000, the solar)"


def test_max_charge_w_skips_phantom_solar_at_night():
    # Defensive case the user surfaced: cloud reports `acip=0` and
    # `solar_w=3500W` at *night* — physically impossible (no sun),
    # almost certainly a cloud bug where it's falsely re-classifying
    # cleared-state input as solar. Without this guard, the night-band
    # fallback would count `input_w=3500` as AC charging.
    pst_night_utc = 1_700_071_200  # 2023-11-15 18:00:00 UTC = 10:00 PST
    # We use UTC=18:00 with tz_offset=-28800 (PST): local = 10:00 — no, that's
    # not night. Pick a UTC that maps to PST night:
    pst_night_utc = 1_700_103_600  # 2023-11-15 03:00:00 UTC next day = 19:00 PST 11/14
    # Better: pick UTC 06:00, which is 22:00 PST night.
    pst_night_utc = 1_700_028_000  # 2023-11-15 06:00:00 UTC = 22:00 PST 11/14
    history = [
        {"ts": pst_night_utc + i * 60, "input_w": 3500, "ac_input_w": 0,
         "solar_w": 3500, "battery_pct": 60}
        for i in range(15)
    ]
    # No weather data passed — fall through to night-band path.
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=-28800)
    # Phantom solar at night should be skipped, leaving no candidates.
    assert n == 0, f"phantom-solar samples should be excluded, got {n}"
    assert w is None


def test_max_charge_w_no_tz_no_ghi_skips_everything():
    # Fresh-install scenario: user hasn't set location yet, so no
    # tz_offset and no weather observations. Without those signals we
    # can't distinguish solar from AC — fall back to source=default
    # rather than guessing.
    base = 1_700_000_000
    history = [
        {"ts": base + i * 600, "input_w": 1500, "ac_input_w": 0,
         "solar_w": 0, "battery_pct": 50}
        for i in range(20)
    ]
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=0)
    assert n == 0, f"with no tz and no GHI we shouldn't count anything, got {n}"
    assert w is None


def test_max_charge_w_skips_classified_ac_when_solar_also_high():
    # Defensive case: cloud reports both ac_input_w=3000 AND
    # solar_w=3000 (mis-classification). Should NOT trust ac_input_w
    # in this case — fall through to GHI-based check.
    noon = 1_700_049_600
    history = [
        {"ts": noon + i * 60, "input_w": 6000, "ac_input_w": 3000,
         "solar_w": 3000, "battery_pct": 50}
        for i in range(10)
    ]
    weather = [{"ts": noon - (noon % 3600), "ghi_w_m2": 800, "cloud_cover_pct": 0}]
    w, n = forecaster.fit_max_charge_w(history, weather_hourly=weather)
    # Both classification AND solar high → can't trust either; skipped.
    assert n == 0, f"expected to skip mixed classification, got n={n}"
    assert w is None


def test_max_charge_w_tz_offset_shifts_night_band():
    # The night-only fallback should respect the user's local time.
    # 08:00 UTC is midnight PST (UTC-8): in PST it should count, in
    # UTC it shouldn't.
    eight_utc = 1_700_035_200  # 2023-11-15 08:00:00 UTC = 00:00 PST
    history = [
        {"ts": eight_utc + i * 60, "input_w": 1200, "ac_input_w": 0,
         "battery_pct": 50}
        for i in range(15)
    ]
    _, n_utc = forecaster.fit_max_charge_w(history, tz_offset_seconds=0)
    assert n_utc == 0  # 08 UTC is daytime, no AC classification → excluded
    w_pst, n_pst = forecaster.fit_max_charge_w(history, tz_offset_seconds=-28800)
    assert n_pst == 15  # 00 PST is night → all included
    assert 1150 <= w_pst <= 1250
