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
# Below this, fall back to a generic coefficient.
MIN_FIT_SAMPLES = 8

# Generic GHI-to-solar coefficient when we don't have enough data yet
# (assumes ~400W of panels at typical 80% derate). User-specific fits will
# replace this within a day or so of running.
DEFAULT_SOLAR_COEFF = 0.32


def battery_capacity_wh(model_code: int | None) -> int:
    if model_code is None:
        return DEFAULT_BATTERY_CAPACITY_WH
    return BATTERY_CAPACITY_WH.get(model_code, DEFAULT_BATTERY_CAPACITY_WH)


# ---------- solar regression ----------
def fit_solar_coefficient(
    energy_history: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
) -> tuple[float, int]:
    """Fit k where solar_w ≈ k * ghi_w_m2 using paired hourly samples.

    Returns (k, n_samples_used). Falls back to DEFAULT_SOLAR_COEFF if there
    aren't enough good pairs.
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
) -> dict[tuple[int, int], float]:
    """Average output_w by (hour-of-day, weekend-flag) over the input history.

    Returns dict keyed by (hour 0-23, weekend 0|1) → average watts. Hours
    not present in the history are absent — the simulator falls back to the
    overall average then.
    """
    sums: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    for row in energy_history:
        ts = row.get("ts")
        out_w = row.get("output_w")
        if ts is None or out_w is None:
            continue
        d = datetime.fromtimestamp(int(ts))
        key = (d.hour, 1 if d.weekday() >= 5 else 0)
        sums[key] = sums.get(key, 0.0) + float(out_w)
        counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}


def expected_load_w(
    profile: dict[tuple[int, int], float],
    overall_avg_w: float,
    ts: int,
) -> float:
    d = datetime.fromtimestamp(ts)
    key = (d.hour, 1 if d.weekday() >= 5 else 0)
    return profile.get(key, overall_avg_w)


# ---------- simulation ----------
def simulate_soc(
    starting_soc_pct: float,
    capacity_wh: int,
    forecast_hours: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk SOC forward through the forecast window.

    `forecast_hours` is a list of {ts, solar_w, load_w, cloud_cover_pct}.
    Output adds `predicted_soc` (clamped 0-100) per hour.
    """
    soc = max(0.0, min(100.0, float(starting_soc_pct)))
    out: list[dict[str, Any]] = []
    for h in forecast_hours:
        solar = float(h.get("solar_w") or 0)
        load = float(h.get("load_w") or 0)
        # 1 hour interval, simple Euler step
        delta_wh = solar - load
        soc += delta_wh / capacity_wh * 100.0
        soc = max(0.0, min(100.0, soc))
        out.append({**h, "predicted_soc": round(soc, 1)})
    return out


def build_forecast(
    energy_history: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
    starting_soc_pct: float,
    capacity_wh: int,
    now_ts: float | None = None,
    horizon_hours: int = 120,
) -> dict[str, Any]:
    """Glue: fit models + simulate. Returns a UI-ready dict."""
    now_ts = now_ts if now_ts is not None else time.time()
    cutoff = int(now_ts)

    k, n_fit = fit_solar_coefficient(energy_history, weather_hourly)
    profile = fit_load_profile(energy_history)
    out_vals = [r["output_w"] for r in energy_history if r.get("output_w") is not None]
    overall_load = sum(out_vals) / len(out_vals) if out_vals else 0.0

    future = [w for w in weather_hourly if int(w.get("ts") or 0) >= cutoff]
    future = future[:horizon_hours]

    forecast_hours = []
    for w in future:
        ts = int(w["ts"])
        ghi = float(w.get("ghi_w_m2") or 0)
        solar_w = max(0.0, k * ghi)
        load_w = expected_load_w(profile, overall_load, ts)
        forecast_hours.append({
            "ts": ts,
            "solar_w": round(solar_w, 1),
            "load_w": round(load_w, 1),
            "cloud_cover_pct": round(float(w.get("cloud_cover_pct") or 0), 1),
        })

    simulated = simulate_soc(starting_soc_pct, capacity_wh, forecast_hours)
    return {
        "starting_soc_pct": round(starting_soc_pct, 1),
        "capacity_wh": capacity_wh,
        "solar_coefficient": round(k, 4),
        "fit_samples": n_fit,
        "overall_load_w": round(overall_load, 1),
        "forecast": simulated,
    }
