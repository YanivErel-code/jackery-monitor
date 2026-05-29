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

import contextlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("forecaster")


# ---------- _row_soc telemetry ----------
# Lifetime counters tracking which SOC field _row_soc() actually walked
# on each call. Used by /api/diagnostics/row_soc to confirm whether
# multi-pack rigs are walking system_soc or silently falling back to
# main-pack battery_pct (the failure mode advisor flagged 2026-05-06).
_ROW_SOC_STATS: dict[str, Any] = {
    "system_soc_hits": 0,
    "battery_pct_fallbacks": 0,
    "none_returns": 0,
    "last_fit_at": None,
    "last_fit_caller": None,
    "last_fit_window": {
        "system_soc_hits": 0,
        "battery_pct_fallbacks": 0,
        "none_returns": 0,
    },
    "since_ts": int(time.time()),
}
_ROW_SOC_STATS_LOCK = threading.Lock()


@contextlib.contextmanager
def _row_soc_fit_window(caller_name: str):
    """Bracket a fit invocation so we can capture the per-fit deltas of
    _row_soc's system_soc vs battery_pct usage. Counters are global; we
    snapshot before/after under the lock to compute the window."""
    with _ROW_SOC_STATS_LOCK:
        before = {
            "system_soc_hits": _ROW_SOC_STATS["system_soc_hits"],
            "battery_pct_fallbacks": _ROW_SOC_STATS["battery_pct_fallbacks"],
            "none_returns": _ROW_SOC_STATS["none_returns"],
        }
    try:
        yield
    finally:
        with _ROW_SOC_STATS_LOCK:
            _ROW_SOC_STATS["last_fit_at"] = int(time.time())
            _ROW_SOC_STATS["last_fit_caller"] = caller_name
            _ROW_SOC_STATS["last_fit_window"] = {
                "system_soc_hits": (
                    _ROW_SOC_STATS["system_soc_hits"] - before["system_soc_hits"]
                ),
                "battery_pct_fallbacks": (
                    _ROW_SOC_STATS["battery_pct_fallbacks"]
                    - before["battery_pct_fallbacks"]
                ),
                "none_returns": (
                    _ROW_SOC_STATS["none_returns"] - before["none_returns"]
                ),
            }


def get_row_soc_stats() -> dict[str, Any]:
    """Snapshot of the _row_soc telemetry counters. Returned dict is a
    copy; the caller can mutate freely."""
    with _ROW_SOC_STATS_LOCK:
        return {
            "system_soc_hits": _ROW_SOC_STATS["system_soc_hits"],
            "battery_pct_fallbacks": _ROW_SOC_STATS["battery_pct_fallbacks"],
            "none_returns": _ROW_SOC_STATS["none_returns"],
            "last_fit_at": _ROW_SOC_STATS["last_fit_at"],
            "last_fit_caller": _ROW_SOC_STATS["last_fit_caller"],
            "last_fit_window": dict(_ROW_SOC_STATS["last_fit_window"]),
            "since_ts": _ROW_SOC_STATS["since_ts"],
        }


