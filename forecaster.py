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

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("forecaster")

# Battery capacity (Wh) by Jackery cloud model_code, loaded from
# `models.json` at module import. Keeping this catalog in a JSON file
# rather than a Python dict lets community contributors add new
# model_codes via PR without touching the simulator code. Per-device
# override (Device tab → capacity_wh_override) still beats the catalog.
def _load_model_catalog() -> dict[str, Any]:
    path = Path(__file__).parent / "models.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("models.json unreadable (%s); using built-in fallback", e)
        return {"default_capacity_wh": 3024, "models": {}}


_MODEL_CATALOG = _load_model_catalog()
DEFAULT_BATTERY_CAPACITY_WH = int(_MODEL_CATALOG.get("default_capacity_wh") or 3024)
BATTERY_CAPACITY_WH: dict[int, int] = {}
for _k, _v in (_MODEL_CATALOG.get("models") or {}).items():
    try:
        BATTERY_CAPACITY_WH[int(_k)] = int(_v["capacity_wh"])
    except (TypeError, ValueError, KeyError) as _e:
        log.warning("models.json: skipping bad entry %r=%r (%s)", _k, _v, _e)

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

# Default charge efficiency: not all solar Wh ends up as stored Wh.
# LiFePO4 chemistry + inverter/charger losses typically combine to
# ~5-10%. The default below is a conservative cold-start fallback.
# `fit_charge_efficiency()` recovers the per-user value from observed
# input_wh vs SOC gain on clean charging windows; that fitted value
# is what `simulate_soc()` actually uses on each forecast.
DEFAULT_CHARGE_EFFICIENCY = 0.90
# Back-compat alias — older tests / external imports keep working.
CHARGE_EFFICIENCY = DEFAULT_CHARGE_EFFICIENCY

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

# Inverter overhead modeled as a percentage of reported out_w rather
# than a flat watt constant. Modern LiFePO4 portable-power inverters
# (Jackery, Bluetti, EcoFlow et al.) advertise 90-95% AC efficiency,
# so ~10% of throughput is lost as heat / DC-bus draw / control
# electronics that don't show up in the `op` field. This scales
# correctly with load: heavy hours have more overhead than idle hours.
# Per-device fits override this default once data accumulates.
DEFAULT_INVERTER_OVERHEAD_PCT = 0.10
INVERTER_OVERHEAD_PCT = DEFAULT_INVERTER_OVERHEAD_PCT

# Back-compat aliases for any external code that imported the old
# flat-watt API. Computed from the percentage at a typical load
# (~500W) so the order of magnitude matches the previous default.
DEFAULT_IDLE_OVERHEAD_W = 50.0  # was 200 (flat); now 10pct of 500W typical
IDLE_OVERHEAD_W = DEFAULT_IDLE_OVERHEAD_W

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


