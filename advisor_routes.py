"""Algorithm advisor: routes + daily background loop.

The advisor uses Claude (extended thinking + agentic DB-query tools) to
review each device's predicted-vs-actual data once per day and surface
config suggestions / anomalies the user can apply or dismiss from the UI.

Public surface:
  - install(app, state, helpers): register /api/algorithm/* routes.
  - advisor_loop(state, helpers):  daily background-task body. Caller
                                   creates and cancels the task.

Both take an AdvisorHelpers object holding the three server-side
capacity helpers (total_capacity_wh, capacity_hints, system_soc_pct)
so this module stays decoupled from server.py — preventing the
circular import that an `from server import ...` would create.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException

import backoff as _backoff
import forecaster
import location as device_location
import settings as user_settings
import smart_charge

log = logging.getLogger("jackery-monitor")


@dataclass
class AdvisorHelpers:
    """Server-side capacity helpers the advisor reuses. Injected at
    install/loop entry so this module doesn't import server.py."""
    total_capacity_wh: Callable[[str | None, int | None], int]
    capacity_hints: Callable[[str | None], tuple[int | None, int | None]]
    system_soc_pct: Callable[[float, str | None, int | None], float]


# ---------- bundle builders ----------

async def _build_advisor_bundle(state, helpers: AdvisorHelpers,
                                device_sn: str) -> dict:
    """Gather the data Claude needs to review yesterday's algorithm
    performance for one device. Plain JSON-serialisable dict — see
    claude_advisor._format_starter_bundle for the rendering."""
    from datetime import datetime, timezone
    def _iso(ts: int | float | None) -> str:
        if ts is None:
            return "—"
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

    dev_meta = next(
        (d for d in state.energy.list_devices()
         if d.get("device_sn") == device_sn),
        {},
    )
    main_soc = (state.last_status or {}).get("battery_percent") if state.device and state.device.device_sn == device_sn else None
    model_code = dev_meta.get("model_code")
    capacity = helpers.total_capacity_wh(device_sn, model_code)
    sys_soc = helpers.system_soc_pct(float(main_soc), device_sn, model_code) if main_soc is not None else None
    pack_count = len(state.battery_packs_by_sn.get(device_sn, []))

    cfg = smart_charge.get_config(device_sn)

    # Per-device fitted drain coefficients — let Claude see what the
    # simulator is actually using. Hybrid model:
    #   drain_w = parasitic_w + load_w * (1 + inverter_overhead_pct)
    # Both surfaced; advisor should reason about them together when
    # diagnosing load-accuracy gaps.
    fitted_parasitic_w: float | None = None
    fitted_overhead_pct: float | None = None
    fitted_drain_n: int = 0
    try:
        ehist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
        fitted_parasitic_w, fitted_overhead_pct, fitted_drain_n = (
            forecaster.fit_drain_model(ehist, capacity)
        )
    except Exception as e:
        log.debug("advisor: drain model fit failed: %s", e)

    main_wh = forecaster.battery_capacity_wh(model_code)
    pack_wh = forecaster.expansion_pack_capacity_wh(model_code)

    accuracy_summary: dict[str, dict[str, float]] = {}
    try:
        samples_acc = state.energy.prediction_accuracy(
            device_sn,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        )
        for s in samples_acc:
            h = s["lead_time_h"]
            bucket = "≤6h" if h <= 6 else "≤24h" if h <= 24 else "≤72h" if h <= 72 else ">72h"
            b = accuracy_summary.setdefault(bucket, {"n": 0, "sum_err": 0.0})
            b["n"] += 1
            b["sum_err"] += s["error"]
        for b in accuracy_summary.values():
            b["mae"] = round(b["sum_err"] / b["n"], 2) if b["n"] else 0
            del b["sum_err"]
    except Exception as e:
        log.debug("advisor: accuracy summary failed: %s", e)

    # Last 24h hourly history (energy_db.history with 1h buckets).
    # IMPORTANT: report the *integrated* W = Wh-per-hour for power
    # fields, not AVG(last_w) which is the average of instantaneous
    # samples. The latter biases low when brief high-load spikes
    # happen between polls (we'd see idle in most samples). The
    # integrated value is the true average power for the hour and
    # what reconciles with SOC drain.
    recent_samples = []
    try:
        for h in state.energy.history(device_sn, hours=24, bucket_s=3600):
            recent_samples.append({
                "hour": _iso(h["ts"]),
                "soc": h.get("battery_pct"),
                # Wh accumulated per 1h bucket → average W during that hour.
                "input_w_avg": int(h.get("input_wh") or 0),
                "output_w_avg": int(h.get("output_wh") or 0),
                "solar_w_avg": int(h.get("solar_wh") or 0),
                "ac_input_w_avg": int(h.get("ac_input_wh") or 0),
                # Also include instantaneous values for comparison —
                # divergence between _avg and _instant reveals brief
                # spikes the poller missed.
                "input_w_instant": h.get("input_w"),
                "output_w_instant": h.get("output_w"),
            })
    except Exception as e:
        log.debug("advisor: samples bundle failed: %s", e)

    # Last 24h weather observations.
    recent_weather = []
    try:
        since = int(time.time()) - 24 * 3600
        for w in state.energy.list_weather_observations(since_ts=since, limit=48):
            recent_weather.append({
                "hour": _iso(w["ts"]),
                "ghi_w_m2": w.get("ghi_w_m2"),
                "cloud_cover_pct": w.get("cloud_cover_pct"),
            })
    except Exception as e:
        log.debug("advisor: weather bundle failed: %s", e)

    # Predicted-vs-actual pairs from the last 48h target window — the
    # raw signal Claude needs to diagnose where the model is missing.
    # `made_iso` is included so Claude can correlate each row to the
    # `recent_code_changes` timestamps and ignore pre-fix predictions.
    recent_predictions = []
    try:
        cutoff = time.time() - 48 * 3600
        for p in state.energy.prediction_accuracy(
            device_sn,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        ):
            if p.get("target", 0) < cutoff:
                continue
            recent_predictions.append({
                "made_iso": _iso(p.get("made_at")),
                "target_iso": _iso(p["target"]),
                "lead_h": p["lead_time_h"],
                "predicted_soc": round(p["predicted_soc"], 1),
                "actual_soc": round(p["actual_soc"], 1),
                "error": round(p["error"], 1),
            })
        # Cap at most 60 rows so we don't blow the prompt budget.
        recent_predictions = recent_predictions[:60]
    except Exception as e:
        log.debug("advisor: predictions bundle failed: %s", e)

    # Smart-charge decisions joined to actuals.
    recent_decisions = []
    try:
        for d in state.energy.smart_charge_analytics(
            device_sn, days=7,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        ):
            recent_decisions.append({
                "decided_iso": _iso(d.get("decided_at")),
                "action": d.get("action"),
                "mode": d.get("mode"),
                "predicted_sunrise_soc_pct": d.get("predicted_sunrise_soc_pct"),
                "actual_sunrise_soc_pct": d.get("actual_sunrise_soc_pct"),
                "target_sunrise_soc_pct": d.get("target_sunrise_soc_pct"),
                "reason": d.get("reason"),
            })
    except Exception as e:
        log.debug("advisor: decisions bundle failed: %s", e)

    return {
        "window_label": f"last 48h ending {datetime.now().isoformat(timespec='minutes')}",
        "device_label": dev_meta.get("name") or "Jackery",
        "device_sn": device_sn,
        "capacity_wh": capacity,
        "pack_count": pack_count,
        "main_soc_pct": main_soc,
        "system_soc_pct": round(sys_soc, 1) if sys_soc is not None else None,
        "smart_charge_config": cfg,
        # Hybrid drain model: surface both terms so the advisor can
        # reason about parasitic baseline vs throughput-scaled overhead
        # separately. `fitted_idle_overhead_w` keeps its old name for
        # back-compat in the narrator/UI but now holds the parasitic_w
        # term directly (the right interpretation all along).
        "fitted_parasitic_w": (round(fitted_parasitic_w, 1)
                               if fitted_parasitic_w is not None else None),
        "fitted_inverter_overhead_pct": (round(fitted_overhead_pct, 4)
                                         if fitted_overhead_pct is not None else None),
        "fitted_drain_n_windows": fitted_drain_n,
        "fitted_idle_overhead_w": (round(fitted_parasitic_w, 1)
                                   if fitted_parasitic_w is not None else None),
        "fitted_idle_overhead_n_windows": fitted_drain_n,
        "forecast_accuracy_summary": accuracy_summary,
        "recent_samples": recent_samples,
        "recent_weather": recent_weather,
        "recent_predictions": recent_predictions,
        "recent_decisions": recent_decisions,
        # Hand-maintained list of recent fixes that change the meaning of
        # historical data. The advisor sees a 48h window, so every fix
        # less than 48h old has stale data on both sides of it; without
        # this hint Claude re-flags bugs we just shipped. Each entry
        # tells Claude "data older than `ts` was generated by buggy
        # code; don't bill the current code for it." Update by hand
        # when a fix touches forecaster / smart-charge / load model.
        # Keep in chronological order and prune entries older than ~7
        # days (they fall out of all advisor windows by then).
        "recent_code_changes": _recent_code_changes(),
    }