def _track_row_soc(caller_name: str):
    """Decorator for fit functions that call _row_soc(): captures the
    per-call delta of system_soc-vs-battery_pct usage so the diagnostics
    endpoint can show whether the most recent fit walked system SOC."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            with _row_soc_fit_window(caller_name):
                return fn(*args, **kwargs)
        wrapper.__wrapped__ = fn
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco

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

# Clear-sky filter for fit_solar_coefficient. Open-Meteo's
# `shortwave_radiation` is post-cloud all-sky GHI, so the same numeric
# GHI value can correspond to "low sun + clear" or "high sun + thick
# clouds", and panel output drops non-linearly with cloud opacity (a
# 70% cloudy sky is NOT 30% of clear output — it's typically 15-25%).
# Including cloudy samples in a least-squares fit pulls the slope below
# the clear-sky truth: the user's 5000+ rig hit 3700W actual peak but
# fit_solar_coefficient returned k=3.04 (predicting 2891W) because the
# regression averaged clear and overcast hours. Filter to bright +
# low-cloud pairs first to capture the panels' true GHI→W relationship,
# then fall back to the broader GHI>50 pool only if the clear-sky pool
# is too sparse for a stable fit.
CLEAR_SKY_GHI_THRESHOLD = 700.0
CLEAR_SKY_MAX_CLOUD_PCT = 30.0

# Diurnal shade / azimuth correction. A single coefficient k can't
# represent a site where production deviates from GHI by time of day —
# afternoon roof/tree shade, east/west panel azimuth, a horizon
# obstruction. fit_diurnal_shape learns a per-local-hour multiplicative
# factor s_h on top of k: predicted_solar = k * GHI * s_h. Each hour's
# factor is the MEDIAN of observed_solar / (k*GHI), shrunk toward 1.0 by
# sample count so sparse hours stay neutral. Cloud cancels in the ratio
# (both observed and GHI scale with it) so the factor isolates geometry,
# not weather. A fresh install with no history gets all-1.0 factors —
# identical to the pure k*GHI behavior. Nothing site-specific is
# hardcoded; the shape is entirely learned per device.
DIURNAL_SHAPE_PRIOR_STRENGTH = 4.0   # pseudo-samples pulling each hour toward 1.0
DIURNAL_MIN_GHI = 50.0               # only learn from real daylight hours
DIURNAL_RATIO_CLAMP = (0.05, 5.0)    # drop non-physical per-sample ratios (broken-cloud max-vs-mean spikes)

# Learned charge ceiling. Multi-pack rigs plateau below 100% system SOC
# because the weakest pack saturates first (its BMS stops accepting
# charge) while the capacity-weighted average is still well under full —
# observed ~78% on a 5-pack 5000+ even when the main unit reads 100%.
# fit_charge_ceiling learns the p95 of daily-max system SOC across days
# that actually ran a charge cycle; simulate_soc caps there instead of
# 100%. Cold start (too few charge days, or the system reaches near-full)
# → None → NO cap, so a fresh / balanced / mostly-cloudy install is never
# falsely limited. Nothing hardcoded; a balanced single unit learns 100.
CHARGE_CEILING_MIN_DAYS = 5               # need this many real charge days to trust a cap
CHARGE_CEILING_MIN_DAILY_RISE_PP = 20.0   # a "charge day" raised SOC at least this much
CHARGE_CEILING_NO_CAP_ABOVE = 97.0        # learned ceiling >= this → no meaningful cap (reaches full)

# SOC headroom filter for fit_solar_coefficient. When SOC is close to
# full, the BMS tapers the charging current to protect the pack — so
# even when GHI is at peak, the reported `solar_w` is the BMS-accepted
# value, not the panel's actual capability. The MPPT idles surplus.
# Including high-SOC hours in the fit therefore back-solves a lower k
# than the panels can really deliver. Symptom on the user's rig
# 2026-05-08: clear-sky moments showed k=4.0-4.2 from 3.6+kW peaks at
# ~900 W/m² GHI, but fit_solar_coefficient returned k=3.29 because
# many "clear-sky" fit hours had SOC ≥ 90% (sub-2kW absorbed despite
# bright sun). The first cut at 80% still left k stuck at 3.73 because
# advisor empirics on 2026-05-09 showed clear-sky k=4.3-4.7 even at
# SOC 76% — the BMS taper apparently begins biting well before 80%
# on this hardware. Tightened to 70% so we only fit during deep
# headroom hours where the MPPT is unambiguously running at panel
# capacity. 70% on a 30 kWh pack still leaves 9 kWh of room — plenty
# to soak a peak hour. Pair with: no AC charging (otherwise solar
# competes with AC for charge headroom and gets curtailed similarly).
SOLAR_FIT_MAX_SOC_PCT = 70.0
SOLAR_FIT_MAX_AC_INPUT_W = 50.0

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
# observed peak within SOLAR_CAP_HISTORY_S. Prevents an overfit regression
# from predicting more solar than the array has ever produced; allows
# headroom for clearer-sky days without runaway overprediction.
#
# History window started at 48h, but that was too tight: a recent cloudy
# stretch tanked `recent_peak`, and even when Open-Meteo correctly
# forecast bright sun for a future hour, k*GHI got clamped down by a
# cap derived purely from the prior weekend's clouds. The advisor flagged
# this on 2026-05-03: predictions made 5/1 for 5/3 saturated at the
# ac_charge floor (35%) while actuals hit 76-79%. Widening to 14 days
# means any clear-sky day in the prior fortnight sets the cap, and the
# multiplier 2.0 (was 1.5) gives extra headroom for genuinely brighter-
# than-recent days.
SOLAR_RECENT_CAP_MULT = 2.0
SOLAR_CAP_HISTORY_S = 14 * 86400

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

# Constant parasitic baseline (W). Captures BMS, idle inverter, and DC-bus
# draw on the MAIN unit (single-pack baseline). Pack-balancing draw is now
# tracked separately via PER_PACK_BASELINE_W below — see fit_drain_model.
# Fit per-device by `fit_drain_model`; the default is a conservative
# "small backup unit" baseline.
DEFAULT_PARASITIC_W = 50.0

# Per-expansion-pack BMS/balancing draw (W). Multi-pack LiFePO4 rigs add a
# roughly-constant baseline draw per pack — independent BMS, balancing
# current between cells, contactor coils. Empirically ~60W per pack on the
# 5000+ (5 packs ≈ 300W). The advisor flagged the gap on 2026-05-23: a
# clean 5h overnight window on the 5-pack rig showed parasitic ≈ 410W
# vs the fit's ≈ 40W, the gap being 5×60W ≈ 300W of per-pack contribution
# that fit_drain_model couldn't capture by taking the median of bimodal
# (quiet vs BMS-active nights) implied parasitics. Modeling it as a
# separate constant times pack_count attributes the right share to packs
# vs the main unit's parasitic_w, and the median across windows then
# captures the cleaner main-unit-only residual.
PER_PACK_BASELINE_W = 60.0


def per_pack_baseline_w(pack_count: int) -> float:
    """Total pack-side baseline draw for a rig with `pack_count` expansion
    packs. Returns 0 for single-unit devices (pack_count=0)."""
    return float(max(0, int(pack_count))) * PER_PACK_BASELINE_W

# Minimum p90/p10 load ratio to trust the joint OLS fit of (parasitic_w,
# overhead_pct). When loads are narrow — e.g. a device that runs the same
# steady ~470W every night — the (load, drain) regressor pair is nearly
# collinear and OLS can't separate intercept from slope, so the fit
# silently collapses into the priors. Below this ratio we fall back to
# a parasitic-only fit (overhead pinned at default).
#
# Uses 10th vs 90th percentile, NOT raw max/min: the original max/min
# version was fooled by a single outlier high-load window (one kettle
# run during a 14d history at otherwise-narrow ~460W loads pushes
# max/min above 2x even though 99% of windows are tightly clustered).
# Advisor caught this on 2026-05-05 ~12h after we shipped the original
# fallback — the deployed fit was still returning exactly the (50W,
# 0.10) cold-start defaults despite the load distribution being
# clearly narrow at the inner spread. p90/p10 ignores the outliers and
# correctly classifies the device as "narrow" → parasitic-only path
# fires.
MIN_LOAD_RANGE_FOR_JOINT_FIT = 2.0

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
#
# Historical: this used to default to 2.0x overall_mean. That clamped real
# morning peaks (~1500-2000W on solar/HVAC users) to 2x mean instead of
# their natural value, AND combined with the ratio-driven inverter
# overhead fit it produced suspiciously bimodal forecasts where every
# busy hour pinned to (mean*MULT) * (1+overhead). The new strategy uses
# the GLOBAL 95th percentile of output_w as the cap — that reflects what
# the device actually does on its busiest 5% of minutes, regardless of
# the mean. Outliers above p95 are already trimmed before bucketing, so
# the cap rarely bites for typical buckets.
#
# We keep LOAD_BUCKET_CAP_MULT for backward-compatibility tests but the
# main code path uses `cap` (the p95) as bucket_ceiling. See
# build_load_profile().
LOAD_BUCKET_CAP_MULT = 2.0

# Inside-bucket outlier trim: the fraction of samples to drop from EACH
# end before computing the per-bucket median. Protects against a few
# oven/AC-cycle-start spikes pulling a sparse bucket's median up — the
# old code took the raw median of all samples, which over a 14-day
# history with ~14 samples per bucket meant 2 oven runs could move the
# median by 600+W. With trim=0.10 we drop the top and bottom 10% before
# computing median. Skipped when n<5 (too few to trim safely).
LOAD_BUCKET_TRIM_PCT = 0.10

# Slope-based fits gate windows on a percentage-point (pp) SOC drop:
#   - battery_pct (single int from main): noise floor ~1pp from
#     quantization; require ≥2pp signal so noise can't dominate.
#   - system_soc (capacity-weighted across N packs): finer effective
#     resolution (~0.4pp on a 6-pack rig); ≥0.5pp is comparable S/N,
#     and 2pp would gate out almost every window since system SOC
#     drops 4-6× slower than main on multi-pack rigs.
#
# Earlier iterations also imposed an absolute Wh floor (100 Wh
# drain / 50 Wh gain / 150 Wh per run), but it turned out to be
# redundant on multi-pack rigs (the pp gate ALREADY translates to a
# bigger Wh threshold there — 0.5pp × 30240 Wh = 151 Wh) and
# over-strict on small single-unit devices (HP3K's typical 30 W load
# produced 60 Wh/h drain, which qualified under the pp gate but
# failed the 100 Wh floor — fit_windows collapsed to 0). Dropped.
INVERTER_FIT_MIN_SOC_DROP_PCT_MAIN = 2.0
INVERTER_FIT_MIN_SOC_DROP_PCT_SYSTEM = 0.5
# Charge-efficiency fit pp thresholds (parallel to the drain pair).
CHARGE_FIT_MIN_SOC_GAIN_PCT_MAIN = 1.0
CHARGE_FIT_MIN_SOC_GAIN_PCT_SYSTEM = 0.25
# Multi-hour clean-discharge runs (drain model's narrow-load fallback):
# longer dt makes the per-pp noise less critical, but the pp threshold
# still scales the gate with capacity correctly (3pp × 3024 Wh = 91 Wh
# minimum for HP3K runs, 3pp × 30240 = 907 Wh for 5000+ main, etc.).
MIN_RUN_SOC_DROP_PCT_MAIN = 3.0
MIN_RUN_SOC_DROP_PCT_SYSTEM = 0.5
MIN_RUN_DRAIN_WH = 150.0


def _row_has_system_soc(row: dict[str, Any]) -> bool:
    return row.get("system_soc") is not None


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

    Three-tier pair pool, in order of preference:
      1. headroom_pairs — clear sky AND SOC < 80% AND no AC charging.
         The cleanest fit: BMS isn't tapering, AC isn't competing for
         headroom, sky is bright. This captures the panel's actual
         capability ceiling.
      2. clear_sky_pairs — clear sky only. Used when there aren't
         enough headroom samples (e.g. user keeps SOC high most of
         the day). Better than nothing but may under-fit due to BMS
         taper biting on some hours.
      3. broad_pairs — any GHI > 50, any sky. Last-resort fallback
         for devices with persistently overcast histories.
    """
    # Bucket both series to the hour (epoch // 3600) and join.
    # Track SOC at hour start AND whether AC charging was active during
    # the hour, so we can filter out BMS-tapered samples below.
    by_hour_solar: dict[int, float] = {}
    by_hour_soc_min: dict[int, float] = {}   # min SOC seen in hour ≈ start
    by_hour_has_ac: dict[int, bool] = {}
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
        # Lowest SOC in the hour represents the "early" part — when BMS
        # taper hasn't kicked in yet (SOC rises during charging).
        soc = _row_soc(row)
        if soc is not None:
            prev_soc = by_hour_soc_min.get(h)
            if prev_soc is None or soc < prev_soc:
                by_hour_soc_min[h] = soc
        if float(row.get("ac_input_w") or 0) > SOLAR_FIT_MAX_AC_INPUT_W:
            by_hour_has_ac[h] = True

    # If the device has produced essentially no solar in 14 days of history,
    # treat it as "no panels detected" rather than guessing with a default.
    # 50W threshold (not 0): the "ip - acip - cip" derivation produces a
    # few watts of sensor noise even when nothing is connected to the DC
    # bus. Real panels easily exceed 50W in midday sun, so this filters
    # out phantom readings without missing real (even small) arrays.
    if not any(v > 50 for v in by_hour_solar.values()):
        return 0.0, 0

    # Build three pools: headroom (cleanest), clear-sky (bright but may
    # include BMS-taper hours), and broad (last-resort fallback).
    headroom_pairs: list[tuple[float, float]] = []
    clear_sky_pairs: list[tuple[float, float]] = []
    broad_pairs: list[tuple[float, float]] = []
    for w in weather_hourly:
        h = (int(w.get("ts") or 0) // 3600) * 3600
        ghi = float(w.get("ghi_w_m2") or 0)
        if ghi <= 50:
            continue  # noise / dawn / dusk; coefficient unstable here
        sol = by_hour_solar.get(h)
        if sol is None or sol <= 0:
            continue
        broad_pairs.append((ghi, sol))
        cloud = float(w.get("cloud_cover_pct") or 0)
        is_clear_sky = (ghi >= CLEAR_SKY_GHI_THRESHOLD
                        and cloud <= CLEAR_SKY_MAX_CLOUD_PCT)
        if is_clear_sky:
            clear_sky_pairs.append((ghi, sol))
            soc_at_h = by_hour_soc_min.get(h)
            has_ac = by_hour_has_ac.get(h, False)
            if (soc_at_h is not None
                    and soc_at_h <= SOLAR_FIT_MAX_SOC_PCT
                    and not has_ac):
                headroom_pairs.append((ghi, sol))

    if len(headroom_pairs) >= MIN_FIT_SAMPLES:
        pairs = headroom_pairs
    elif len(clear_sky_pairs) >= MIN_FIT_SAMPLES:
        pairs = clear_sky_pairs
    else:
        pairs = broad_pairs

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


def fit_diurnal_shape(
    energy_history: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
    k: float,
    utc_offset_seconds: int = 0,
) -> dict[int, float]:
    """Learn a per-local-hour-of-day multiplicative correction on the
    global solar coefficient `k`, capturing site geometry (afternoon
    shade, panel azimuth, horizon obstructions) that a single coefficient
    cannot represent.

    Returns ``{local_hour_0_23: factor}``. A forecast hour's solar
    becomes ``k * GHI * factor[local_hour]``. Hours with no history
    default to 1.0, so a fresh install reproduces the pure ``k*GHI``
    behavior exactly — nothing site-specific is baked in.

    Each hour's factor is the MEDIAN of ``observed_solar / (k*GHI)`` over
    the history, shrunk toward 1.0 by the sample count so sparse hours
    stay conservative::

        factor_h = (n_h * median_h + PRIOR) / (n_h + PRIOR)

    Median (not mean) ignores broken-cloud spikes where the per-hour MAX
    solar is paired with a per-hour MEAN GHI. Cloud cancels in the ratio
    (numerator and denominator both scale with it) so the factor reflects
    geometry, not weather. ``k <= 0`` (no panels detected) → all 1.0.

    `utc_offset_seconds` buckets history into the device's LOCAL hour of
    day; the same offset is used when the forecast applies the factor, so
    even a cold-start offset of 0 stays self-consistent."""
    shape = {h: 1.0 for h in range(24)}
    if k <= 0:
        return shape
    # Per-hour MAX solar, consistent with fit_solar_coefficient's basis.
    by_hour_solar: dict[int, float] = {}
    for row in energy_history:
        ts = int(row.get("ts") or 0)
        if ts <= 0:
            continue
        h = (ts // 3600) * 3600
        sol = float(row.get("solar_w") or 0)
        if sol > by_hour_solar.get(h, 0.0):
            by_hour_solar[h] = sol
    lo, hi = DIURNAL_RATIO_CLAMP
    ratios: dict[int, list[float]] = {}
    for w in weather_hourly:
        h = (int(w.get("ts") or 0) // 3600) * 3600
        ghi = float(w.get("ghi_w_m2") or 0)
        if ghi <= DIURNAL_MIN_GHI:
            continue
        sol = by_hour_solar.get(h)
        if sol is None or sol <= 0:
            continue
        predicted = k * ghi
        if predicted <= 0:
            continue
        ratio = sol / predicted
        if ratio < lo or ratio > hi:
            continue
        lhod = ((h + int(utc_offset_seconds)) % 86400) // 3600
        ratios.setdefault(lhod, []).append(ratio)
    for lhod, rs in ratios.items():
        rs.sort()
        n = len(rs)
        median = rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2.0
        shape[lhod] = ((n * median + DIURNAL_SHAPE_PRIOR_STRENGTH)
                       / (n + DIURNAL_SHAPE_PRIOR_STRENGTH))
    return shape


# ---------- idle overhead ----------
def _row_soc(row: dict[str, Any]) -> float | None:
    """Slope-based fits below want capacity-weighted system SOC when
    available (multi-pack rigs) and main `battery_pct` otherwise. The
    history extension in energy_db.history() adds `system_soc` per row
    when capacity hints are passed; absent that field, fall back to
    `battery_pct` so single-unit devices and legacy callers still work.

    The two values diverge on multi-pack devices: main-pack SOC drains
    4-6× faster than system SOC before the BMS rebalances, so any
    slope-based fit that walks main but multiplies by system capacity
    over-attributes drain by the pack ratio. Advisor flagged this as
    the root cause of parasitic_w fitting at 316-370W vs the empirical
    ~130W on the 30240 Wh / 6-pack rig 2026-05-06."""
    s = row.get("system_soc")
    if s is not None:
        with _ROW_SOC_STATS_LOCK:
            _ROW_SOC_STATS["system_soc_hits"] += 1
        return float(s)
    p = row.get("battery_pct")
    if p is not None:
        with _ROW_SOC_STATS_LOCK:
            _ROW_SOC_STATS["battery_pct_fallbacks"] += 1
        return float(p)
    with _ROW_SOC_STATS_LOCK:
        _ROW_SOC_STATS["none_returns"] += 1
    return None


@_track_row_soc("fit_inverter_overhead_pct")
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
    MIN_OUT_W = 50.0        # need real throughput to compute a ratio

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    # Collect raw signed ratios. We do NOT clamp negatives per-sample
    # anymore — that produced an asymmetric bias when the cloud's SOC
    # quantization (1pp integer steps) symmetrically jittered the
    # implied drain around the truth. Clamping per-sample turned random
    # symmetric noise into a one-sided positive bias — every "this
    # window looked too efficient" sample became 0.0, while every "this
    # window looked too lossy" sample kept its full positive value.
    # Median-of-signed-ratios eliminates the bias. We still clamp
    # NEGATIVE final medians to 0 (a real device can't have negative
    # overhead) but only at the end.
    pcts: list[float] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        soc_a, soc_b = _row_soc(a), _row_soc(b)
        if soc_a is None or soc_b is None:
            continue
        soc_drop = soc_a - soc_b
        # pp threshold scales with capacity_wh implicitly (pp × cap = Wh)
        # so it's the device-agnostic noise gate.
        min_pp = (INVERTER_FIT_MIN_SOC_DROP_PCT_SYSTEM
                  if _row_has_system_soc(a) and _row_has_system_soc(b)
                  else INVERTER_FIT_MIN_SOC_DROP_PCT_MAIN)
        if soc_drop < min_pp:
            continue
        if (a.get("solar_wh") or 0) > SOLAR_NOISE_WH:
            continue
        if (a.get("ac_input_wh") or 0) > AC_NOISE_WH:
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        # Require >= 30 min so dt errors don't dominate; cap at 6h so
        # we don't reach across overnight gaps where SOC could have
        # changed via untracked sources.
        if dt_h < 0.5 or dt_h > 6.0:
            continue
        observed_drain_w = soc_drop * capacity_wh / 100.0 / dt_h
        reported_out_w = (a.get("output_wh") or 0) / dt_h
        if reported_out_w < MIN_OUT_W:
            continue
        pct = (observed_drain_w - reported_out_w) / reported_out_w
        pcts.append(pct)

    if len(pcts) < min_windows:
        return float(default), len(pcts)
    pcts.sort()
    median = pcts[len(pcts) // 2]
    # Final clamps:
    #  - Negative median = device's reported out_w consistently
    #    overstates real drain, which is implausible — fall back.
    #  - >50% loss = measurement error; fall back.
    if median < 0.0 or median > 0.50:
        return float(default), len(pcts)
    return float(median), len(pcts)


@_track_row_soc("fit_drain_model")
def fit_drain_model(
    energy_history: list[dict[str, Any]],
    capacity_wh: int,
    *,
    default_parasitic_w: float = DEFAULT_PARASITIC_W,
    default_overhead_pct: float = DEFAULT_INVERTER_OVERHEAD_PCT,
    min_windows: int = 5,
    pack_count: int = 0,
) -> tuple[float, float, int]:
    """Joint 2-parameter fit of the drain model::

        drain_w ≈ parasitic_w + (pack_count * PER_PACK_BASELINE_W)
                 + load_w * (1 + overhead_pct)

    The pure-percentage model (`fit_inverter_overhead_pct`) systematically
    misses the steady-state baseline draw — main-unit BMS, idle inverter,
    DC-bus losses. The advisor flagged a 430W unaccounted gap on the
    user's 5000+ on 2026-05-04 that boiled down to exactly this model
    limitation, AND a residual gap on 2026-05-23 that turned out to be
    PER-PACK BMS/balancing scaling with the expansion pack count.

    `pack_count` (default 0) is the number of expansion packs on this
    device. When > 0, we SUBTRACT `pack_count * PER_PACK_BASELINE_W`
    from each window's observed drain BEFORE attributing the residual
    to (parasitic_w, overhead_pct). That way the fit's parasitic_w
    captures the MAIN UNIT's residual only, and the per-pack
    contribution is attributed cleanly as a separate constant term
    that build_forecast adds back when feeding the simulator.

    Returns ``(parasitic_w, overhead_pct, n_windows)``. Falls back to
    ``(default_parasitic_w, default_overhead_pct, n)`` when the fit
    can't converge or produces implausible coefficients (negative
    parasitic, >1000W parasitic, negative overhead, >50% overhead).

    When the user's load distribution is narrow (max/min ratio below
    `MIN_LOAD_RANGE_FOR_JOINT_FIT`), the joint OLS regression is
    ill-conditioned — the (load, drain) pairs are nearly collinear and
    the solver collapses to the priors. In that case we pin
    `overhead_pct` at the default and fit `parasitic_w` alone via the
    median of `drain_i - load_i * (1 + default_pct)` per window. This
    is what the advisor flagged on 2026-05-05 after we shipped the
    initial joint fit.

    Window-selection gates mirror `fit_inverter_overhead_pct` exactly;
    keep them in sync with `diagnose_idle_windows` if you change either.
    """
    # Tightened from 50 → 20 Wh per advisor 2026-05-10T16:51: even
    # small input ramps (e.g. dawn solar trickle, AC plug bouncing on
    # for a minute) confound the slope. 20 Wh = ~20W average, well
    # below the device's signal floor.
    SOLAR_NOISE_WH = 20.0
    AC_NOISE_WH = 20.0
    MIN_OUT_W = 50.0
    # Only fit on windows that start near full (>85% SOC). Empirical
    # finding 2026-05-13: on multi-pack LiFePO4 rigs, the system_soc
    # reading is unreliable in mid-discharge (~30-80%) because the
    # flat voltage curve forces the BMS to fall back on coulomb
    # counting + periodic recalibration, producing apparent SOC
    # "drops" that don't match actual energy delivered. Pack-output
    # data dumped during the 2026-05-13 investigation showed packs
    # delivering ~430W in both heavy-drain (drain/load=1.80) and
    # near-full (drain/load=1.17) windows — only the SOC reading
    # differed. Windows starting >85% SOC pass through the reliable
    # high-voltage region of the curve and produce a consistent
    # drain/load ratio matching the empirical reconciliation.
    #
    # Earlier code had an UPPER bound (>95% rejected for "BMS taper")
    # — that's now dropped because the data shows windows starting
    # at 96-98% give the same well-behaved ratio (1.19-1.21) as 85-
    # 95% starts. The original 316W parasitic noted on 2026-05-10
    # turned out to be the SOC non-linearity issue, not BMS taper.
    MIN_FIT_START_SOC_PCT = 85.0

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    # Detect whether this device has ANY system_soc data. If yes, it's
    # a multi-pack rig and we MUST require system_soc on every fit
    # window — falling back to battery_pct (main-pack only) on rows
    # that happen to lack pack snapshots reintroduces the pack-ratio
    # bias we fixed in eee1228. The advisor flagged on 2026-05-11 that
    # the 414W fitted parasitic was suspiciously close to the old
    # pre-fix main-pct-biased range (316-370W); confirmed the
    # contamination by checking: if even a handful of fit windows fall
    # back to main_pct ×system_capacity, their inflated drain pulls
    # the median up. Single-unit devices have no pack data → this gate
    # never fires for them.
    require_system_soc = any(_row_has_system_soc(r) for r in rows)
    pack_baseline_w = per_pack_baseline_w(pack_count)
    pairs: list[tuple[float, float]] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if require_system_soc and not (
            _row_has_system_soc(a) and _row_has_system_soc(b)
        ):
            continue
        soc_a, soc_b = _row_soc(a), _row_soc(b)
        if soc_a is None or soc_b is None:
            continue
        if soc_a < MIN_FIT_START_SOC_PCT:
            continue
        soc_drop = soc_a - soc_b
        # pp gate (same as fit_inverter_overhead_pct).
        min_pp = (INVERTER_FIT_MIN_SOC_DROP_PCT_SYSTEM
                  if _row_has_system_soc(a) and _row_has_system_soc(b)
                  else INVERTER_FIT_MIN_SOC_DROP_PCT_MAIN)
        if soc_drop < min_pp:
            continue
        if (a.get("solar_wh") or 0) > SOLAR_NOISE_WH:
            continue
        if (a.get("ac_input_wh") or 0) > AC_NOISE_WH:
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h < 0.5 or dt_h > 6.0:
            continue
        observed_drain_w = soc_drop * capacity_wh / 100.0 / dt_h
        reported_load_w = (a.get("output_wh") or 0) / dt_h
        if reported_load_w < MIN_OUT_W:
            continue
        # Subtract the per-pack constant before attributing the rest to
        # (parasitic_w, overhead_pct). Clamp to 0 so a noisy window
        # where pack baseline > observed drain doesn't go negative and
        # bias the fit downward.
        adjusted_drain_w = max(0.0, observed_drain_w - pack_baseline_w)
        pairs.append((reported_load_w, adjusted_drain_w))

    n = len(pairs)
    if n < min_windows:
        return float(default_parasitic_w), float(default_overhead_pct), n

    # Robust load-range metric: 10th vs 90th percentile of load values,
    # so a single outlier high-load window can't disable the narrow-
    # distribution fallback. See MIN_LOAD_RANGE_FOR_JOINT_FIT comment.
    sorted_loads = sorted(p[0] for p in pairs)
    p10_idx = max(0, int(n * 0.10))
    p90_idx = min(n - 1, int(n * 0.90))
    load_p10 = sorted_loads[p10_idx]
    load_p90 = sorted_loads[p90_idx]
    load_range_ratio = (load_p90 / load_p10) if load_p10 > 0 else 1.0

    # Narrow-load fallback: pin overhead at the default and fit
    # parasitic_w from MULTI-HOUR clean-discharge runs (not adjacent
    # 1h pairs). The 1h pairs were biased low by SOC quantization
    # noise — when the true rate is ~2.8pp/h, the integer-pp readings
    # randomly produce 2pp drops (-30% under-count on observed drain)
    # roughly half the time, and the median across pairs collapsed
    # toward those low samples. Multi-hour runs accumulate enough
    # SOC drop that ±1pp quantization noise is small relative to the
    # signal. Advisor flagged this 2026-05-05T18:57 — fitted parasitic
    # was 103.8 W vs the reconciled-truth ~385 W.
    if load_range_ratio < MIN_LOAD_RANGE_FOR_JOINT_FIT:
        run_triples = _clean_discharge_runs(
            rows, capacity_wh,
            require_system_soc=require_system_soc,
        )
        # Plain median over runs ≥ 4h. The dt² weighting we tried on
        # 5/9 over-fit to whichever single run was longest (5/8's 9h
        # overnight implied 321W parasitic; 5/9's comparable run
        # implied ~50W). Different nights have genuinely different
        # parasitic — BMS rebalancing activity, ambient temperature,
        # pack thermal management — and dt² treated them as if they
        # were noisy measurements of the same value. Plain median is
        # the right combiner for night-to-night-varying signal: it
        # gives the typical-night parasitic. The ≥4h length filter
        # keeps quality control: short 2-3h runs have 1/dt² more
        # quantization noise so they don't get to vote on the typical
        # value. If too few long runs, fall back to all qualifying
        # runs (rare on a device with daily overnight discharges).
        long_runs = [t for t in run_triples if t[2] >= 4.0]
        median_pool = long_runs if len(long_runs) >= 2 else run_triples
        if len(median_pool) >= 2:
            # Subtract pack baseline from observed drain before
            # attributing the rest to (parasitic + load*overhead). Same
            # reasoning as the joint-fit path above: cleanly separate
            # main-unit parasitic from per-pack contribution.
            implied = sorted(
                max(0.0, d - pack_baseline_w) - load * (1.0 + default_overhead_pct)
                for load, d, _ in median_pool
            )
            parasitic_w = implied[len(implied) // 2]
            # Clamp negative results to 0 rather than falling through
            # to the noisier per-pair median. Implied parasitic going
            # negative on this device means the metered out_w already
            # includes inverter conversion losses (i.e. it's AC-side,
            # not DC-side) — adding the +10% overhead on top
            # double-counts. Advisor 2026-05-10T16:51: clean overnight
            # windows show implied parasitic of -125W to +27W. Until
            # we ship a per-device overhead correction, clamp to 0
            # so the simulator predicts roughly load×1.10 of drain
            # rather than load×1.10 + 316W. Over-prediction by ~10%
            # is preferable to over-prediction by ~80%.
            if parasitic_w < 0:
                return 0.0, float(default_overhead_pct), len(median_pool)
            if parasitic_w <= 1000:
                return float(parasitic_w), float(default_overhead_pct), len(median_pool)
        # Run-based fit didn't produce enough samples — last-resort
        # fallback to the per-pair median (noisier but better than
        # leaving the user on cold-start defaults indefinitely).
        implied_parasitics = sorted(
            d - load * (1.0 + default_overhead_pct) for load, d in pairs
        )
        parasitic_w = implied_parasitics[len(implied_parasitics) // 2]
        if parasitic_w < 0:
            return 0.0, float(default_overhead_pct), n
        if parasitic_w > 1000:
            return float(default_parasitic_w), float(default_overhead_pct), n
        return float(parasitic_w), float(default_overhead_pct), n

    # Ordinary least-squares: y = a + b * x where y=drain_w, x=load_w.
    # parasitic_w = a, overhead_pct = b - 1.
    sum_x = sum(x for x, _ in pairs)
    sum_y = sum(y for _, y in pairs)
    sum_xy = sum(x * y for x, y in pairs)
    sum_xx = sum(x * x for x, _ in pairs)
    denom = n * sum_xx - sum_x * sum_x
    if denom <= 0:
        # Degenerate: all loads identical (defensive — the load-range
        # gate above should already catch this).
        return float(default_parasitic_w), float(default_overhead_pct), n
    b_coef = (n * sum_xy - sum_x * sum_y) / denom
    a_coef = (sum_y - b_coef * sum_x) / n

    parasitic_w = a_coef
    overhead_pct = b_coef - 1.0

    # Plausibility clamps — fall back to defaults when either coefficient
    # leaves the physically-reasonable band. Negative parasitic implies the
    # battery gains energy at idle; >1000W on a single device is a sensor
    # fault; >50% overhead is a measurement-error band per the original
    # percentage-model rationale.
    if parasitic_w < 0 or parasitic_w > 1000:
        return float(default_parasitic_w), float(default_overhead_pct), n
    if overhead_pct < 0 or overhead_pct > 0.50:
        return float(default_parasitic_w), float(default_overhead_pct), n
    return float(parasitic_w), float(overhead_pct), n


def _clean_discharge_runs(
    sorted_rows: list[dict[str, Any]],
    capacity_wh: int,
    *,
    require_system_soc: bool = False,
) -> list[tuple[float, float, float]]:
    """Walk consecutive clean-discharge buckets (no solar, no AC charge,
    SOC available) and emit one `(avg_load_w, observed_drain_w, dt_h)`
    triple per run. Only runs that span ≥ MIN_RUN_HOURS with
    ≥ MIN_RUN_SOC_DROP qualify, so quantization noise (±1pp on each
    end of the run) is small relative to the observed drop.

    Returns dt_h alongside the load+drain pair so callers can
    length-weight the run when computing a parasitic estimate. Long
    runs (e.g. 9h overnight) have far less quantization noise than
    short 2h runs — quantization noise on system_soc is ~0.5pp, which
    translates to ~151/dt_h W of drain noise per run. Variance scales
    as 1/dt², so the optimal Bayesian weight when combining estimates
    is dt² (or simply favoring longer runs). Without this, the median
    gives equal weight to noisy short runs and pulls the parasitic
    estimate below the truth that long runs reveal.

    Used by `fit_drain_model`'s narrow-load fallback when the per-pair
    median was biased low by short-window quantization rounding.
    Reconciliation math from advisor 2026-05-05T18:57: a true ~2.8pp/h
    drain quantized to 2pp gives 605 W observed (vs 847 W true), and
    that low cluster pulled the per-pair median to ~100 W parasitic
    even though reality was ~385 W. Aggregating across 4-6h runs
    eliminates the bias.
    """
    # Tightened in lockstep with fit_drain_model — 20 Wh signal floor.
    SOLAR_NOISE_WH = 20.0
    AC_NOISE_WH = 20.0
    MIN_RUN_HOURS = 2.0
    MIN_RUN_AVG_LOAD_W = 50.0
    MAX_RUN_HOURS = 12.0  # don't reach across overnight gaps
    MAX_BUCKET_GAP_H = 1.5  # break a run if poll dropped > 90 min
    # Same start-SOC floor as fit_drain_model (kept in sync). Multi-pack
    # LiFePO4 SOC reading is unreliable in mid-discharge (<85%) because
    # of the flat voltage curve — see fit_drain_model's docstring for
    # the 2026-05-13 pack-output investigation that established this.
    MIN_RUN_START_SOC_PCT = 85.0

    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_ts: float | None = None
    for row in sorted_rows:
        # When require_system_soc, exclude rows missing pack data —
        # otherwise _row_soc falls back to battery_pct, reintroducing
        # pack-ratio bias on multi-pack rigs.
        has_soc = (_row_has_system_soc(row) if require_system_soc
                   else _row_soc(row) is not None)
        is_clean = (
            (row.get("solar_wh") or 0) <= SOLAR_NOISE_WH
            and (row.get("ac_input_wh") or 0) <= AC_NOISE_WH
            and has_soc
        )
        ts = row.get("ts")
        gap_too_large = (
            last_ts is not None
            and ts is not None
            and (ts - last_ts) / 3600.0 > MAX_BUCKET_GAP_H
        )
        if not is_clean or gap_too_large:
            if current:
                runs.append(current)
            current = []
        if is_clean:
            current.append(row)
            last_ts = ts
        else:
            last_ts = None
    if current:
        runs.append(current)

    out: list[tuple[float, float, float]] = []
    for run in runs:
        if len(run) < 2:
            continue
        a, b = run[0], run[-1]
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h < MIN_RUN_HOURS or dt_h > MAX_RUN_HOURS:
            continue
        soc_a, soc_b = _row_soc(a), _row_soc(b)
        if soc_a is None or soc_b is None:
            continue
        if soc_a < MIN_RUN_START_SOC_PCT:
            continue
        soc_drop = soc_a - soc_b
        min_pp = (MIN_RUN_SOC_DROP_PCT_SYSTEM
                  if _row_has_system_soc(a) and _row_has_system_soc(b)
                  else MIN_RUN_SOC_DROP_PCT_MAIN)
        if soc_drop < min_pp:
            continue
        observed_drain_w = soc_drop * capacity_wh / 100.0 / dt_h
        # Sum output_wh across the leading buckets — the trailing one
        # is the SOC reading at run end and shouldn't double-count.
        total_out_wh = sum(r.get("output_wh") or 0 for r in run[:-1])
        avg_load_w = total_out_wh / dt_h
        if avg_load_w < MIN_RUN_AVG_LOAD_W:
            continue
        out.append((avg_load_w, observed_drain_w, dt_h))
    return out


def _length_weighted_median(
    items: list[tuple[float, float]],
) -> float | None:
    """Weighted median where each item is `(value, weight)`. Returns the
    value at which the cumulative weight crosses half the total. Used
    by fit_drain_model's narrow-load fallback to weight clean-discharge
    runs by dt² — long runs have far less quantization noise than
    short ones, so the median should reflect their higher confidence
    rather than treating all runs as equally informative.
    """
    if not items:
        return None
    sorted_items = sorted(items, key=lambda x: x[0])
    total_w = sum(w for _, w in sorted_items)
    if total_w <= 0:
        return None
    half = total_w / 2.0
    cum = 0.0
    for v, w in sorted_items:
        cum += w
        if cum >= half:
            return v
    return sorted_items[-1][0]


def diagnose_idle_windows(
    energy_history: list[dict[str, Any]],
    capacity_wh: int | None = None,
) -> dict[str, Any]:
    """Walk adjacent hourly buckets and report why each candidate window
    pair was accepted or rejected by the inverter-overhead-fit gate.
    Used by /api/forecast?_diag=1 to explain a stuck `calibrating`
    state — the gate's `have_idle_windows: 2/5` is opaque on its own;
    this breaks it down into actionable causes.

    Mirrors the gate logic in fit_inverter_overhead_pct — keep these
    two in sync. If a new gate is added there, add it here too."""
    SOLAR_NOISE_WH = 50.0
    AC_NOISE_WH = 50.0
    MIN_OUT_W = 50.0

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    rejected = {
        "missing_soc": 0,
        "soc_drop_below_min_pp": 0,
        "solar_above_noise": 0,
        "ac_input_above_noise": 0,
        "dt_out_of_range": 0,
        "out_w_under_50": 0,
    }
    qualifying = 0
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        soc_a, soc_b = _row_soc(a), _row_soc(b)
        if soc_a is None or soc_b is None:
            rejected["missing_soc"] += 1
            continue
        soc_drop = soc_a - soc_b
        min_pp = (INVERTER_FIT_MIN_SOC_DROP_PCT_SYSTEM
                  if _row_has_system_soc(a) and _row_has_system_soc(b)
                  else INVERTER_FIT_MIN_SOC_DROP_PCT_MAIN)
        if soc_drop < min_pp:
            rejected["soc_drop_below_min_pp"] += 1
            continue
        if (a.get("solar_wh") or 0) > SOLAR_NOISE_WH:
            rejected["solar_above_noise"] += 1
            continue
        if (a.get("ac_input_wh") or 0) > AC_NOISE_WH:
            rejected["ac_input_above_noise"] += 1
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h < 0.5 or dt_h > 6.0:
            rejected["dt_out_of_range"] += 1
            continue
        if ((a.get("output_wh") or 0) / dt_h) < MIN_OUT_W:
            rejected["out_w_under_50"] += 1
            continue
        qualifying += 1
    return {
        "total_pairs": max(0, len(rows) - 1),
        "qualifying_windows": qualifying,
        "needed_windows": MIN_FORECAST_IDLE_WINDOWS,
        "rejected": rejected,
    }


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
@_track_row_soc("fit_charge_efficiency")
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
      - net_input_wh ≥ 100Wh in the window (see net-energy note below),
      - dt 1-6h (longer than that mixes regimes).

    For each qualifying window:
      net_input_wh = max(input_wh - output_wh, 0)
      stored_wh = ΔSOC% * capacity_wh / 100
      efficiency = stored_wh / net_input_wh

    Net-energy denominator: when loads run concurrently with charging
    (e.g. solar charges battery while the home draws ~150W constantly),
    only `input_wh - output_wh` is the energy actually available to
    store. The original code divided stored_wh by raw input_wh, which
    silently attributed the load passthrough as charging losses — on
    one user's history that pulled the fit to 0.583, well below the
    LiFePO4 + inverter physical floor of ~0.85. simulate_soc applies
    `eff` to (solar - load) net inflow, so this denominator matches
    the simulator's semantics. The fit is still slightly biased low
    by parasitic+overhead drain during the window (those drains eat
    into ΔSOC but aren't subtracted from input_wh), but the residual
    is single-digit-percent rather than the multi-tens that load
    passthrough caused.

    Take the median across windows for robustness. Clamp to a sane
    physical range [0.50, 0.99] — anything outside that band is almost
    certainly bad data (sensor glitch or mis-attributed energy flow).
    Falls back to `default` when too few windows are available.

    Returns (efficiency, n_windows_used).
    """
    MIN_INPUT_WH = 100.0
    MAX_START_SOC_PCT = 95.0   # below the top-balance taper regime
    MAX_END_SOC_PCT = 99.0     # below the 100% ceiling clip

    rows = sorted(
        (r for r in (energy_history or []) if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    effs: list[float] = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        soc_a, soc_b = _row_soc(a), _row_soc(b)
        if soc_a is None or soc_b is None:
            continue
        soc_gain = soc_b - soc_a
        # pp gate scales with capacity_wh implicitly (same reasoning as
        # the drain fits — see INVERTER_FIT_MIN_SOC_DROP_PCT_* comment).
        min_pp = (CHARGE_FIT_MIN_SOC_GAIN_PCT_SYSTEM
                  if _row_has_system_soc(a) and _row_has_system_soc(b)
                  else CHARGE_FIT_MIN_SOC_GAIN_PCT_MAIN)
        if soc_gain < min_pp:
            continue
        stored_wh = soc_gain * capacity_wh / 100.0
        if soc_a > MAX_START_SOC_PCT or soc_b > MAX_END_SOC_PCT:
            continue
        input_wh = float(a.get("input_wh") or 0)
        output_wh = float(a.get("output_wh") or 0)
        net_input_wh = max(input_wh - output_wh, 0.0)
        if net_input_wh < MIN_INPUT_WH:
            continue
        dt_h = (b["ts"] - a["ts"]) / 3600.0
        if dt_h <= 0 or dt_h > 6.0:
            continue
        eff = stored_wh / net_input_wh
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
def _trimmed_median(sorted_vals: list[float], trim_pct: float) -> float:
    """Median of `sorted_vals` after dropping the top and bottom
    `trim_pct` fraction. Robust against outliers in sparse buckets
    (e.g. two oven samples in a 14-sample 11am bucket pulling the raw
    median up by 600W). Skipped when n<5 — too few samples to trim
    safely; in that case returns the raw median.

    Caller must pass an already-sorted list. Returns 0.0 for empty.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n < 5:
        return sorted_vals[n // 2]
    k = max(1, int(n * trim_pct))
    trimmed = sorted_vals[k:n - k] if n - 2 * k >= 1 else sorted_vals
    return trimmed[len(trimmed) // 2]


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

    # Net out solar-charge diversion before fitting demand patterns.
    # The controller can intentionally drive a downstream load (EV
    # charger) for hours on sunny days; counting that as "demand"
    # would teach the model that the user has a 1.4kW phantom load
    # every sunny afternoon. Subtract the bucket-averaged diversion
    # rate from output_w to recover real demand. Falls back to raw
    # output_w when no diversion was recorded (pre-feature buckets
    # default to 0 via the schema migration).
    def _net_load_w(r: dict) -> float | None:
        out_w = r.get("output_w")
        if out_w is None:
            return None
        # diverted_wh integrates the plug's draw over the bucket;
        # divide by bucket-hours to get the average plug-draw rate
        # in W. For 10-minute buckets that's diverted_wh / (10/60).
        # Since we don't carry bucket_s on the row, infer from the
        # next row's ts (back-compat: if only one row, assume 600s).
        diverted_wh = float(r.get("solar_charge_diverted_wh") or 0)
        if diverted_wh <= 0:
            return float(out_w)
        # Approx bucket length: 600s default (smallest aggregation).
        # The actual db.history bucket_s isn't on the row, but for
        # the fit we only need a ratio. Use 600 as the safe default —
        # if the caller bucketed to 3600s, diversion stretches over
        # 1h and our subtraction is conservative (under-subtracts
        # by 6x, leaving some pollution but never over-subtracting
        # which would invent negative demand).
        avg_diverted_w = diverted_wh / (600.0 / 3600.0)
        return max(0.0, float(out_w) - avg_diverted_w)

    all_vals_raw = [
        _net_load_w(r) for r in energy_history
        if r.get("output_w") is not None
    ]
    all_vals_raw = [v for v in all_vals_raw if v is not None]
    if not all_vals_raw:
        return {}
    all_vals = sorted(all_vals_raw)
    # Global p95 — used both as the per-sample cap (kills extreme spikes
    # before bucketing) and as the per-bucket ceiling (replaces the old
    # 2 * mean heuristic which clamped real morning peaks).
    cap = all_vals[min(len(all_vals) - 1, int(len(all_vals) * 0.95))]
    # Per-bucket ceiling: the global p95. Floor at IDLE_LOAD_W * 6 so a
    # quiet history (where p95 is e.g. 50W) still allows for real
    # daytime activity to be predicted.
    bucket_ceiling = max(IDLE_LOAD_W * 6, cap)

    # bucket → list of (value, ts)
    buckets: dict[tuple[int, int], list[tuple[float, int]]] = {}
    for row in energy_history:
        ts = row.get("ts")
        out_w = _net_load_w(row)
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
        # Trimmed median: drop top/bottom LOAD_BUCKET_TRIM_PCT before
        # taking the median. Robust against a few oven-style spikes
        # in a sparse bucket. Falls back to raw median when n is too
        # small to trim.
        median = _trimmed_median(vals, LOAD_BUCKET_TRIM_PCT)
        # IQR over the FULL sorted list — we want to detect a
        # genuinely variable bucket (high spread), not the spread of
        # the trimmed center. A bucket with two oven samples and
        # twelve idle samples should still be classified as stable
        # so we don't recency-weight a tiny tail.
        q25 = vals[n // 4]
        q75 = vals[(3 * n) // 4]
        iqr = q75 - q25
        rel_iqr = iqr / median if median > 0 else 0

        # Stable bucket OR too few samples to recency-weight reliably.
        if rel_iqr < LOAD_VARIABILITY_THRESHOLD or n < 6:
            profile[key] = min(median, bucket_ceiling)
            continue

        # Variable bucket: blend recent and older trimmed medians.
        recent = sorted(v for v, t in samples if t >= recency_cutoff)
        older = sorted(v for v, t in samples if t < recency_cutoff)
        if not recent:
            profile[key] = min(median, bucket_ceiling)
            continue
        recent_med = _trimmed_median(recent, LOAD_BUCKET_TRIM_PCT)
        older_med = (_trimmed_median(older, LOAD_BUCKET_TRIM_PCT)
                     if older else recent_med)
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
def fit_charge_ceiling(
    energy_history: list[dict[str, Any]],
    utc_offset_seconds: int = 0,
    *,
    min_charge_days: int = CHARGE_CEILING_MIN_DAYS,
    min_daily_rise_pp: float = CHARGE_CEILING_MIN_DAILY_RISE_PP,
) -> float | None:
    """Learn the per-device charge ceiling — the system-SOC plateau the
    BMS actually allows. Multi-pack rigs top out below 100% because the
    weakest pack saturates first while the capacity-weighted system SOC
    is still well under full; balanced single units reach ~100%.

    Returns the p95 of daily-max system SOC across days that ran a real
    charge cycle (daily SOC rose at least `min_daily_rise_pp`), or
    ``None`` when there isn't enough evidence — in which case the caller
    applies NO cap (100%). Gating on actual charge days keeps a fresh or
    persistently-cloudy install from falsely capping itself low just
    because it hasn't filled yet. Returns None when the learned ceiling
    is at/above CHARGE_CEILING_NO_CAP_ABOVE (the system reaches full, so
    a cap would be a no-op). Uses `_row_soc` so it sees capacity-weighted
    system SOC on multi-pack rigs and main `battery_pct` otherwise.

    Nothing is hardcoded per device: a balanced unit learns ~100 (→ None,
    no cap), an imbalanced 5-pack learns ~78."""
    by_day_max: dict[int, float] = {}
    by_day_min: dict[int, float] = {}
    for row in energy_history:
        soc = _row_soc(row)
        ts = int(row.get("ts") or 0)
        if soc is None or ts <= 0:
            continue
        day = (ts + int(utc_offset_seconds)) // 86400
        by_day_max[day] = max(by_day_max.get(day, -1.0), soc)
        by_day_min[day] = min(by_day_min.get(day, 101.0), soc)
    charge_maxes = sorted(
        by_day_max[d] for d in by_day_max
        if by_day_max[d] - by_day_min.get(d, by_day_max[d]) >= min_daily_rise_pp
    )
    if len(charge_maxes) < min_charge_days:
        return None
    idx = min(len(charge_maxes) - 1, int(len(charge_maxes) * 0.95))
    ceiling = float(charge_maxes[idx])
    if ceiling >= CHARGE_CEILING_NO_CAP_ABOVE:
        return None
    return ceiling


def simulate_soc(
    starting_soc_pct: float,
    capacity_wh: int,
    forecast_hours: list[dict[str, Any]],
    *,
    ac_charge_floor_pct: float | None = None,
    charge_efficiency: float | None = None,
    extra_load_w: float | None = None,
    extra_load_floor_pct: float | None = None,
    soc_ceiling_pct: float | None = None,
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

    `extra_load_w`: add this many watts to the natural load profile on
    every future hour where the controller is projected to be on. Used
    by solar_charge's "with-diversion" projection so the controller's
    gate sees what would *actually* happen if the car charger kept
    running, not just the natural baseline. Pair with
    `extra_load_floor_pct`: at any hour, if simulated SOC would drop
    below that floor, the extra load is dropped (the controller's hard
    SOC floor would have caught it). That way the diversion gets
    credited only for the hours the controller would actually keep it
    on. Pass `extra_load_w=None` for the legacy "natural drain only"
    behavior.

    `soc_ceiling_pct`: cap the simulated SOC at this value every hour
    (in addition to the hard 100% clamp). Models the BMS plateau on
    imbalanced multi-pack rigs that never reach 100% system SOC. None
    (default) → no extra cap. Fit per-device with `fit_charge_ceiling`.
    """
    eff = CHARGE_EFFICIENCY if charge_efficiency is None else float(charge_efficiency)
    soc = max(0.0, min(100.0, float(starting_soc_pct)))
    floor = (max(0.0, min(100.0, float(ac_charge_floor_pct)))
             if ac_charge_floor_pct is not None else None)
    extra_w = float(extra_load_w or 0)
    extra_floor = (float(extra_load_floor_pct)
                   if extra_load_floor_pct is not None else None)
    n = len(forecast_hours)

    # Pre-compute the natural-only (no-extra) cumulative SOC trajectory
    # so the per-hour extra-load decision below knows what the WORST
    # natural-only SOC would be over the remaining window. Without this
    # look-ahead, the simulator's greedy "apply if next-hour SOC is OK"
    # check kept the controller running until the IMMEDIATE next hour
    # would breach the floor — then natural drain continued unchecked
    # and the trough landed well below the user's target. With this,
    # the simulator self-throttles: each hour, apply extra only if the
    # entire rest-of-window natural trajectory (from this point) would
    # still stay above the floor. Effectively models the controller's
    # actual ON/OFF behavior tick-by-tick.
    nat_hourly_pp = []
    for h in forecast_hours:
        net = float(h.get("solar_w") or 0) - float(h.get("load_w") or 0)
        if net > 0:
            net *= eff
        nat_hourly_pp.append(net / capacity_wh * 100.0)
    # cum_nat[i] = cumulative natural SOC delta from start to start of hour i
    cum_nat = [0.0] * (n + 1)
    for i in range(n):
        cum_nat[i + 1] = cum_nat[i] + nat_hourly_pp[i]
    # min_cum_from[i] = min of cum_nat[j] over all j in i..n (inclusive)
    # Used to compute the worst-case (lowest) natural-only SOC reachable
    # from hour i onward, given a known starting SOC at that point.
    min_cum_from = [cum_nat[n]] * (n + 1)
    for i in range(n - 1, -1, -1):
        min_cum_from[i] = min(cum_nat[i], min_cum_from[i + 1])

    out: list[dict[str, Any]] = []
    for i, h in enumerate(forecast_hours):
        solar = float(h.get("solar_w") or 0)
        load = float(h.get("load_w") or 0)
        applied_extra = 0.0
        if extra_w > 0 and extra_floor is not None:
            # Tentatively apply extra this hour and compute SOC at end
            # of the hour.
            tentative_net = solar - (load + extra_w)
            if tentative_net > 0:
                tentative_net *= eff
            tentative_soc = soc + tentative_net / capacity_wh * 100.0
            tentative_soc = max(0.0, min(100.0, tentative_soc))
            # From start of hour i+1 onward, assume natural-only (the
            # controller stops). Worst-case SOC achieved:
            #   min_future = tentative_soc + (min_cum_from[i+1] - cum_nat[i+1])
            # i.e. the lowest natural-only trajectory starting from
            # tentative_soc at hour i+1.
            min_future_soc = (tentative_soc
                              + (min_cum_from[i + 1] - cum_nat[i + 1]))
            if min_future_soc >= extra_floor:
                applied_extra = extra_w
        elif extra_w > 0 and extra_floor is None:
            # No floor constraint — apply extra every hour.
            applied_extra = extra_w
        # 1 hour interval, simple Euler step. Apply CHARGE_EFFICIENCY when
        # net inflow positive — discharge already accounts for inverter
        # losses on the load side.
        net = solar - (load + applied_extra)
        if net > 0:
            net *= eff
        soc += net / capacity_wh * 100.0
        soc = max(0.0, min(100.0, soc))
        # Learned charge ceiling — multi-pack rigs plateau below 100%
        # system SOC (weakest pack saturates, BMS curtails). Clamp here
        # so net-positive days don't run the simulated SOC up to 100%
        # when the real system tops out lower. None → no cap (default).
        if soc_ceiling_pct is not None and soc > soc_ceiling_pct:
            soc = soc_ceiling_pct
        # Smart-charge floor — the user has Kasa-driven grid top-up that
        # holds SOC at or above target_sunrise_soc_pct. Modeling it as a
        # hard floor undercounts how much grid energy is actually used
        # but cleanly addresses the "predicted 0% / actual 92%" cliff.
        if floor is not None and soc < floor:
            soc = floor
        out.append({**h, "predicted_soc": round(soc, 1),
                    "extra_load_w_applied": round(applied_extra, 1)})
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
    forecast. Returns a dict with `ready` + the metrics the gate
    evaluated, so the UI can show "8 of 24 hours captured" progress
    instead of a binary unhelpful "not yet".

    The only hard gate is span: ≥ MIN_FORECAST_HISTORY_HOURS between
    earliest and latest sample. A full diurnal cycle is the minimum to
    fit a useful solar coefficient and a per-hour load profile.

    The inverter-overhead fit's window count is reported but doesn't
    block readiness anymore. Backup devices that intentionally sit idle
    most of the time (e.g. a HomePower 3000 used a few times a year for
    camping) will never produce 5 clean discharge windows — requiring
    them to run a heater for the algorithm's benefit was a poor design.
    Instead we surface `low_confidence_overhead_fit` and let the fit
    fall back to the population-default 10% overhead, which is close
    enough on a mostly-idle device where parasitic drain dominates and
    the load model is already correctly captured by the per-hour bucket
    profile.
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
            "low_confidence_overhead_fit": True,
        }
    span_hours = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0
    _, n_windows = fit_idle_overhead_w(energy_history, capacity_wh)
    ready = span_hours >= MIN_FORECAST_HISTORY_HOURS
    return {
        "ready": ready,
        "reason": "ready" if ready else "calibrating",
        "have_hours": round(span_hours, 1),
        "needed_hours": MIN_FORECAST_HISTORY_HOURS,
        "have_idle_windows": n_windows,
        "needed_idle_windows": MIN_FORECAST_IDLE_WINDOWS,
        "low_confidence_overhead_fit": n_windows < MIN_FORECAST_IDLE_WINDOWS,
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
    extra_load_w: float | None = None,
    extra_load_floor_pct: float | None = None,
    pack_count: int = 0,
    utc_offset_seconds: int = 0,
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
    # Per-local-hour shade/azimuth correction on top of k. All-1.0 on a
    # fresh install → identical to pure k*GHI; learns the site's diurnal
    # curve (e.g. afternoon shade) as history accumulates.
    diurnal_shape = fit_diurnal_shape(energy_history, weather_hourly, k,
                                      utc_offset_seconds=utc_offset_seconds)
    profile = fit_load_profile(energy_history, now_ts=now_ts)
    # Per-device drain model: parasitic baseline (W) + percentage of
    # throughput. The parasitic term captures BMS, idle inverter, pack
    # balancing, and any unmeasured DC bus draw — pieces the
    # pure-percentage model couldn't represent and which the advisor
    # consistently flagged as an "unaccounted gap" on multi-pack rigs
    # (e.g. 430W on the user's 5000+ on 2026-05-04).
    parasitic_w, overhead_pct, overhead_n = fit_drain_model(
        energy_history, capacity_wh, pack_count=pack_count,
    )
    # Effective parasitic for the simulator: main-unit residual (from
    # the fit, with pack contribution already subtracted) PLUS the
    # constant per-pack BMS baseline. Single-unit devices (pack_count=0)
    # get pack_baseline=0 and behave exactly as before.
    pack_baseline_w = per_pack_baseline_w(pack_count)
    effective_parasitic_w = parasitic_w + pack_baseline_w
    # Per-device charge efficiency, same idea: fit from the ratio of
    # observed SOC gain to reported input_wh on clean charging windows.
    charge_eff, charge_eff_n = fit_charge_efficiency(energy_history, capacity_wh)
    out_vals = [r["output_w"] for r in energy_history if r.get("output_w") is not None]
    # Reported as a debug stat only — NOT used as a fallback for missing
    # hours. Mixing daytime samples into nighttime forecasts was the bug.
    overall_load = sum(out_vals) / len(out_vals) if out_vals else 0.0
    out_vals_sorted = sorted(out_vals)
    out_p95 = (out_vals_sorted[min(len(out_vals_sorted) - 1,
                                    int(len(out_vals_sorted) * 0.95))]
               if out_vals_sorted else 0.0)
    # Source labels: "fit" if the per-device fit landed in plausible
    # range, "default" if it fell back. Lets the UI (and the AI
    # advisor) tell at a glance whether the device is calibrated or
    # still using population defaults.
    overhead_source = "fit" if overhead_n >= 5 else "default"
    charge_eff_source = "fit" if charge_eff_n >= 5 else "default"

    # Cap projected solar by the device's observed peak in the last
    # SOLAR_CAP_HISTORY_S so an overfit regression can't predict more
    # solar than the array has actually produced. The window is
    # deliberately wide (14 days) so a recent cloudy stretch can't
    # falsely clamp predictions for upcoming bright days — see the
    # SOLAR_CAP_HISTORY_S comment for the advisor finding that
    # motivated this. SOLAR_RECENT_CAP_MULT leaves headroom for
    # clearer-sky days; falls back to None (no cap) when there's no
    # qualifying data to anchor against.
    recent_cutoff = cutoff - SOLAR_CAP_HISTORY_S
    recent_peak = max(
        (float(r.get("solar_w") or 0) for r in energy_history
         if r.get("ts") and r["ts"] >= recent_cutoff),
        default=0.0,
    )
    solar_cap = recent_peak * SOLAR_RECENT_CAP_MULT if recent_peak > 50 else None

    # Per-device charge ceiling (None → no cap). Learned from observed
    # daily-max system SOC on real charge days; caps the simulator so
    # multi-pack rigs that plateau ~78% don't run up to 100% on long-
    # lead net-positive days.
    soc_ceiling = fit_charge_ceiling(energy_history,
                                     utc_offset_seconds=utc_offset_seconds)

    future = [w for w in weather_hourly if int(w.get("ts") or 0) >= cutoff]
    future = future[:horizon_hours]

    forecast_hours = []
    for w in future:
        ts = int(w["ts"])
        ghi = float(w.get("ghi_w_m2") or 0)
        # Apply the learned diurnal shape (afternoon shade etc.) on top of
        # the global coefficient. shape defaults to 1.0 per hour, so this
        # is a no-op until the per-hour factors are learned from history.
        lhod = ((ts + int(utc_offset_seconds)) % 86400) // 3600
        shape_factor = diurnal_shape.get(lhod, 1.0)
        solar_w_uncapped = max(0.0, k * ghi * shape_factor)
        solar_w = solar_w_uncapped
        capped = False
        if solar_cap is not None and solar_w > solar_cap:
            solar_w = solar_cap
            capped = True
        load_w = expected_load_w(profile, ts,
                                  idle_overhead_w=effective_parasitic_w,
                                  inverter_overhead_pct=overhead_pct)
        forecast_hours.append({
            "ts": ts,
            "solar_w": round(solar_w, 1),
            # Surfacing the underlying GHI + uncapped k*GHI value lets
            # the dashboard tell whether a high solar_w prediction came
            # from a high Open-Meteo GHI (model trusts the weather feed)
            # or from the recent-peak cap NOT binding. cloud_cover is
            # informational only — the forecaster does NOT attenuate
            # GHI by it; Open-Meteo's `shortwave_radiation` already
            # incorporates cloud cover by spec.
            "ghi_w_m2": round(ghi, 1),
            "solar_w_uncapped": round(solar_w_uncapped, 1),
            "solar_capped": capped,
            "load_w": round(load_w, 1),
            "cloud_cover_pct": round(float(w.get("cloud_cover_pct") or 0), 1),
        })

    simulated = simulate_soc(
        starting_soc_pct, capacity_wh, forecast_hours,
        ac_charge_floor_pct=ac_charge_floor_pct,
        charge_efficiency=charge_eff,
        extra_load_w=extra_load_w,
        extra_load_floor_pct=extra_load_floor_pct,
        soc_ceiling_pct=soc_ceiling,
    )
    return {
        "ready": True,
        "readiness": readiness,
        "starting_soc_pct": round(starting_soc_pct, 1),
        "capacity_wh": capacity_wh,
        "solar_coefficient": round(k, 4),
        "fit_samples": n_fit,
        # Anchors the recent-peak solar cap. `solar_recent_peak_w` is
        # the highest solar_w sample in the last 48h; `solar_cap_w` is
        # that x SOLAR_RECENT_CAP_MULT (or null when the cap is
        # disabled because there's no recent data > 50W). Useful for
        # diagnosing whether a high forecast solar_w is being capped
        # or running away — pair with the per-hour `solar_capped` flag.
        "solar_recent_peak_w": round(recent_peak, 1),
        "solar_cap_w": round(solar_cap, 1) if solar_cap is not None else None,
        # Per-local-hour diurnal shade/azimuth correction applied on top
        # of solar_coefficient. 1.0 = no correction (cold start / no
        # shade); < 1.0 = that local hour produces less than k*GHI would
        # predict (e.g. afternoon shade). Keyed by local hour-of-day.
        "diurnal_shape": {str(h): round(v, 3) for h, v in sorted(diurnal_shape.items())},
        # Learned charge ceiling (system-SOC plateau the BMS allows).
        # null = no cap (reaches full, or too little charge-cycle data).
        "charge_ceiling_pct": round(soc_ceiling, 1) if soc_ceiling is not None else None,
        "overall_load_w": round(overall_load, 1),
        # NEW: p95 of output_w. Used as the per-bucket ceiling in the
        # load profile, replacing the old 2*mean heuristic that biased
        # busy-hour predictions upward.
        "output_w_p95": round(out_p95, 1),
        # Hybrid drain model: parasitic_w (main-unit constant baseline)
        # + pack_baseline_w (per-pack BMS, scales with expansion pack
        # count) + base_load * (1 + inverter_overhead_pct). _n reports
        # how many clean discharge windows fed the joint fit — when
        # small, parasitic_w + overhead_pct are population defaults
        # (50W, 10%); pack_baseline_w is computed from pack_count
        # regardless of fit confidence.
        "parasitic_w": round(parasitic_w, 1),
        "pack_baseline_w": round(pack_baseline_w, 1),
        "pack_count": int(pack_count),
        "effective_parasitic_w": round(effective_parasitic_w, 1),
        "inverter_overhead_pct": round(overhead_pct, 4),
        "inverter_overhead_n_windows": overhead_n,
        "inverter_overhead_source": overhead_source,
        # Back-compat: legacy clients read `idle_overhead_w` expecting an
        # absolute watt figure. Surface the EFFECTIVE parasitic (main +
        # packs) under that name since callers feeding it back into
        # expected_load_w expect the full baseline, not just the main
        # share.
        "idle_overhead_w": round(effective_parasitic_w, 1),
        "idle_overhead_n_windows": overhead_n,
        # Auto-fitted per-device charge efficiency (input_wh → stored_wh
        # ratio). Same n_windows convention as idle_overhead.
        "charge_efficiency": round(charge_eff, 3),
        "charge_efficiency_n_windows": charge_eff_n,
        "charge_efficiency_source": charge_eff_source,
        "forecast": simulated,
    }
