"""
Daily algorithm advisor — Claude Opus + extended thinking.

What I do every morning right now manually (look at predicted-vs-actual,
diagnose patterns, propose tweaks) is exactly what this module automates.

Per-device daily flow:
  1. Bundle last 48h of forecasts, samples, weather, decisions, config
  2. Call Opus with extended thinking enabled — it's a hard reasoning
     task with multi-step diagnosis (cause → mechanism → tweak), so we
     trade cost for quality vs the Haiku narrator
  3. Force a structured response via the tools API (schema-validated
     JSON, no parsing failures)
  4. Validate against a whitelist of tunable parameters + hard safety
     floors before persisting any suggestion
  5. Persist to algorithm_suggestions; user approves/dismisses in UI

Cost: ~one Opus call per device per day. Expensive vs Haiku ($0.20-0.30
per call rough order) but daily cadence keeps monthly cost small AND
the whole point is high-quality diagnosis. Run-on-demand button gives
the user a way to trigger reviews without waiting for the daily tick.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import anthropic_creds

log = logging.getLogger("claude_advisor")

# Latest Claude Opus. Override via env var if Anthropic releases a
# newer one before this codebase ships an update.
MODEL = os.environ.get("JACKERY_ADVISOR_MODEL", "claude-opus-4-5")

# Extended thinking budget — large enough for the model to walk through
# the data systematically (errors at each lead time, hypothesize cause,
# pick a single high-confidence tweak), small enough to keep cost in line.
THINKING_BUDGET = 8000

# Final-response token cap. Most reviews fit easily in 1-2K; allow more
# for days where multiple anomalies need explaining.
MAX_TOKENS = 16000

# Whitelist of parameters Claude is allowed to propose changes to.
# Anything outside this falls back to an anomaly callout (no auto-apply).
ALLOWED_TARGETS: dict[str, dict[str, Any]] = {
    "smart_charge.max_charge_w": {
        "scope": "device",  # per-device
        "min": 50, "max": 3000,
    },
    "smart_charge.target_sunrise_soc_pct": {
        "scope": "device",
        "min": 15, "max": 60,  # hard floor at 15 — never let Claude lower it more
    },
    "smart_charge.max_on_duration_minutes": {
        "scope": "device",
        "min": 30, "max": 720,
    },
}


def _resolve_key() -> str | None:
    return anthropic_creds.load() or os.environ.get("ANTHROPIC_API_KEY") or None


def _format_data_bundle(bundle: dict) -> str:
    """Render the data bundle as a compact, copy-paste-friendly text
    block. Claude sees this as the user-message body. Keeps numeric
    precision low — full precision would just inflate token count
    without improving reasoning quality."""
    lines: list[str] = []
    lines.append(f"Review window: {bundle['window_label']}")
    lines.append(f"Device: {bundle['device_label']} (SN tail …{bundle['device_sn'][-6:]})")
    lines.append(f"Capacity: {bundle['capacity_wh']} Wh "
                 f"({bundle.get('pack_count', 0)} expansion packs)")
    lines.append(f"Current SOC: main {bundle['main_soc_pct']}%, "
                 f"system {bundle['system_soc_pct']}%")
    lines.append("")
    lines.append("Current smart-charge config:")
    for k, v in (bundle.get("smart_charge_config") or {}).items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    accuracy = bundle.get("forecast_accuracy_summary") or {}
    if accuracy:
        lines.append("Forecast accuracy by lead time (last 14d):")
        for bucket, stats in accuracy.items():
            lines.append(f"  {bucket}: n={stats.get('n')}, MAE {stats.get('mae')} pp")
        lines.append("")

    samples = bundle.get("recent_samples") or []
    if samples:
        lines.append("Last 24h hourly samples (ts ISO, soc, "
                     "input_w, output_w, solar_w, ac_in_w):")
        for s in samples:
            lines.append(
                f"  {s['hour']}  soc={s.get('soc')}%  "
                f"in={s.get('input_w')}W  out={s.get('output_w')}W  "
                f"solar={s.get('solar_w')}W  ac_in={s.get('ac_input_w')}W"
            )
        lines.append("")

    weather = bundle.get("recent_weather") or []
    if weather:
        lines.append("Last 24h weather (ts ISO, GHI, cloud %):")
        for w in weather:
            lines.append(f"  {w['hour']}  GHI {w.get('ghi_w_m2')} W/m²  "
                         f"cloud {w.get('cloud_cover_pct')}%")
        lines.append("")

    pred_pairs = bundle.get("recent_predictions") or []
    if pred_pairs:
        lines.append("Recent predicted-vs-actual SOC pairs:")
        for p in pred_pairs:
            lines.append(
                f"  target {p['target_iso']}  lead {p.get('lead_h')}h  "
                f"predicted {p.get('predicted_soc')}%  "
                f"actual {p.get('actual_soc')}%  "
                f"err {p.get('error')}pp"
            )
        lines.append("")

    decisions = bundle.get("recent_decisions") or []
    if decisions:
        lines.append("Smart-charge decisions (last 14d):")
        for d in decisions:
            lines.append(
                f"  {d.get('decided_iso')} {d.get('action', '?').upper()} "
                f"[{d.get('mode')}] "
                f"pred_sunrise {d.get('predicted_sunrise_soc_pct')}% → "
                f"actual {d.get('actual_sunrise_soc_pct')}% "
                f"target {d.get('target_sunrise_soc_pct')}% "
                f"reason: {d.get('reason')}"
            )
        lines.append("")

    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You are a careful, honest algorithm advisor for a residential "
        "solar-battery dashboard. The user runs a deterministic forecaster "
        "+ smart-charge controller against real telemetry, and you review "
        "yesterday's results to suggest tunable improvements.\n\n"
        "Hard rules:\n"
        " - Only suggest changes to the explicit whitelist of parameters "
        "in the tool schema. If the symptom looks like a code bug or "
        "model-structure issue, surface it as an anomaly with severity "
        "'warn' — do NOT propose a config tweak as a workaround.\n"
        " - Be specific. Each suggestion needs a numeric current value, "
        "a numeric proposed value, and a reasoning that cites the data.\n"
        " - High confidence: the data clearly supports the change "
        "(systematic miss across multiple days, clear cause). Medium: "
        "plausible signal but only a few data points. Low: speculative.\n"
        " - It is FINE to return zero suggestions. If the system is "
        "tracking well, say so in the summary and return empty arrays.\n"
        " - It is BAD to propose target_sunrise_soc_pct < 15 or > 60.\n"
        " - Anomalies are observations the user should know about even "
        "if no config change applies (e.g. 'main inverter pulled 3 kW "
        "between 2-4 am yesterday — unusual, possibly a defrost cycle').\n"
        "\n"
        "Use extended thinking to walk through the data systematically "
        "before deciding. Don't skip directly to suggestions; first "
        "diagnose the residual error pattern."
    )


def _review_tool() -> dict:
    return {
        "name": "submit_algorithm_review",
        "description": "Submit your structured review of yesterday's algorithm performance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence plain-English overview of how the system did.",
                },
                "config_suggestions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "enum": list(ALLOWED_TARGETS.keys()),
                            },
                            "current_value": {"type": "number"},
                            "proposed_value": {"type": "number"},
                            "reasoning": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                        },
                        "required": ["target", "current_value", "proposed_value",
                                     "reasoning", "confidence"],
                    },
                },
                "anomalies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["info", "warn"],
                            },
                        },
                        "required": ["description", "severity"],
                    },
                },
            },
            "required": ["summary", "config_suggestions", "anomalies"],
        },
    }


def _validate_suggestion(s: dict) -> tuple[bool, str]:
    target = s.get("target")
    if target not in ALLOWED_TARGETS:
        return False, f"target {target!r} not whitelisted"
    rules = ALLOWED_TARGETS[target]
    try:
        proposed = float(s["proposed_value"])
    except Exception:
        return False, "proposed_value not numeric"
    if proposed < rules["min"] or proposed > rules["max"]:
        return False, (f"proposed {proposed} out of range "
                       f"[{rules['min']}, {rules['max']}]")
    return True, "ok"


async def review(bundle: dict) -> dict:
    """Run the daily review against a pre-built data bundle. Returns
    the parsed tool-call payload (summary, config_suggestions filtered
    to validated entries, anomalies). Empty/skipped on missing key or
    SDK-not-installed; the caller is responsible for skipping the
    persistence step in those cases."""
    api_key = _resolve_key()
    if not api_key:
        log.info("advisor: no Anthropic key — skipping review")
        return {"summary": "", "config_suggestions": [], "anomalies": [],
                "skipped_reason": "no_api_key"}
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        log.warning("advisor: anthropic SDK not installed: %s", e)
        return {"summary": "", "config_suggestions": [], "anomalies": [],
                "skipped_reason": "sdk_missing"}

    client = AsyncAnthropic(api_key=api_key)
    user_msg = _format_data_bundle(bundle)
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
            tools=[_review_tool()],
            tool_choice={"type": "tool", "name": "submit_algorithm_review"},
            system=_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        log.warning("advisor: Claude call failed: %s", e)
        return {"summary": "", "config_suggestions": [], "anomalies": [],
                "skipped_reason": f"api_error: {type(e).__name__}"}

    payload: dict | None = None
    for block in (resp.content or []):
        # Skip thinking blocks — we only want the tool_use args.
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_algorithm_review":
            payload = dict(block.input or {})
            break
    if payload is None:
        log.warning("advisor: no tool_use block in response")
        return {"summary": "", "config_suggestions": [], "anomalies": [],
                "skipped_reason": "no_tool_call"}

    # Whitelist + range-check each suggestion. Drop invalid ones rather
    # than rejecting the whole review — partial output is still useful.
    raw_suggestions = payload.get("config_suggestions") or []
    valid: list[dict] = []
    for s in raw_suggestions:
        ok, why = _validate_suggestion(s)
        if ok:
            valid.append(s)
        else:
            log.info("advisor: rejected suggestion %s: %s", s, why)

    return {
        "summary": payload.get("summary") or "",
        "config_suggestions": valid,
        "anomalies": payload.get("anomalies") or [],
        "model": MODEL,
        "thinking_used": True,
    }


def has_usable_key() -> bool:
    return _resolve_key() is not None


# Re-export the whitelist so server.py can validate apply requests
# against the same source of truth without duplicating constants.
__all__ = ["ALLOWED_TARGETS", "has_usable_key", "review"]
