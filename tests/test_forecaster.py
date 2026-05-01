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


def test_expected_load_uses_idle_default_when_no_data():
    # Empty profile → forecast hour gets IDLE_LOAD_W (the per-hour
    # fallback) PLUS IDLE_OVERHEAD_W (the inverter idle / DC-bus draw
    # that doesn't show up in `op` but still drains the battery — added
    # universally so the load model matches SOC slope reality).
    # This is the regression test for the 0%-predicted-vs-44%-actual bug.
    profile: dict[tuple[int, int], float] = {}
    load = forecaster.expected_load_w(profile, 1_700_000_000)
    assert load == forecaster.IDLE_LOAD_W + forecaster.IDLE_OVERHEAD_W


def test_expected_load_falls_back_to_neighbor_hour_not_global_mean():
    # User has heavy daytime activity (avg ~500W) but quiet evenings
    # (~50W). A missing 2am bucket should inherit from neighboring night
    # hours (1am, 3am) — NOT the global mean. The IDLE_OVERHEAD_W term
    # is added uniformly so it doesn't affect WHICH bucket is selected,
    # only the absolute value.
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
    # Should pick a night neighbor (40 or 50), with overhead added on
    # top — NOT 500-ish (which would be daytime leakage).
    expected = {40.0 + forecaster.IDLE_OVERHEAD_W,
                50.0 + forecaster.IDLE_OVERHEAD_W}
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
    # get the default back.
    history = _discharge_window(1_700_000_000, 80, 79, 200)
    overhead, n = forecaster.fit_idle_overhead_w(history, capacity_wh=30000)
    assert overhead == forecaster.DEFAULT_IDLE_OVERHEAD_W
    assert n == 1


def test_idle_overhead_recovers_known_parasitic():
    # Synthetic: SOC drops 1pp/hour on a 30000 Wh pack = 300 W observed
    # drain. Reported out_w is 100 W. Implied parasitic = 200 W.
    history = []
    base = 1_700_000_000
    for i in range(8):
        history.extend(_discharge_window(base + i * 3600 * 2,
                                          80 - i, 79 - i, out_w=100))
    overhead, n = forecaster.fit_idle_overhead_w(history, capacity_wh=30000)
    assert n >= 5
    assert 180 <= overhead <= 220, f"got {overhead}, expected ~200"


def test_idle_overhead_skips_solar_polluted_windows():
    # Windows with solar should be excluded — otherwise the fit would
    # mis-attribute solar charging as negative drain. Synthesize 6 clean
    # discharge windows (200W parasitic) and 6 solar-polluted ones with
    # bogus high gaps; the fit should only use the clean set.
    history = []
    base = 1_700_000_000
    # Clean: 1pp/hour drop + 100W out_w on 30000Wh = 200W parasitic
    for i in range(6):
        history.extend(_discharge_window(base + i * 3600 * 3,
                                          80 - i, 79 - i, out_w=100))
    # Polluted: same SOC drop + same out_w but with solar present
    for i in range(6):
        ts = base + (100 + i) * 3600
        history.append({"ts": ts, "battery_pct": 70 - i, "output_wh": 100,
                        "solar_wh": 1500, "ac_input_wh": 0})
        history.append({"ts": ts + 3600, "battery_pct": 69 - i, "output_wh": 100,
                        "solar_wh": 1500, "ac_input_wh": 0})
    overhead, _n = forecaster.fit_idle_overhead_w(history, capacity_wh=30000)
    # Fit should land around 200W — solar windows excluded.
    assert 180 <= overhead <= 220, f"got {overhead}, expected ~200"


def test_idle_overhead_clamps_negative_to_zero():
    # If reported out_w exceeds the SOC-implied drain (a noisy sensor
    # reading), the gap is negative — we clamp to 0 rather than letting
    # bad samples push the median down.
    history = []
    base = 1_700_000_000
    for i in range(8):
        # 1pp drop on 30000Wh = 300W observed, but report 800W out_w.
        # Gap would be -500W; should be clamped to 0.
        history.extend(_discharge_window(base + i * 3600 * 2,
                                          80 - i, 79 - i, out_w=800))
    overhead, n = forecaster.fit_idle_overhead_w(history, capacity_wh=30000)
    assert n >= 5
    assert overhead == 0.0