def _recent_code_changes() -> list[dict[str, Any]]:
    """Return the rolling list of fixes the advisor needs to know about
    when interpreting historical samples / predictions / decisions."""
    return [
        {
            "ts_iso": "2026-04-30T14:30:00+00:00",
            "subsystem": "smart_charge",
            "summary": (
                "Fixed smart_charge.record_decision() — was calling a "
                "non-existent method swallowed by a broad except, so the "
                "decisions table was empty for ~14 days. As of this "
                "timestamp, every smart-charge tick (incl. 'test' mode "
                "skips) writes a decisions row. Empty decisions before "
                "this is a logging bug, not a controller failure."
            ),
        },
        {
            "ts_iso": "2026-04-30T18:55:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Two material changes to address bugs YOU surfaced in a "
                "prior review: (1) added IDLE_OVERHEAD_W=600 to every "
                "expected_load_w() lookup so the load model accounts "
                "for inverter idle / DC-bus / balancing draw that "
                "doesn't show up in out_w but does drain the battery; "
                "(2) added ac_charge_floor_pct to simulate_soc — when "
                "smart-charge is enabled, SOC is clamped at the "
                "target_sunrise_soc_pct floor so long-lead predictions "
                "no longer saturate at 0%. Predictions made BEFORE this "
                "timestamp WILL show a 0% long-lead cliff and a 5-15pp "
                "short-lead under-bias; those are the bugs this commit "
                "fixed. Don't re-flag them — assess only predictions "
                "with made_at >= this timestamp."
            ),
        },
        {
            "ts_iso": "2026-05-01T16:00:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Re-tuned IDLE_OVERHEAD_W from 600 → 200 based on YOUR "
                "previous review's empirical reconciliation: steady-state "
                "windows showed actual constant overhead is closer to "
                "145-190W, and 600W was over-predicting drain by 300-450W."
            ),
        },
        {
            "ts_iso": "2026-05-01T17:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Replaced the hardcoded IDLE_OVERHEAD_W constant with a "
                "per-device auto-fit: fit_idle_overhead_w() now walks the "
                "user's own discharge history (bucket pairs with no solar, "
                "no AC charging, ≥1pp SOC drop) and computes the implied "
                "parasitic from observed SOC slope minus reported out_w, "
                "then takes the median across qualifying windows. The 200W "
                "constant is now just the cold-start fallback. The fitted "
                "value for THIS device is in the bundle as "
                "fitted_idle_overhead_w — use that when reasoning about "
                "load accuracy, not the constant. If the fitted value "
                "looks wrong, flag the FIT (data quality, edge cases), "
                "not the constant."
            ),
        },
        {
            "ts_iso": "2026-05-01T17:00:00+00:00",
            "subsystem": "telemetry",
            "summary": (
                "Per-pack temperatures (`internal_temp_c` / `it`) are "
                "now ALWAYS None — the field is unconditionally stripped "
                "at ingestion and at every read path. The Jackery "
                "5000 Plus's BMS reports unreliable per-pack temps "
                "across firmwares (observed 4°C with 20°C+ ambient, "
                "135°C while neighbors read 78°C, etc.) and the user "
                "explicitly asked us to ignore the field. Only the "
                "main unit's `bt` field (rendered as battery_temp_c) is "
                "trustworthy. Do NOT flag missing per-pack temps as a "
                "concern; do NOT propose tunables that depend on pack "
                "temperature. Pack thermal monitoring is out of scope."
            ),
        },
        {
            "ts_iso": "2026-05-01T18:35:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Inverter overhead model switched from a flat watt "
                "constant to a percentage of throughput. Was: "
                "expected_load = base + idle_overhead_w (200W default). "
                "Now: expected_load = base * (1 + inverter_overhead_pct) "
                "with default 0.10 (10%) — modern LiFePO4 inverters lose "
                "~10% as heat in DC->AC conversion, scales with load. "
                "fit_inverter_overhead_pct replaces fit_idle_overhead_w; "
                "the legacy fit_idle_overhead_w is now a thin shim that "
                "converts the percentage to watts at a typical 500W "
                "load. DEVICE_PARAM_KEYS exposes `inverter_overhead_pct` "
                "(unit=ratio); the bundle still carries idle_overhead_w "
                "for back-compat. When reasoning about load accuracy, "
                "prefer the percentage — it's the source of truth."
            ),
        },
        {
            "ts_iso": "2026-05-04T15:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Closed the predicted-vs-actual measurement asymmetry "
                "you flagged on 2026-05-04: prediction_accuracy and "
                "smart_charge_analytics now compute capacity-weighted "
                "system SOC for the actual side too, by joining "
                "battery_packs at target ±30min. Predicted (system) "
                "now compared to actual (system). Single-unit devices "
                "and pre-pack-recording history degenerate to the "
                "main-only behavior, so historical data isn't rewritten. "
                "Headline accuracy summary should drop several pp once "
                "fresh predictions accumulate; if long-lead MAE doesn't "
                "improve, the residual is a real solar/load-model "
                "defect, not the asymmetry."
            ),
        },
        {
            "ts_iso": "2026-05-05T03:00:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Drain model switched from pure-percentage to hybrid: "
                "drain_w = parasitic_w + load_w * (1 + overhead_pct), "
                "fit jointly via 2-param OLS on (load, drain) pairs. "
                "Closes the 'unaccounted ~430W gap' you flagged on "
                "2026-05-04 02:00→12:00 (and similar on 5/3 overnight): "
                "BMS + idle inverter + pack-balancing on multi-pack "
                "rigs is a near-constant baseline that the previous "
                "percentage-only model couldn't represent — its 50% "
                "overhead clamp rejected exactly the windows where "
                "this baseline showed up, falling back to the 10% "
                "default. The bundle now exposes `parasitic_w` "
                "alongside `inverter_overhead_pct`; reason about the "
                "two together when evaluating load accuracy. The legacy "
                "`idle_overhead_w` field now holds parasitic_w directly "
                "(it always meant absolute watts; only the fit was "
                "wrong). User confirmed no DC loads (USB/12V/car port) "
                "so the gap is genuine parasitic, not unmeasured load."
            ),
        },
        {
            "ts_iso": "2026-05-05T03:45:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Followup on the hybrid drain fit: you correctly "
                "flagged 03:13 that the OLS collapsed to (50W, 0.10) "
                "priors when the user's load distribution is narrow "
                "(steady ~470W overnight). Added a load-range gate: "
                "when load is narrow, fall back to a parasitic-only "
                "fit with overhead pinned at the default — solve "
                "parasitic_w = drain - load * (1 + default_pct) per "
                "window, take the median."
            ),
        },
        {
            "ts_iso": "2026-05-05T15:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Followup on the followup: you flagged 15:04 that "
                "the parasitic-only fallback STILL wasn't firing on "
                "the user's data — fit kept returning the 50W cold-"
                "start default. Root cause: the load-range gate used "
                "raw max/min, which gets fooled by a single outlier "
                "high-load window (1 kettle run during 14d history "
                "pushes max/min to 3.2x even when 99% of windows are "
                "tightly clustered at ~460W). Switched to p90/p10 "
                "percentile-based metric — outlier-resistant, "
                "correctly classifies the device as 'narrow' so the "
                "fallback fires. Should now recover parasitic_w ≈ "
                "80-100W on this device per your reconciliation "
                "(advisor said true value ≈ 84W after subtracting "
                "the 10% pinned overhead from 130W total)."
            ),
        },
        {
            "ts_iso": "2026-05-06T04:14:26+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Smart-charge floor (target_sunrise_soc_pct) is "
                "REMOVED from the displayed/persisted forecast in "
                "every mode. Previously the simulator clamped SOC at "
                "the target in active mode (and originally in test "
                "mode too) so the prediction reflected what the user "
                "would observe given controller intervention. User "
                "explicitly rejected this 2026-05-06: the prediction "
                "should show the TRUTH — what the battery will do "
                "without intervention — and the controller's effect "
                "is shown separately via the Plan (predicted vs "
                "target + deficit + charge schedule). Conflating "
                "them was a feedback loop and was hiding real model "
                "bias (advisor's 03:42 anomaly directly asked for "
                "the unclamped forecast). Post-fix: /api/forecast "
                "and forecast_predictions are baseline. compute_plan "
                "still uses baseline_predicted for deficit math "
                "(unchanged); with floor=None the `forecast` and "
                "`baseline_forecast` arguments are now identical, "
                "and Plan.predicted_sunrise_soc_pct == "
                "Plan.baseline_predicted_sunrise_soc_pct (both = "
                "truth). Predictions made BEFORE this timestamp in "
                "active mode show the floor clamp at target; those "
                "at or after are baseline. When evaluating accuracy "
                "for active-mode nights, expect persisted predictions "
                "to be lower than actuals by ~target-actual_unclamped "
                "— that's the controller's grid-charge work, not "
                "model bias. Use the smart_charge_decisions table to "
                "identify which nights had intervention."
            ),
        },
        {
            "ts_iso": "2026-05-06T14:14:33+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Slope-based fits (drain model, charge efficiency, "
                "inverter overhead) now walk capacity-weighted SYSTEM "
                "SOC instead of main-pack SOC on multi-pack rigs. "
                "Root cause flagged by you 2026-05-06T13:47: with "
                "battery_pct = main pack and capacity_wh = system "
                "(30240 Wh on a 6-pack rig), the implied drain was "
                "the real drain × pack_ratio (~6×), inflating "
                "fitted parasitic_w to 316-370W vs the empirical "
                "~130W. Implementation: energy_db.history() takes "
                "optional (main_capacity_wh, pack_capacity_wh) and "
                "adds `system_soc` per row by joining the closest "
                "battery_packs snapshot (±30 min) — same logic as "
                "the prediction-accuracy capacity-weighting. "
                "forecaster's _row_soc() helper prefers system_soc "
                "and falls back to battery_pct, so single-unit "
                "devices and tests without capacity hints keep the "
                "old behavior. Slope-magnitude thresholds switched "
                "from pp ('≥2pp drop') to Wh ('≥100 Wh drained') so "
                "the gate is device-agnostic — same energy floor "
                "whether walking main or system. Expect parasitic_w "
                "to drop sharply on multi-pack devices when this "
                "rebuilds; the 3-8pp under-bias on long-lead "
                "predictions you flagged in today's INFO anomaly "
                "should resolve. Single-unit devices unchanged."
            ),
        },
        {
            "ts_iso": "2026-05-06T15:30:06+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Removed absolute Wh signal-floor gates from the "
                "slope-based fits (drain model, charge efficiency, "
                "inverter overhead, multi-hour clean-discharge runs). "
                "Were 100 Wh / 50 Wh / 150 Wh respectively. They were "
                "redundant on multi-pack rigs (the pp gate already "
                "translates to a comparable Wh threshold via "
                "pp×capacity) and over-strict on small single-unit "
                "devices: HP3K's typical 30 W load yielded ~60 Wh/h "
                "drain, qualifying under the pp gate but failing the "
                "100 Wh signal floor — fit_windows collapsed to 0 "
                "(advisor flagged 2026-05-06T15:10). Pp gates remain "
                "(2pp main / 0.5pp system for drain & inverter, 1pp / "
                "0.25pp for charge efficiency, 3pp / 0.5pp for runs). "
                "Expect HP3K's parasitic_w to start fitting from "
                "discharge windows again; the 5000+ behavior is "
                "unchanged because the pp gate dominated there anyway."
            ),
        },
        {
            "ts_iso": "2026-05-06T17:57:49+00:00",
            "subsystem": "forecaster",
            "summary": (
                "fit_solar_coefficient now filters to clear-sky pairs "
                "(GHI ≥ 700 W/m² AND cloud_cover ≤ 30%) before the "
                "least-squares fit, falling back to the broad GHI>50 "
                "pool only if too few clear-sky samples exist. "
                "Open-Meteo's `shortwave_radiation` is post-cloud "
                "all-sky GHI and clouds attenuate panel output non-"
                "linearly, so mixing cloudy and clear hours pulled the "
                "LSQ slope below the clear-sky truth. Symptom on the "
                "user's 5000+ on 2026-05-06: actuals peaked at 3700+W "
                "but fit_solar_coefficient returned k=3.04 (predicting "
                "2891W peak), driving an 18pp long-lead MAE. Expect k "
                "to drift up to ~3.5-3.7 over the next refit cycle and "
                "long-lead MAE to drop into the 10-12pp range. Don't "
                "re-flag the prior under-prediction as a new defect — "
                "this commit IS the fix."
            ),
        },
        {
            "ts_iso": "2026-05-06T17:57:49+00:00",
            "subsystem": "forecaster",
            "summary": (
                "fit_charge_efficiency now subtracts output_wh from "
                "input_wh before computing the per-window efficiency. "
                "Was: eff = stored_wh / input_wh. Now: eff = stored_wh "
                "/ max(input_wh - output_wh, 0). Bug: when loads ran "
                "concurrently with charging (very common — solar "
                "charges battery while home draws ~150W constantly), "
                "the load passthrough showed up as fake 'charging "
                "losses' and dragged the fit below the LiFePO4 "
                "physical floor. User's 5000+ was fitting to 0.583 "
                "with the old code (advisor flagged 'that can't be "
                "right'); LiFePO4 + inverter is 0.85-0.95 in reality. "
                "simulate_soc applies eff to (solar - load) net "
                "inflow, so the new denominator matches simulator "
                "semantics. Residual under-bias from parasitic + "
                "overhead drain during the window is single-digit "
                "percent (those drains eat into ΔSOC but aren't "
                "subtracted from input_wh), much smaller than the "
                "load-passthrough error this fixes. Don't re-flag the "
                "old 0.583 as a separate defect."
            ),
        },
    ]