# ---------- idle overhead ----------
def fit_inverter_overhead_pct(
    energy_history: list[dict[str, Any]],
    capacity_wh: int,
    *,
    default: float = DEFAULT_INVERTER_OVERHEAD_PCT,
    min_windows: int = 5,
) -> tuple[float, int]:
    """Fit the per-device inverter overhead PERCENTAGE by reconciling
    reported `op` against observed SOC slope on pure-discharge windows.

    Modern LiFePO4 inverters lose ~10% of throughput as heat in the
    DC→AC conversion — that share doesn't show up in `op` but does
    drain the battery. The exact percentage varies by inverter
    model, age, and operating temperature. We fit it from each user's
    own history rather than hard-coding.

    Algorithm: walk adjacent hourly buckets and keep only "clean
    discharge" windows where:
      - `solar_wh` is below a noise floor (no solar muddying SOC slope),
      - `ac_input_wh` is below the same floor (no Kasa-driven grid charge),
      - SOC actually dropped (≥1pp — the device's own sensor resolution),
      - reported out_w >= MIN_OUT_W (need throughput to compute a ratio).

    For each qualifying window:
      observed_drain_w = SOC_drop * capacity_wh / 100 / dt_h
      pct = (observed_drain_w - reported_out_w) / reported_out_w

    Median across windows for robustness; clamped to a physically
    plausible [0.0, 0.50] band (50% loss is the worst case for a
    decent inverter; anything higher is measurement error).

    Falls back to `default` (10%) when fewer than `min_windows`
    qualifying pairs are available.

    Returns (overhead_pct_decimal, n_windows_used). 0.10 means 10%.
    """
    SOLAR_NOISE_WH = 50.0   # noise floor below which we treat solar as 0
    AC_NOISE_WH = 50.0      # same for AC charging
    MIN_SOC_DROP_PCT = 1.0  # below this it's sensor jitter, not real drain
    MIN_OUT_W = 50.0        # need real throughput to compute a ratio

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    pcts: list[float] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        soc_a, soc_b = a.get("battery_pct"), b.get("battery_pct")
        if soc_a is None or soc_b is None:
            continue
        soc_drop = soc_a - soc_b
        if soc_drop < MIN_SOC_DROP_PCT:
            continue
        if (a.get("solar_wh") or 0) > SOLAR_NOISE_WH:
            continue
        if (a.get("ac_input_wh") or 0) > AC_NOISE_WH:
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h <= 0 or dt_h > 6.0:
            continue
        observed_drain_w = soc_drop * capacity_wh / 100.0 / dt_h
        reported_out_w = (a.get("output_wh") or 0) / dt_h
        if reported_out_w < MIN_OUT_W:
            continue
        pct = (observed_drain_w - reported_out_w) / reported_out_w
        # Negative ratio = SOC drained slower than out_w would imply
        # (sensor noise / pack-balancing artifact). Clamp to 0.
        if pct < 0:
            pct = 0.0
        pcts.append(pct)

    if len(pcts) < min_windows:
        return float(default), len(pcts)
    pcts.sort()
    median = pcts[len(pcts) // 2]
    # Sanity-clamp: above 50% loss is measurement error.
    if median > 0.50:
        return float(default), len(pcts)
    return float(median), len(pcts)


# Back-compat alias for callers / tests that import the old name.
# Same data, just returns a watt-equivalent at a typical 500W load.
def fit_idle_overhead_w(
    energy_history: list[dict[str, Any]],
    capacity_wh: int,
    *,
    default: float = DEFAULT_IDLE_OVERHEAD_W,
    min_windows: int = 5,
) -> tuple[float, int]:
    """Deprecated: use fit_inverter_overhead_pct. Kept so older imports
    don't break. Returns the proportional fit converted to watts at a
    typical 500W load."""
    pct, n = fit_inverter_overhead_pct(
        energy_history, capacity_wh, min_windows=min_windows,
    )
    return pct * 500.0, n


# ---------- observed AC charging rate ----------
# Local-time night band when solar production is physically zero. Used
# as a fallback discriminator for users whose cloud `acip` field is
# broken (returns 0 even during obvious AC charging — see the
# anomaly the AI advisor flagged on this codebase). 21:00-06:00 is
# conservative enough to clear all twilight contributions year-round.
_NIGHT_START_LOCAL_HOUR = 21
_NIGHT_END_LOCAL_HOUR = 6


def fit_max_charge_w(
    energy_history: list[dict[str, Any]],
    *,
    tz_offset_seconds: int = 0,
    weather_hourly: list[dict[str, Any]] | None = None,
    min_input_w: float = 100.0,
    min_samples: int = 6,
    percentile: float = 0.95,
    return_candidates: bool = False,
) -> tuple[float | None, int]:
    """Estimate the AC charging rate this device actually pulls from
    the wall — read from the user's own telemetry rather than guessed
    from the model_code. Returns (watts, n_samples_used).

    The 5000 Plus's documented modes (Standard 600W / Fast 1500W /
    Fast on dedicated 20A 1800W / Super-fast w/ STS 2400W) and similar
    tables for other models are user-configurable and accessory-
    dependent — there is no correct answer per model_code, only per
    deployment.

    Solar must be excluded. Two paths, in order of trust:
      1. The cloud reports `ac_input_w` (`acip`) classifying input as
         grid charging — when ≥ min_input_w we use it directly.
      2. When `acip` is 0 but input_w is high (the broken-cloud case
         the user has — see advisor anomaly), fall back to local-time
         night hours where solar is physically impossible. `input_w`
         during those hours must be AC charging via the smart-charge
         Kasa plug or similar.
    Daytime samples without a populated `acip` are skipped — solar vs
    grid can't be disentangled there.

    Solar exclusion (in priority order):
      a. `weather_hourly` GHI lookup at the sample's hour. If GHI < 50
         W/m² the sun is physically too low to produce, so any input
         must be from another source (AC plug). This is timezone-free
         and works for any user worldwide.
      b. Local-time night band [21:00, 06:00) using `tz_offset_seconds`
         as a fallback when no weather data is available.
      c. If neither check is conclusive (daytime, no weather), skip.

    `return_candidates=True` returns ([candidate_dicts], n_used)
    instead of (watts, n) — used by the Device-tab debug UI to
    inspect what samples the fit picked. Each dict carries
    {ts, input_w, ac_input_w, solar_w, ghi_w_m2, value_used, path}.
    """
    # Build a {hour_aligned_ts: ghi} index for fast lookup.
    ghi_by_hour: dict[int, float] = {}
    for w in (weather_hourly or []):
        try:
            wts = int(w.get("ts") or 0)
            wts -= wts % 3600  # align to hour
            ghi_by_hour[wts] = float(w.get("ghi_w_m2") or 0)
        except (TypeError, ValueError):
            continue
    GHI_DARK_THRESHOLD = 50.0

    candidates_w: list[float] = []
    candidates_dbg: list[dict] = []
    for r in (energy_history or []):
        try:
            in_w = float(r.get("input_w") or 0)
            ac_w = float(r.get("ac_input_w") or 0)
            solar_w = float(r.get("solar_w") or 0)
            ts = int(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        path = None
        value: float | None = None
        # Path 1: cloud's `acip` field is populated AND the device's
        # own solar reading is near-zero, so we can trust the grid
        # classification. (When `acip` is non-zero but solar_w is also
        # high, the cloud may be misclassifying — fall through.)
        if ac_w >= min_input_w and solar_w < min_input_w:
            path = "ac_input_w"
            value = ac_w
        elif in_w >= min_input_w and ts > 0:
            # Path 2a: GHI-based exclusion when we have weather data.
            # Required: GHI < threshold (sun physically below horizon).
            hour_ts = ts - (ts % 3600)
            ghi = ghi_by_hour.get(hour_ts)
            if ghi is not None:
                if ghi < GHI_DARK_THRESHOLD:
                    path = "input_w_dark_ghi"
                    value = in_w
                else:
                    path = "skipped_solar_possible"
            elif tz_offset_seconds != 0:
                # Path 2b: no weather, but we have a non-UTC timezone
                # so local-time night is meaningful. Also require
                # solar_w near zero — guards against the
                # acip-broken-and-solar-misclassified-as-input case
                # where in_w is high at night because the cloud lied.
                local_h = ((ts + tz_offset_seconds) // 3600) % 24
                is_night = (local_h >= _NIGHT_START_LOCAL_HOUR
                            or local_h < _NIGHT_END_LOCAL_HOUR)
                if not is_night:
                    path = "skipped_daytime_no_ghi"
                elif solar_w >= min_input_w:
                    # Cloud is claiming solar AT NIGHT. Either device
                    # has a phantom solar reading or some other input
                    # source. Either way, not AC; don't count.
                    path = "skipped_phantom_solar_at_night"
                else:
                    path = "input_w_night"
                    value = in_w
            else:
                # No weather AND no timezone → can't classify safely.
                # Better to under-fit (source=default) than to pull in
                # solar. Users who haven't set location will see this
                # until they configure it.
                path = "skipped_no_tz_no_ghi"
        if value is not None:
            candidates_w.append(value)
        if return_candidates and (value is not None or path is not None
                                   or in_w >= min_input_w):
            candidates_dbg.append({
                "ts": ts, "input_w": in_w, "ac_input_w": ac_w,
                "solar_w": solar_w,
                "ghi_w_m2": ghi_by_hour.get(ts - (ts % 3600)),
                "value_used": value, "path": path or "below_min_input",
            })
    if return_candidates:
        return candidates_dbg, len(candidates_w)  # type: ignore[return-value]
    if len(candidates_w) < min_samples:
        return None, len(candidates_w)
    candidates_w.sort()
    idx = min(len(candidates_w) - 1, int(len(candidates_w) * percentile))
    return float(candidates_w[idx]), len(candidates_w)


# ---------- charge efficiency ----------
def fit_charge_efficiency(
    energy_history: list[dict[str, Any]],
    capacity_wh: int,
    *,
    default: float = DEFAULT_CHARGE_EFFICIENCY,
    min_windows: int = 5,
) -> tuple[float, int]:
    """Fit the per-device charge efficiency by reconciling input_wh
    against the SOC gain it produced on clean charging windows.

    Charge efficiency varies between users: different inverter model,
    different battery chemistry/age, different ambient temperature all
    move it. Hard-coding 0.90 is a guess that's wrong for everyone
    except whoever set it.

    Algorithm: walk adjacent hourly buckets and keep windows where:
      - SOC actually rose (≥1pp — sensor resolution),
      - SOC at start < 95% (avoid the top-balance regime where charge
        tapers and the BMS reports input that isn't really stored),
      - SOC at end ≤ 99% (no clipping at the 100% ceiling),
      - input_wh ≥ 100Wh in the window (enough signal vs noise),
      - dt 1-6h (longer than that mixes regimes).

    For each qualifying window:
      stored_wh = ΔSOC% * capacity_wh / 100
      efficiency = stored_wh / input_wh

    Take the median across windows for robustness. Clamp to a sane
    physical range [0.50, 0.99] — anything outside that band is almost
    certainly bad data (sensor glitch or mis-attributed energy flow).
    Falls back to `default` when too few windows are available.

    Returns (efficiency, n_windows_used).
    """
    MIN_INPUT_WH = 100.0
    MIN_SOC_GAIN_PCT = 1.0
    MAX_START_SOC_PCT = 95.0   # below the top-balance taper regime
    MAX_END_SOC_PCT = 99.0     # below the 100% ceiling clip

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    effs: list[float] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        soc_a, soc_b = a.get("battery_pct"), b.get("battery_pct")
        if soc_a is None or soc_b is None:
            continue
        soc_gain = soc_b - soc_a
        if soc_gain < MIN_SOC_GAIN_PCT:
            continue
        if soc_a > MAX_START_SOC_PCT or soc_b > MAX_END_SOC_PCT:
            continue
        input_wh = a.get("input_wh") or 0
        if input_wh < MIN_INPUT_WH:
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h <= 0 or dt_h > 6.0:
            continue
        stored_wh = soc_gain * capacity_wh / 100.0
        eff = stored_wh / input_wh
        effs.append(eff)

    if len(effs) < min_windows:
        return float(default), len(effs)
    effs.sort()
    median = effs[len(effs) // 2]
    # Clamp to physically plausible range — outside this band is bad data.
    if median < 0.50 or median > 0.99:
        return float(default), len(effs)
    return float(median), len(effs)


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
    *,
    idle_overhead_w: float | None = None,
    inverter_overhead_pct: float | None = None,
) -> float:
    """Look up the expected load for a forecast hour.

    Returns base_load * (1 + inverter_overhead_pct). The overhead term
    represents the share of battery throughput that's lost as heat in
    the inverter's DC→AC conversion (modern LiFePO4 inverters are
    ~90% efficient → ~10% lost). Scales with load so heavy hours
    incur more overhead than idle hours — which matches reality
    better than the old flat-watts model.

    Pass `inverter_overhead_pct` from `fit_inverter_overhead_pct()` so
    it reflects the user's actual setup; `None` falls back to the
    population default `INVERTER_OVERHEAD_PCT` (0.10).

    `idle_overhead_w` is the back-compat kwarg. When provided, it's
    interpreted as additive watts on top of the proportional term —
    rare, only used by tests that pre-date the proportional model.

    Fallback hierarchy when the (hour, weekend) bucket is empty:
      1. Same hour, opposite weekend-flag.
      2. Neighboring hours within ±3, same weekend-flag (preserves day/night).
      3. Neighboring hours within ±3, opposite weekend-flag.
      4. IDLE_LOAD_W (30W) — never the global mean.
    """
    pct = (INVERTER_OVERHEAD_PCT if inverter_overhead_pct is None
           else float(inverter_overhead_pct))
    flat = float(idle_overhead_w or 0.0)
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
    return base * (1.0 + pct) + flat


# ---------- simulation ----------
def simulate_soc(
    starting_soc_pct: float,
    capacity_wh: int,
    forecast_hours: list[dict[str, Any]],
    *,
    ac_charge_floor_pct: float | None = None,
    charge_efficiency: float | None = None,
) -> list[dict[str, Any]]:
    """Walk SOC forward through the forecast window.

    `forecast_hours` is a list of {ts, solar_w, load_w, cloud_cover_pct}.
    Output adds `predicted_soc` (clamped 0-100) per hour. Net positive
    inflow is multiplied by `charge_efficiency` (None falls back to the
    population default `CHARGE_EFFICIENCY = 0.90`); pass the fitted
    per-user value from `fit_charge_efficiency()` for accuracy.

    `ac_charge_floor_pct`: when set, simulate the user's smart-charge /
    Kasa-driven AC top-up — if SOC would drop below this floor in any
    hour, treat it as if the controller intervened and clamp at the
    floor. This was previously NOT modeled, which caused long-lead
    predictions (24h+) to saturate at 0% even though the real device
    was being grid-charged overnight by the smart-charge automation,
    and produced a persistent negative bias at short lead times. Pass
    None to keep the original "solar-only" behavior.
    """
    eff = CHARGE_EFFICIENCY if charge_efficiency is None else float(charge_efficiency)
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
            net *= eff
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


# Minimum data required before we'll produce a forecast at all. A
# half-fit forecast on day 1 is worse than none — it's confidently
# wrong and the smart-charge controller would act on it.
MIN_FORECAST_HISTORY_HOURS = 24
MIN_FORECAST_IDLE_WINDOWS = 5


def forecast_readiness(
    energy_history: list[dict[str, Any]],
    capacity_wh: int,
) -> dict[str, Any]:
    """Decide whether we have enough per-device history to build a
    forecast users can trust. Returns a dict with `ready` + the metrics
    the gate evaluated, so the UI can show "8 of 24 hours captured"
    style progress instead of a binary unhelpful "not yet".

    Gates:
      1. ≥ MIN_FORECAST_HISTORY_HOURS of *span* between earliest and
         latest sample. A full diurnal cycle is the minimum to fit a
         useful solar coefficient and a per-hour load profile.
      2. ≥ MIN_FORECAST_IDLE_WINDOWS clean discharge windows so the
         parasitic overhead is fit from the user's own data, not the
         population default.

    A failed gate is not a bug — it's expected on a fresh install.
    """
    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    if len(rows) < 2:
        return {
            "ready": False,
            "reason": "no_history",
            "have_hours": 0.0,
            "needed_hours": MIN_FORECAST_HISTORY_HOURS,
            "have_idle_windows": 0,
            "needed_idle_windows": MIN_FORECAST_IDLE_WINDOWS,
        }
    span_hours = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0
    _, n_windows = fit_idle_overhead_w(energy_history, capacity_wh)
    ready = (
        span_hours >= MIN_FORECAST_HISTORY_HOURS
        and n_windows >= MIN_FORECAST_IDLE_WINDOWS
    )
    return {
        "ready": ready,
        "reason": "ready" if ready else "calibrating",
        "have_hours": round(span_hours, 1),
        "needed_hours": MIN_FORECAST_HISTORY_HOURS,
        "have_idle_windows": n_windows,
        "needed_idle_windows": MIN_FORECAST_IDLE_WINDOWS,
    }


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

    Returns `{ready: False, readiness: {...}, forecast: []}` when there
    isn't enough per-device history to fit a trustworthy model. Callers
    must check `ready` before consuming `forecast` — the smart-charge
    controller and the Forecast tab both gate on it.

    `ac_charge_floor_pct`: passed through to `simulate_soc`. Callers
    that have smart-charge enabled (and a target_sunrise_soc_pct) should
    pass it so long-lead predictions don't saturate at 0% — see
    `simulate_soc` docstring for the mechanism.
    """
    readiness = forecast_readiness(energy_history, capacity_wh)
    if not readiness["ready"]:
        return {
            "ready": False,
            "readiness": readiness,
            "capacity_wh": capacity_wh,
            "starting_soc_pct": round(starting_soc_pct, 1),
            "forecast": [],
        }
    now_ts = now_ts if now_ts is not None else time.time()
    cutoff = int(now_ts)

    k, n_fit = fit_solar_coefficient(energy_history, weather_hourly)
    profile = fit_load_profile(energy_history, now_ts=now_ts)
    # Per-device inverter overhead as a percentage of throughput
    # (modern inverters lose ~10% to heat in DC→AC conversion).
    # Fit from the user's own discharge history; falls back to 10%
    # default during the first ~24h of operation.
    overhead_pct, overhead_n = fit_inverter_overhead_pct(
        energy_history, capacity_wh,
    )
    # Per-device charge efficiency, same idea: fit from the ratio of
    # observed SOC gain to reported input_wh on clean charging windows.
    charge_eff, charge_eff_n = fit_charge_efficiency(energy_history, capacity_wh)
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
        load_w = expected_load_w(profile, ts, inverter_overhead_pct=overhead_pct)
        forecast_hours.append({
            "ts": ts,
            "solar_w": round(solar_w, 1),
            "load_w": round(load_w, 1),
            "cloud_cover_pct": round(float(w.get("cloud_cover_pct") or 0), 1),
        })

    simulated = simulate_soc(
        starting_soc_pct, capacity_wh, forecast_hours,
        ac_charge_floor_pct=ac_charge_floor_pct,
        charge_efficiency=charge_eff,
    )
    return {
        "ready": True,
        "readiness": readiness,
        "starting_soc_pct": round(starting_soc_pct, 1),
        "capacity_wh": capacity_wh,
        "solar_coefficient": round(k, 4),
        "fit_samples": n_fit,
        "overall_load_w": round(overall_load, 1),
        # Auto-fitted per-device inverter overhead as a fraction of
        # throughput. _n reports how many clean discharge windows the
        # fit used — when small, value is the 10% default.
        "inverter_overhead_pct": round(overhead_pct, 4),
        "inverter_overhead_n_windows": overhead_n,
        # Back-compat: report watt-equivalent at typical 500W load too.
        "idle_overhead_w": round(overhead_pct * 500.0, 1),
        "idle_overhead_n_windows": overhead_n,
        # Auto-fitted per-device charge efficiency (input_wh → stored_wh
        # ratio). Same n_windows convention as idle_overhead.
        "charge_efficiency": round(charge_eff, 3),
        "charge_efficiency_n_windows": charge_eff_n,
        "forecast": simulated,
    }
