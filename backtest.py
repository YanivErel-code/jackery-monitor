"""Replay historical smart-charge decisions through the current
compute_plan to validate behavior changes without waiting for fresh
ticks to accumulate.

The replay is *approximate*: per-tick fit values (charge_efficiency,
solar_coefficient, inverter_overhead_pct) are NOT snapshotted in the
DB, so we use whatever the forecaster fits from the truncated history
at each historical T. For the user's typical case where SOC stays
clear of the floor, this is faithful enough to validate "would this
decision have flipped?" For borderline cases where the floor would
have bound, treat results as guidance not ground truth.
"""
from __future__ import annotations

import logging
from typing import Any

import forecaster
import smart_charge

log = logging.getLogger("backtest")


def _replay_one(
    *,
    decided_at: int,
    starting_soc_pct: float,
    target_pct: float,
    capacity_wh: int,
    max_charge_w: float,
    energy_history_to_ts: list[dict[str, Any]],
    weather_hourly: list[dict[str, Any]],
    cost_plan: dict[str, Any],
    tz_offset_seconds: int,
) -> smart_charge.Plan:
    """Run the current compute_plan as if `now` were `decided_at`.
    Builds both the with-floor (display) and counterfactual (no-floor)
    forecasts the new code expects."""
    fcast = forecaster.build_forecast(
        energy_history=energy_history_to_ts,
        weather_hourly=weather_hourly,
        starting_soc_pct=starting_soc_pct,
        capacity_wh=capacity_wh,
        ac_charge_floor_pct=target_pct,
    )
    baseline_fcast = forecaster.build_forecast(
        energy_history=energy_history_to_ts,
        weather_hourly=weather_hourly,
        starting_soc_pct=starting_soc_pct,
        capacity_wh=capacity_wh,
        ac_charge_floor_pct=None,
    )
    cfg = {
        "mode": "active",  # exercise the same code path active mode would
        "target_sunrise_soc_pct": target_pct,
        "max_charge_w": max_charge_w,
    }
    return smart_charge.compute_plan(
        config=cfg,
        current_soc_pct=starting_soc_pct,
        forecast=fcast,
        baseline_forecast=baseline_fcast,
        cost_plan=cost_plan,
        capacity_wh=capacity_wh,
        tz_offset_seconds=tz_offset_seconds,
        now_ts=decided_at,
    )


def replay_decisions(
    *,
    decisions: list[dict[str, Any]],
    full_energy_history: list[dict[str, Any]],
    weather_observations: list[dict[str, Any]],
    capacity_wh: int,
    max_charge_w: float,
    cost_plan: dict[str, Any],
    tz_offset_seconds: int,
    target_override: float | None = None,
) -> list[dict[str, Any]]:
    """For each historical decision, replay through compute_plan and
    return a comparison row. Older decisions first."""
    out: list[dict[str, Any]] = []
    history_sorted = sorted(
        (r for r in full_energy_history if r.get("ts") is not None),
        key=lambda r: r["ts"],
    )
    weather_sorted = sorted(
        (w for w in weather_observations if w.get("ts") is not None),
        key=lambda w: w["ts"],
    )
    for d in sorted(decisions, key=lambda x: x.get("decided_at") or 0):
        ts = int(d.get("decided_at") or 0)
        sunrise_ts = d.get("sunrise_ts")
        soc = d.get("current_soc_pct")
        if not ts or not sunrise_ts or soc is None:
            continue
        target = float(target_override
                       if target_override is not None
                       else (d.get("target_sunrise_soc_pct") or 25))
        # Truncate history at T so the fits and load-profile only see
        # what was knowable at decision time.
        history_to_ts = [r for r in history_sorted if r["ts"] <= ts]
        # Weather hours we care about: a few days of lookback for the
        # solar-coefficient fit + 72h lookahead for the prediction.
        weather_filtered = [
            w for w in weather_sorted
            if (ts - 14 * 86400) <= w["ts"] <= (ts + 72 * 3600)
        ]
        try:
            plan = _replay_one(
                decided_at=ts,
                starting_soc_pct=float(soc),
                target_pct=target,
                capacity_wh=capacity_wh,
                max_charge_w=max_charge_w,
                energy_history_to_ts=history_to_ts,
                weather_hourly=weather_filtered,
                cost_plan=cost_plan,
                tz_offset_seconds=tz_offset_seconds,
            )
        except Exception as e:
            log.debug("backtest replay failed at ts=%s: %s", ts, e)
            out.append({
                "ts": ts,
                "old_action": d.get("action"),
                "new_action": None,
                "would_flip": None,
                "error": str(e),
            })
            continue
        out.append({
            "ts": ts,
            "old_action": d.get("action"),
            "new_action": plan.action,
            "would_flip": plan.action != d.get("action"),
            "old_reason": d.get("reason"),
            "new_reason": plan.reason,
            "soc": float(soc),
            "old_predicted_sunrise": d.get("predicted_sunrise_soc_pct"),
            "new_predicted_sunrise": plan.predicted_sunrise_soc_pct,
            "new_baseline_sunrise": plan.baseline_predicted_sunrise_soc_pct,
            "target": target,
            "planned_hours": plan.planned_hours or [],
            "extension_active": plan.extension_active,
            "deficit_kwh": plan.deficit_kwh,
            "sunrise_ts": plan.sunrise_ts,
        })
    return out


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up replay rows into a quick-look summary."""
    by_old: dict[str, int] = {}
    by_new: dict[str, int] = {}
    n_flip = 0
    n_ext = 0
    n_err = 0
    flip_pairs: dict[str, int] = {}
    for r in results:
        old = r.get("old_action") or "?"
        new = r.get("new_action") or "?"
        by_old[old] = by_old.get(old, 0) + 1
        by_new[new] = by_new.get(new, 0) + 1
        if r.get("error"):
            n_err += 1
            continue
        if r.get("would_flip"):
            n_flip += 1
            flip_pairs[f"{old}→{new}"] = flip_pairs.get(f"{old}→{new}", 0) + 1
        if r.get("extension_active"):
            n_ext += 1
    return {
        "n": len(results),
        "n_flipped": n_flip,
        "n_extension": n_ext,
        "n_error": n_err,
        "by_action_old": by_old,
        "by_action_new": by_new,
        "flip_pairs": flip_pairs,
    }