def test_idle_overhead_used_by_build_forecast():
    # End-to-end: a history with a clear 200W parasitic should make
    # build_forecast surface idle_overhead_w ≈ 200 in its result. We
    # spread 15 windows across ~30h so the new readiness gate passes
    # (≥24h of history span + ≥5 discharge windows).
    now = int(time.time())
    history = []
    for i in range(15):
        ts = now - 30 * 3600 + i * 3600 * 2
        history.append({"ts": ts, "battery_pct": 90 - i, "output_w": 100,
                        "output_wh": 100, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 89 - i, "output_w": 100,
                        "output_wh": 100, "solar_w": 0, "solar_wh": 0,
                        "ac_input_wh": 0, "ac_input_w": 0})
    weather = [{"ts": now + i * 3600, "ghi_w_m2": 0, "cloud_cover_pct": 100}
               for i in range(48)]
    res = forecaster.build_forecast(
        energy_history=history, weather_hourly=weather,
        starting_soc_pct=50.0, capacity_wh=30000, now_ts=now,
        horizon_hours=24,
    )
    assert res.get("ready") is True
    assert "idle_overhead_w" in res
    assert "idle_overhead_n_windows" in res
    assert res["idle_overhead_n_windows"] >= 5
    assert 180 <= res["idle_overhead_w"] <= 220


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
    # Discharge windows: 8 more across 16h to satisfy the readiness gate.
    for i in range(8):
        ts = now - 14 * 3600 + i * 3600 * 2
        history.append({"ts": ts, "battery_pct": 70 - i,
                        "input_wh": 0, "input_w": 0,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 100, "output_wh": 100,
                        "ac_input_wh": 0, "ac_input_w": 0})
        history.append({"ts": ts + 3600, "battery_pct": 69 - i,
                        "input_wh": 0, "input_w": 0,
                        "solar_w": 0, "solar_wh": 0,
                        "output_w": 100, "output_wh": 100,
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
    # 1700 don't dominate).
    base = 1_700_000_000
    history = [
        {"ts": base + i * 600, "input_w": v, "battery_pct": 50}
        for i, v in enumerate([1500, 1500, 1480, 1520, 1500] * 10
                              + [1700, 1650])
    ]
    w, n = forecaster.fit_max_charge_w(history)
    assert n >= 6
    assert 1450 <= w <= 1750, f"got {w}, expected ~1500-1700"


def test_max_charge_w_works_for_low_power_users():
    # A 600W standard-charging user should get back ~600W, not the
    # 1500W "fast" default we used to hardcode. base=1.7e9 lands on a
    # UTC-22:13 timestamp, so all samples fall within the 21-06 night
    # band and the input_w fallback path counts them.
    base = 1_700_000_000
    history = [
        {"ts": base + i * 600, "input_w": v, "battery_pct": 50}
        for i, v in enumerate([580, 600, 620, 600, 595, 605, 600, 610, 590, 600] * 3)
    ]
    w, n = forecaster.fit_max_charge_w(history)
    assert n >= 6
    assert 580 <= w <= 650, f"got {w}, expected ~600"


def test_max_charge_w_excludes_solar_daytime_input():
    # Regression test for the bug a user reported: fit was returning
    # ~3812W on a setup whose actual AC charge rate is ~1500W. Cause:
    # the function read input_w (= grid + solar + car), so daytime
    # solar production got counted as AC charging.
    noon_utc = 1_700_049_600    # 2023-11-15 12:00:00 UTC — daytime
    threeam_utc = 1_700_016_000  # 2023-11-15 03:00:00 UTC — night
    history = []
    # 20 daytime samples at 3700W of solar input (no AC).
    for i in range(20):
        history.append({"ts": noon_utc + i * 60, "input_w": 3700,
                        "ac_input_w": 0, "battery_pct": 80})
    # 10 night samples at 1500W of real AC charging.
    for i in range(10):
        history.append({"ts": threeam_utc + i * 60, "input_w": 1500,
                        "ac_input_w": 0, "battery_pct": 50})
    w, n = forecaster.fit_max_charge_w(history, tz_offset_seconds=0)
    # Daytime solar is excluded; only night-time input counts.
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
