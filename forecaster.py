"""
Battery state-of-charge forecaster.

Three pieces, all simple:

1. **Solar model** — fits the linear coefficient k where:
       observed_solar_w ≈ k * ghi_w_m2
   using paired hourly samples from `energy_db` and Open-Meteo historical
   irradiance. Equivalent to "effective panel capacity"; subsumes panel
   area, efficiency, tilt, and partial shading.

2. **Load model** — averages output_w by (hour-of-day, weekday-vs-weekend)
   over recent samples. Captures fridge cycles, lighting, daily routines.

3. **SOC simulation** — walks forward hour by hour:
       SOC(t+1) = SOC(t) + (solar_w(t) - load_w(t)) * 1h / capacity_wh
   AC/grid charging is intentionally NOT modeled — the user is mostly
   solar, and predicted SOC dips are MORE alarming/actionable when grid
   charging is excluded. Add it later if needed.

Battery capacity defaults are hardcoded by Jackery model code; unknown
models fall back to 3024 Wh (HomePower 3000 size).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

log = logging.getLogger("forecaster")

# Battery capacity (Wh) by Jackery cloud model_code. The 5000 Plus reports
# under both 13 and 22 depending on firmware/region; both are the same cell
# stack (5040 Wh nominal). Unknown model_codes fall back to the smaller
# HomePower 3000 size so we don't over-promise SOC headroom.
BATTERY_CAPACITY_WH: dict[int, int] = {
    13: 5040,
    22: 5040,
}
DEFAULT_BATTERY_CAPACITY_WH = 3024

# Minimum paired (solar_w, ghi) samples required to trust a regression fit.
# Below this, fall back to a generic coefficient. 2 is the floor for a
# regression-through-origin (single point gives a fit but with no degrees
# of freedom for variance estimate). The SOLAR_RECENT_CAP_MULT cap below
# guards against runaway overprediction when the regression is noisy.
MIN_FIT_SAMPLES = 2

# Generic GHI-to-solar coefficient when we don't have enough data yet
# (assumes ~400W of panels at typical 80% derate). User-specific fits will
# replace this within a day or so of running.
DEFAULT_SOLAR_COEFF = 0.32

# Charge efficiency: not all solar Wh ends up as stored Wh. LiFePO4 chemistry
# + inverter/charger losses typically combine to ~5-10%. 0.90 is the
# conservative end so the simulator doesn't overpromise SOC headroom.
CHARGE_EFFICIENCY = 0.90

# Solar overshoot guard: cap forecast solar at this multiple of the device's
# recent (last 48h) observed peak. Prevents an overfit regression from
# predicting more solar than the array has ever produced; allows a modest
# headroom for clearer-sky days without runaway overprediction.
SOLAR_RECENT_CAP_MULT = 1.5

# Idle/standby load when an hour has no historical data of its own AND
# neighboring hours don't either. ~30W covers the device's own electronics
# + small always-on draws (LED indicators, USB hubs idle). Critically: NOT
# the global mean — that's what bled daytime activity into nighttime
# forecasts and caused the 44pp overnight error.
IDLE_LOAD_W = 30.0

# Inverter idle / DC-bus / parasitic overhead that the device's `op` (AC
# output) field doesn't capture. The advisor surfaced this as a 600-700W
# constant gap: SOC slope on a 30240 Wh nameplate implies ~1140W avg
# discharge over a 9h window where reported out_w avg was only ~460W.
# Adding this as a flat additive to every per-hour load lookup makes the
# forecaster's drain model match SOC reality. Conservative end of the
# observed 600-700W band; a slight under-estimate is preferable to over-
# predicting a 0% cliff.
IDLE_OVERHEAD_W = 600.0

# Cutoff for "recent" samples in load-profile recency weighting (seconds).
# Variable buckets (high IQR / median) weight samples newer than this 70%
# vs older 30%, so recent behavior shifts dominate without throwing away
# longer-term context entirely.
LOAD_RECENCY_S = 3 * 86400

# Threshold for treating a bucket as "variable" vs "stable". Buckets with
# relative-IQR (IQR / median) above this get recency-weighted; below it,
# we just use the median (stable hour, e.g. overnight idle).
LOAD_VARIABILITY_THRESHOLD = 0.5

# Per-bucket load ceiling, expressed as a multiplier of the *overall mean*
# load across history. With sparse histories (3-4 days), a single high-
# output event at e.g. 1pm yields a per-bucket median of 2.5 kW while the
# user's typical 1pm draw is 400W. The simulation then drains overnight
# on a phantom multi-kW daily run and 24h+ predictions snap to 0%.
# 2.0x overall mean keeps real daytime peaks (e.g. an HVAC cycle) but
# blocks single-event medians from dominating.
LOAD_BUCKET_CAP_MULT = 2.0


def battery_capacity_wh(model_code: int | None) -> int:
    if model_code is None:
        return DEFAULT_BATTERY_CAPACITY_WH
    return BATTERY_CAPACITY_WH.get(model_code, DEFAULT_BATTERY_CAPACITY_WH)


def expansion_pack_capacity_wh(model_code: int | None) -> int:
    """Per-pack capacity for a given main-unit model. The 5000 Plus uses
    5040 Wh expansion packs (same as the main unit); older 1500/2000-class
    units use 2042 Wh packs. Unknown models default to the main capacity
    since stacked packs of a different size are uncommon."""
    return battery_capacity_wh(model_code)


# ---------- solar regression ----------
def fit_solar_coefficient(
    energy_history: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
) -> tuple[float, int]:
    """Fit k where solar_w ≈ k * ghi_w_m2 using paired hourly samples.

    Returns (k, n_samples_used). Three regimes:
      • No positive solar readings in history → k = 0 (this device has no
        panels, or none we can detect — don't fabricate production).
      • Few pairs but evidence of solar → DEFAULT_SOLAR_COEFF (rough fit).
      • Enough pairs → least-squares regression against actual data.
    """
    # Bucket both series to the hour (epoch // 3600) and join.
    by_hour_solar: dict[int, float] = {}
    for row in energy_history:
        ts = int(row.get("ts") or 0)
        sol = float(row.get("solar_w") or 0)
        if ts <= 0:
            continue
        h = (ts // 3600) * 3600
        # If multiple buckets fell in the same hour, take max — captures the
        # peak rather than smearing it with the trailing zero edges.
        prev = by_hour_solar.get(h, 0.0)
        if sol > prev:
            by_hour_solar[h] = sol

    # If the device has produced essentially no solar in 14 days of history,
    # treat it as "no panels detected" rather than guessing with a default.
    # 50W threshold (not 0): the "ip - acip - cip" derivation produces a
    # few watts of sensor noise even when nothing is connected to the DC
    # bus. Real panels easily exceed 50W in midday sun, so this filters
    # out phantom readings without missing real (even small) arrays.
    if not any(v > 50 for v in by_hour_solar.values()):
        return 0.0, 0

    pairs: list[tuple[float, float]] = []
    for w in weather_hourly:
        h = (int(w.get("ts") or 0) // 3600) * 3600
        ghi = float(w.get("ghi_w_m2") or 0)
        if ghi <= 50:
            continue  # noise / dawn / dusk; coefficient unstable here
        sol = by_hour_solar.get(h)
        if sol is None or sol <= 0:
            continue
        pairs.append((ghi, sol))

    if len(pairs) < MIN_FIT_SAMPLES:
        return DEFAULT_SOLAR_COEFF, len(pairs)

    # Least-squares through origin: k = Σ(xy) / Σ(x²)
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    if sxx <= 0:
        return DEFAULT_SOLAR_COEFF, len(pairs)
    k = sxy / sxx
    # Sanity-clamp: a residential setup can plausibly hit 2-10 W per W/m²
    # (i.e. 1.6-8 kW panel array). Anything outside [0.05, 15] is almost
    # certainly garbage data — fall back.
    if k < 0.05 or k > 15.0:
        log.warning("solar coefficient %.3f out of plausible range; using default", k)
        return DEFAULT_SOLAR_COEFF, len(pairs)
    return k, len(pairs)


# ---------- load model ----------
def fit_load_profile(
    energy_history: list[dict[str, Any]],
    *,
    now_ts: float | None = None,
) -> dict[tuple[int, int], float]:
    """Per-hour load profile with stability-aware fitting.

    Pipeline:
      1. Cap each sample at global 95th percentile (kills one-off spikes).
      2. Bucket by (hour-of-day, weekend-flag), keeping the timestamp.
      3. For each bucket, compute median + IQR. If IQR/median is small
         (stable hour — typical for overnight idle), use the median. If
         large (variable hour — daytime activity), blend recent (last 3d
         at 70%) with older (30%) so the forecast follows recent shifts.
      4. Final per-bucket cap at LOAD_BUCKET_CAP_MULT x overall mean —
         protects against a single high-output event in a sparse history
         (e.g. running an oven once at 1pm) from claiming "1pm load is
         2.5kW every day". Without this, 18-24h-out predictions snap to
         0% as the simulation drains the battery on phantom loads.

    Hours absent from the dict get a neighbor-hour fallback in
    `expected_load_w`, NOT a global average — global average bleeds
    daytime activity into nighttime forecasts.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    recency_cutoff = now_ts - LOAD_RECENCY_S

    all_vals_raw = [
        float(r["output_w"]) for r in energy_history
        if r.get("output_w") is not None
    ]
    if not all_vals_raw:
        return {}
    all_vals = sorted(all_vals_raw)
    cap = all_vals[min(len(all_vals) - 1, int(len(all_vals) * 0.95))]
    overall_mean = sum(all_vals_raw) / len(all_vals_raw)
    # Per-bucket ceiling: never let a single hour claim more than this,
    # regardless of what the median computed. Floor at a sensible "device
    # is doing something" level so quiet histories still allow real
    # daytime activity through.
    bucket_ceiling = max(IDLE_LOAD_W * 6, LOAD_BUCKET_CAP_MULT * overall_mean)

    # bucket → list of (value, ts)
    buckets: dict[tuple[int, int], list[tuple[float, int]]] = {}
    for row in energy_history:
        ts = row.get("ts")
        out_w = row.get("output_w")
        if ts is None or out_w is None:
            continue
        v = min(float(out_w), cap)
        d = datetime.fromtimestamp(int(ts))
        key = (d.hour, 1 if d.weekday() >= 5 else 0)
        buckets.setdefault(key, []).append((v, int(ts)))

    profile: dict[tuple[int, int], float] = {}
    for key, samples in buckets.items():
        vals = sorted(v for v, _ in samples)
        n = len(vals)
        median = vals[n // 2]
        q25 = vals[n // 4]
        q75 = vals[(3 * n) // 4]
        iqr = q75 - q25
        rel_iqr = iqr / median if median > 0 else 0

        # Stable bucket OR too few samples to recency-weight reliably.
        if rel_iqr < LOAD_VARIABILITY_THRESHOLD or n < 6:
            profile[key] = min(median, bucket_ceiling)
            continue

        # Variable bucket: blend recent and older medians.
        recent = sorted(v for v, t in samples if t >= recency_cutoff)
        older = sorted(v for v, t in samples if t < recency_cutoff)
        if not recent:
            profile[key] = min(median, bucket_ceiling)
            continue
        recent_med = recent[len(recent) // 2]
        older_med = older[len(older) // 2] if older else recent_med
        blended = 0.7 * recent_med + 0.3 * older_med
        profile[key] = min(blended, bucket_ceiling)

    return profile


def expected_load_w(
    profile: dict[tuple[int, int], float],
    ts: int,
) -> float:
    """Look up the expected load for a forecast hour.

    Returns out_w_bucket + IDLE_OVERHEAD_W. The overhead term covers the
    inverter idle / DC-bus / balancing draw that doesn't show up in the
    `op` (AC output) field but does drain the battery — without it, the
    forecaster's load model systematically under-counts real discharge by
    ~600W (verified against SOC slope on a 30240 Wh nameplate).

    Fallback hierarchy when the (hour, weekend) bucket is empty:
      1. Same hour, opposite weekend-flag.
      2. Neighboring hours within ±3, same weekend-flag (preserves day/night).
      3. Neighboring hours within ±3, opposite weekend-flag.
      4. IDLE_LOAD_W (30W) — never the global mean.

    Step 2 is the critical fix: a missing 2am bucket should be guessed
    from 1am or 3am, NOT from the global daytime-skewed average.
    """
    d = datetime.fromtimestamp(ts)
    h = d.hour
    w = 1 if d.weekday() >= 5 else 0

    if (h, w) in profile:
        base = profile[(h, w)]
    elif (h, 1 - w) in profile:
        base = profile[(h, 1 - w)]
    else:
        base = None
        for delta in (1, -1, 2, -2, 3, -3):
            nh = (h + delta) % 24
            if (nh, w) in profile:
                base = profile[(nh, w)]
                break
        if base is None:
            for delta in (1, -1, 2, -2, 3, -3):
                nh = (h + delta) % 24
                if (nh, 1 - w) in profile:
                    base = profile[(nh, 1 - w)]
                    break
        if base is None:
            base = IDLE_LOAD_W
    return base + IDLE_OVERHEAD_W


# ---------- simulation ----------
def simulate_soc(
    starting_soc_pct: float,
    capacity_wh: int,
    forecast_hours: list[dict[str, Any]],
    *,
    ac_charge_floor_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Walk SOC forward through the forecast window.

    `forecast_hours` is a list of {ts, solar_w, load_w, cloud_cover_pct}.
    Output adds `predicted_soc` (clamped 0-100) per hour. Net positive
    inflow has CHARGE_EFFICIENCY applied; the simulator was previously
    over-predicting SOC by ignoring real-world charge losses.

    `ac_charge_floor_pct`: when set, simulate the user's smart-charge /
    Kasa-driven AC top-up — if SOC would drop below this floor in any
    hour, treat it as if the controller intervened and clamp at the
    floor. This was previously NOT modeled, which caused long-lead
    predictions (24h+) to saturate at 0% even though the real device
    was being grid-charged overnight by the smart-charge automation,
    and produced a persistent negative bias at short lead times. Pass
    None to keep the original "solar-only" behavior.
    """
    soc = max(0.0, min(100.0, float(starting_soc_pct)))
    floor = (max(0.0, min(100.0, float(ac_charge_floor_pct)))
             if ac_charge_floor_pct is not None else None)
    out: list[dict[str, Any]] = []
    for h in forecast_hours:
        solar = float(h.get("solar_w") or 0)
        load = float(h.get("load_w") or 0)
        # 1 hour interval, simple Euler step. Apply CHARGE_EFFICIENCY when
        # net inflow positive — discharge already accounts for inverter
        # losses on the load side.
        net = solar - load
        if net > 0:
            net *= CHARGE_EFFICIENCY
        soc += net / capacity_wh * 100.0
        soc = max(0.0, min(100.0, soc))
        # Smart-charge floor — the user has Kasa-driven grid top-up that
        # holds SOC at or above target_sunrise_soc_pct. Modeling it as a
        # hard floor undercounts how much grid energy is actually used
        # but cleanly addresses the "predicted 0% / actual 92%" cliff.
        if floor is not None and soc < floor:
            soc = floor
        out.append({**h, "predicted_soc": round(soc, 1)})
    return out


def build_forecast(
    energy_history: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
    starting_soc_pct: float,
    capacity_wh: int,
    now_ts: float | None = None,
    horizon_hours: int = 120,
    *,
    ac_charge_floor_pct: float | None = None,
) -> dict[str, Any]:
    """Glue: fit models + simulate. Returns a UI-ready dict.

    `ac_charge_floor_pct`: passed through to `simulate_soc`. Callers
    that have smart-charge enabled (and a target_sunrise_soc_pct) should
    pass it so long-lead predictions don't saturate at 0% — see
    `simulate_soc` docstring for the mechanism.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    cutoff = int(now_ts)

    k, n_fit = fit_solar_coefficient(energy_history, weather_hourly)
    profile = fit_load_profile(energy_history, now_ts=now_ts)
    out_vals = [r["output_w"] for r in energy_history if r.get("output_w") is not None]
    # Reported as a debug stat only — NOT used as a fallback for missing
    # hours. Mixing daytime samples into nighttime forecasts was the bug.
    overall_load = sum(out_vals) / len(out_vals) if out_vals else 0.0

    # Cap projected solar by the device's recent observed peak so an
    # overfit regression can't predict more solar than the array has
    # actually produced. SOLAR_RECENT_CAP_MULT leaves headroom for
    # clearer-sky days; falls back to None (no cap) when there's no
    # recent data to anchor against.
    recent_cutoff = cutoff - 48 * 3600
    recent_peak = max(
        (float(r.get("solar_w") or 0) for r in energy_history
         if r.get("ts") and r["ts"] >= recent_cutoff),
        default=0.0,
    )
    solar_cap = recent_peak * SOLAR_RECENT_CAP_MULT if recent_peak > 50 else None

    future = [w for w in weather_hourly if int(w.get("ts") or 0) >= cutoff]
    future = future[:horizon_hours]

    forecast_hours = []
    for w in future:
        ts = int(w["ts"])
        ghi = float(w.get("ghi_w_m2") or 0)
        solar_w = max(0.0, k * ghi)
        if solar_cap is not None:
            solar_w = min(solar_w, solar_cap)
        load_w = expected_load_w(profile, ts)
        forecast_hours.append({
            "ts": ts,
            "solar_w": round(solar_w, 1),
            "load_w": round(load_w, 1),
            "cloud_cover_pct": round(float(w.get("cloud_cover_pct") or 0), 1),
        })

    simulated = simulate_soc(
        starting_soc_pct, capacity_wh, forecast_hours,
        ac_charge_floor_pct=ac_charge_floor_pct,
    )
    return {
        "starting_soc_pct": round(starting_soc_pct, 1),
        "capacity_wh": capacity_wh,
        "solar_coefficient": round(k, 4),
        "fit_samples": n_fit,
        "overall_load_w": round(overall_load, 1),
        "forecast": simulated,
    }