def _parse_iso(s: str | None) -> int | None:
    """Loose ISO-8601 → unix-seconds parser for the advisor's tool args.
    Accepts trailing Z, offsetless naive datetimes (treated as UTC), or
    anything Python's fromisoformat understands."""
    if not s:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _make_advisor_query_fn(state, helpers: AdvisorHelpers, device_sn: str):
    """Build a closure that runs Claude's tool calls against the local
    DB. Each tool returns a JSON-serialisable dict; on bad inputs we
    return an `error` field rather than raising — Claude can then
    re-issue the call with corrected args."""
    from datetime import datetime, timezone

    main_wh, pack_wh = helpers.capacity_hints(device_sn)

    def _iso(ts: float | int | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

    async def query(name: str, args: dict) -> dict:
        if name == "query_samples":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            bucket_s = int(args.get("bucket_s") or 3600)
            if not start or not end:
                return {"error": "start_iso/end_iso required (ISO 8601)"}
            hours = max(1, (end - start) // 3600 + 1)
            rows = state.energy.history(device_sn, hours=hours, bucket_s=bucket_s)
            # Filter to the requested window (history() goes back N hours
            # from now; we then clip).
            out = []
            for r in rows:
                if r["ts"] < start or r["ts"] >= end:
                    continue
                out.append({
                    "ts": _iso(r["ts"]),
                    "soc": r.get("battery_pct"),
                    "in_w_avg": int(r.get("input_wh") or 0)
                              if bucket_s == 3600 else None,
                    "out_w_avg": int(r.get("output_wh") or 0)
                               if bucket_s == 3600 else None,
                    "solar_w_avg": int(r.get("solar_wh") or 0)
                                 if bucket_s == 3600 else None,
                    "ac_input_w_avg": int(r.get("ac_input_wh") or 0)
                                    if bucket_s == 3600 else None,
                    "in_w_instant": r.get("input_w"),
                    "out_w_instant": r.get("output_w"),
                    "solar_w_instant": r.get("solar_w"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_predictions":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            max_lead = args.get("max_lead_h")
            samples = state.energy.prediction_accuracy(
                device_sn,
                main_capacity_wh=main_wh,
                pack_capacity_wh=pack_wh,
            )
            out = []
            for p in samples:
                if start and p.get("target", 0) < start:
                    continue
                if end and p.get("target", 0) >= end:
                    continue
                if max_lead is not None and p.get("lead_time_h", 0) > max_lead:
                    continue
                out.append({
                    "made_at": _iso(p.get("made_at")),
                    "target": _iso(p.get("target")),
                    "lead_time_h": p.get("lead_time_h"),
                    "predicted_soc": round(p.get("predicted_soc", 0), 1),
                    "actual_soc": round(p.get("actual_soc", 0), 1),
                    "error_pp": round(p.get("error", 0), 1),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_decisions":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            samples = state.energy.smart_charge_analytics(
                device_sn, days=90,
                main_capacity_wh=main_wh,
                pack_capacity_wh=pack_wh,
            )
            out = []
            for d in samples:
                if start and (d.get("decided_at") or 0) < start:
                    continue
                if end and (d.get("decided_at") or 0) >= end:
                    continue
                out.append({
                    "decided_at": _iso(d.get("decided_at")),
                    "action": d.get("action"),
                    "mode": d.get("mode"),
                    "predicted_sunrise_soc_pct": d.get("predicted_sunrise_soc_pct"),
                    "actual_sunrise_soc_pct": d.get("actual_sunrise_soc_pct"),
                    "target_sunrise_soc_pct": d.get("target_sunrise_soc_pct"),
                    "reason": d.get("reason"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_weather":
            start = _parse_iso(args.get("start_iso")) or 0
            end = _parse_iso(args.get("end_iso")) or int(time.time())
            obs = state.energy.list_weather_observations(since_ts=start, limit=2000)
            out = []
            for w in obs:
                if w["ts"] >= end:
                    continue
                out.append({
                    "hour": _iso(w["ts"]),
                    "ghi_w_m2": w.get("ghi_w_m2"),
                    "cloud_cover_pct": w.get("cloud_cover_pct"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_battery_packs":
            packs = state.energy.latest_battery_packs(device_sn)
            # Per-pack `it` is unconditionally dropped — see the comment
            # on _sanitize_pack_telemetry. Strip historical rows here so
            # the advisor never sees garbage values from before the
            # filter shipped.
            cleaned = [{**r, "internal_temp_c": None} for r in packs]
            return {"rows": cleaned, "row_count": len(cleaned)}

        return {"error": f"unknown tool: {name}"}

    return query


async def _run_advisor_review(state, helpers: AdvisorHelpers,
                              device_sn: str) -> dict:
    """Build the starter bundle, run Claude through the agentic
    multi-turn loop with DB-query tools, persist whatever suggestions
    and anomalies come back."""
    import claude_advisor
    if not claude_advisor.has_usable_key():
        return {"ok": False, "reason": "no_api_key"}
    bundle = await _build_advisor_bundle(state, helpers, device_sn)
    query_fn = _make_advisor_query_fn(state, helpers, device_sn)
    result = await claude_advisor.review(bundle, query_fn=query_fn)
    if result.get("skipped_reason") and result["skipped_reason"] not in ("no_tool_call", "turn_cap_reached"):
        return {"ok": False, "reason": result["skipped_reason"]}

    # Auto-expire stale pending suggestions before adding new ones, so
    # the user's pending list doesn't grow unboundedly.
    state.energy.expire_old_suggestions()

    new_ids: list[int] = []
    for s in result.get("config_suggestions", []):
        try:
            sid = state.energy.insert_suggestion(
                device_sn=device_sn,
                kind="config",
                target=s["target"],
                current_value=s["current_value"],
                proposed_value=s["proposed_value"],
                reasoning=s["reasoning"],
                confidence=s["confidence"],
                severity=None,
            )
            new_ids.append(sid)
        except Exception as e:
            log.warning("advisor: failed to persist suggestion %s: %s", s, e)

    for a in result.get("anomalies", []):
        try:
            sid = state.energy.insert_suggestion(
                device_sn=device_sn,
                kind="anomaly",
                target=None,
                current_value=None,
                proposed_value=None,
                reasoning=a.get("description", ""),
                confidence=None,
                severity=a.get("severity"),
            )
            new_ids.append(sid)
        except Exception as e:
            log.warning("advisor: failed to persist anomaly %s: %s", a, e)

    log.info("advisor: %s — %d suggestions, %d anomalies in %d turns "
             "(%d tool calls, model=%s)",
             device_sn,
             len(result.get("config_suggestions", [])),
             len(result.get("anomalies", [])),
             result.get("turns", 0),
             result.get("tool_calls", 0),
             result.get("model"))
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "new_suggestion_ids": new_ids,
        "model": result.get("model"),
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", 0),
    }


async def _advisor_review_job(state, helpers: AdvisorHelpers,
                              device_sn: str) -> None:
    """Background task body. Updates state.advisor_jobs[device_sn] in place
    so the polling endpoint can report progress + final result without
    holding the HTTP request open through the entire 60-180s review."""
    job = state.advisor_jobs.setdefault(device_sn, {})
    job.update({
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    })
    try:
        result = await _run_advisor_review(state, helpers, device_sn)
        job["finished_at"] = time.time()
        if result.get("ok"):
            job["status"] = "done"
            job["result"] = result
        else:
            job["status"] = "error"
            job["error"] = str(result.get("reason") or "unknown")
    except Exception as e:
        log.exception("advisor: background job failed for %s", device_sn)
        job["finished_at"] = time.time()
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


# ---------- public surface ----------

async def advisor_loop(state, helpers: AdvisorHelpers) -> None:
    """Run the advisor once per device per day. Anchored to ~8am local
    time when location info is available, otherwise just every 24h
    from the first tick. Skipping is cheap (no key / no SDK) so we
    iterate every hour to keep the wake-up logic simple."""
    bo = _backoff.LoopBackoff(max_s=4 * 3600)
    while True:
        try:
            await asyncio.sleep(60)  # warm-up — let credentials load
            try:
                import claude_advisor
            except Exception:
                claude_advisor = None
            if claude_advisor is None or not claude_advisor.has_usable_key():
                # Try again in an hour — user may save a key later.
                await asyncio.sleep(3600)
                continue
            now = time.time()
            tz_off = device_location.get_tz_offset() or 0
            local_hour = (int(now + tz_off) // 3600) % 24
            # Run once when local hour first equals our trigger hour.
            if local_hour == user_settings.get("advisor_trigger_hour"):
                for d in state.energy.list_devices():
                    sn = d.get("device_sn")
                    if not sn:
                        continue
                    last = state.last_advisor_run_by_sn.get(sn, 0.0)
                    if now - last < 23 * 3600:
                        continue
                    try:
                        await _run_advisor_review(state, helpers, sn)
                    except Exception as e:
                        log.warning("advisor loop: %s failed: %s", sn, e)
                    state.last_advisor_run_by_sn[sn] = now
            bo.reset()
        except Exception as e:
            bo.record_failure()
            log.warning("advisor loop iteration failed: %s", e)
        # Tick every hour. The local-time gate inside ensures we only
        # actually run reviews once per device per day.
        await asyncio.sleep(bo.next_sleep(3600))


def install(app: FastAPI, state, helpers: AdvisorHelpers) -> None:
    """Register /api/algorithm/* routes — suggestions list, on-demand
    review (background), apply/dismiss, preview, and audit log."""

    @app.get("/api/algorithm/suggestions")
    def api_alg_suggestions(device_sn: str | None = None,
                            status: str | None = "pending"):
        """List algorithm suggestions. Defaults to status=pending so the UI
        shows what's awaiting the user's decision; pass status='applied' /
        'dismissed' / null (all) to see history."""
        if not device_sn:
            device_sn = state.device.device_sn if state.device else None
        return {
            "device_sn": device_sn,
            "status_filter": status,
            "suggestions": state.energy.list_suggestions(
                device_sn=device_sn, status=status, limit=100,
            ),
        }

    @app.post("/api/algorithm/review_now", status_code=202)
    async def api_alg_review_now(device_sn: str | None = None):
        """Kick off a Claude review in the background and return immediately.

        Reviews routinely run 60-180s with adaptive thinking + multi-turn
        tool calls, which exceeds Cloudflare's 100s edge timeout (HTTP 524).
        So we spawn the review as a background asyncio task and let the UI
        poll /api/algorithm/review_status until done. Re-clicking while one
        is in flight is a no-op (returns the existing job)."""
        if not device_sn:
            device_sn = state.device.device_sn if state.device else None
        if not device_sn:
            raise HTTPException(400, "no active device")
        existing = state.advisor_jobs.get(device_sn)
        if existing and existing.get("status") == "running":
            return {"status": "running", "device_sn": device_sn,
                    "started_at": existing.get("started_at"),
                    "already_running": True}
        asyncio.create_task(_advisor_review_job(state, helpers, device_sn))
        return {"status": "running", "device_sn": device_sn,
                "started_at": time.time(), "already_running": False}

    @app.get("/api/algorithm/review_status")
    async def api_alg_review_status(device_sn: str | None = None):
        """Poll for the latest review job's state for one device."""
        if not device_sn:
            device_sn = state.device.device_sn if state.device else None
        if not device_sn:
            raise HTTPException(400, "no active device")
        job = state.advisor_jobs.get(device_sn)
        if not job:
            return {"status": "idle", "device_sn": device_sn}
        out = {"device_sn": device_sn, **job}
        if job.get("status") == "running":
            out["elapsed_s"] = round(time.time() - (job.get("started_at") or time.time()), 1)
        return out

    @app.post("/api/algorithm/suggestions/{suggestion_id}/apply")
    async def api_alg_suggestion_apply(suggestion_id: int):
        """Apply a single pending suggestion. Re-validates against the
        advisor's whitelist + safety floors at apply time so a config tweak
        that was valid at suggestion time but isn't now (e.g. user lowered
        capacity_wh override) gets rejected. Writes an audit row."""
        import claude_advisor
        s = state.energy.get_suggestion(suggestion_id)
        if not s:
            raise HTTPException(404, "suggestion not found")
        if s["status"] != "pending":
            raise HTTPException(400, f"suggestion is {s['status']}, not pending")
        if s["kind"] != "config":
            raise HTTPException(400, "anomalies are not directly applicable; use dismiss/acknowledge")

        target = s["target"]
        rules = claude_advisor.ALLOWED_TARGETS.get(target)
        if not rules:
            raise HTTPException(400, f"target {target!r} no longer in whitelist")
        proposed = s["proposed_value"]
        try:
            proposed_n = float(proposed)
        except Exception:
            raise HTTPException(400, "proposed_value not numeric") from None
        if proposed_n < rules["min"] or proposed_n > rules["max"]:
            raise HTTPException(400, f"proposed value out of safe range [{rules['min']}, {rules['max']}]")

        # Per-device smart-charge config tweaks are the only kind we
        # currently apply. Forecaster-global params would need a
        # runtime-config layer that we haven't built yet; advisor can
        # surface them as anomalies, but we won't auto-apply them here.
        if not target.startswith("smart_charge."):
            raise HTTPException(400, f"applying {target!r} is not yet supported")
        if rules.get("scope") == "device" and not s.get("device_sn"):
            raise HTTPException(400, "device-scoped suggestion missing device_sn")

        field = target.split(".", 1)[1]
        cfg = smart_charge.get_config(s["device_sn"])
        old = cfg.get(field)
        cfg[field] = int(proposed_n) if isinstance(old, int) else proposed_n
        smart_charge.set_config(cfg, device_sn=s["device_sn"])

        # Persist the audit row + flip suggestion to applied.
        state.energy.record_change(
            suggestion_id=suggestion_id, device_sn=s["device_sn"],
            target=target, old_value=old, new_value=cfg[field],
            reasoning=s.get("reasoning"),
        )
        state.energy.update_suggestion_status(suggestion_id, "applied")
        return {"ok": True, "applied": {target: cfg[field]}, "previous": old}

    @app.post("/api/algorithm/suggestions/{suggestion_id}/dismiss")
    def api_alg_suggestion_dismiss(suggestion_id: int):
        s = state.energy.get_suggestion(suggestion_id)
        if not s:
            raise HTTPException(404, "suggestion not found")
        if s["status"] != "pending":
            return {"ok": True, "already": s["status"]}
        state.energy.update_suggestion_status(suggestion_id, "dismissed")
        return {"ok": True}

    @app.get("/api/algorithm/preview")
    async def api_alg_preview(device_sn: str | None = None):
        """Return the exact starter bundle the advisor sends to Claude as
        its opening user message — minus the system prompt and the tool
        schema. Claude follows up with DB query tools, but this is the
        initial context. Used by the UI's "Show what Claude sees" button
        so the user can verify the data flow without burning an API call."""
        import claude_advisor
        if not device_sn:
            device_sn = state.device.device_sn if state.device else None
        if not device_sn:
            raise HTTPException(400, "no active device")
        bundle = await _build_advisor_bundle(state, helpers, device_sn)
        # Resolve the model at call time — same precedence the actual review
        # uses (env var > anthropic_prefs > DEFAULT_MODEL). Don't reach for
        # a module-level constant; there isn't one any more.
        return {
            "device_sn": device_sn,
            "rendered": claude_advisor._format_starter_bundle(bundle),
            "model": claude_advisor._get_model(),
            "thinking_budget": claude_advisor.THINKING_BUDGET,
            "raw_bundle": bundle,
        }

    @app.get("/api/algorithm/changes")
    def api_alg_changes(device_sn: str | None = None):
        if not device_sn:
            device_sn = state.device.device_sn if state.device else None
        return {
            "device_sn": device_sn,
            "changes": state.energy.list_changes(device_sn, limit=50),
        }
