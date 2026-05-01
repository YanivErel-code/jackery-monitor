"""
Daily algorithm advisor — Claude Opus 4.7 (1M context) + extended thinking,
agentic with DB-query tools.

Instead of pre-bundling all the data we think Claude might need, we send
a compact starter context (current config + accuracy summary by lead-time
bucket) and expose DB-query tools. Claude reads the summary, hypothesizes
about residuals, and *itself* requests the specific data it wants to
inspect — like a human analyst running ad-hoc SQL.

Multi-turn flow:
  1. Server gives Claude a compact initial bundle.
  2. Claude reasons (extended thinking).
  3. Claude calls one of the query tools with a specific window/resolution.
  4. Server runs the query, returns results.
  5. Loop until Claude calls submit_algorithm_review (or the turn cap
     forces a final review pass).

This is meaningfully more useful than a static bundle because:
  - Claude drills into anomalies it actually spots (not what we anticipated)
  - Picks resolution per question (1-min for sub-hour drains, daily for
    longer-term trends)
  - Doesn't burn context on rows it doesn't end up reading

Cost: more tool turns = more API calls. With Opus 4.7's 1M context window
we can run 20+ turns comfortably; expect $0.40-0.80 per review (vs
$0.20-0.30 single-call). The depth of analysis is dramatically better.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic_creds

log = logging.getLogger("claude_advisor")

# Latest Claude Opus model. The 1M-context capability is opted into via
# the anthropic-beta header below — for models that don't support it the
# header is ignored. Override via env var if Anthropic ships a newer
# point release before this codebase updates.
MODEL = os.environ.get("JACKERY_ADVISOR_MODEL", "claude-opus-4-7")

# Beta header to opt into the 1M-token context window. The advisor's
# multi-turn agent loop with tool results can grow context fast on a
# data-rich review; 1M gives plenty of headroom. Header value tracks
# Anthropic's published beta name; override via env var if it rotates.
CONTEXT_1M_HEADER = os.environ.get(
    "JACKERY_ADVISOR_BETA", "context-1m-2025-08-07")

# Extended thinking effort level for adaptive-thinking models (Opus 4.7+).
# Valid: "low" | "medium" | "high". Higher = the model will think longer
# when the task warrants it. The model self-allocates the budget.
# (Older models use a fixed token budget instead — see THINKING_BUDGET.)
THINKING_EFFORT = os.environ.get("JACKERY_ADVISOR_THINKING_EFFORT", "high")

# Legacy fixed-budget thinking, used only if JACKERY_ADVISOR_THINKING_MODE
# is set to "budget" (for older models that don't accept adaptive).
THINKING_BUDGET = int(os.environ.get("JACKERY_ADVISOR_THINKING", "16000"))
THINKING_MODE = os.environ.get("JACKERY_ADVISOR_THINKING_MODE", "adaptive")

# Output token cap per turn. Tool-call turns are usually short; the final
# submit_algorithm_review turn can be longer if many anomalies need
# explaining.
MAX_TOKENS = int(os.environ.get("JACKERY_ADVISOR_MAX_TOKENS", "32000"))

# Maximum number of agent turns (Claude → tool → Claude → tool → …) per
# review. Each turn is one API round-trip. Cap exists so a buggy prompt
# can't loop forever.
MAX_TURNS = int(os.environ.get("JACKERY_ADVISOR_MAX_TURNS", "20"))

# Per-query row cap returned to Claude. Keeps single tool results in
# manageable size; if Claude needs more it can paginate via narrower
# windows or coarser bucket_s.
MAX_QUERY_ROWS = 500

# Whitelist of parameters Claude is allowed to propose changes to.
# Anything outside this falls back to an anomaly callout (no auto-apply).
ALLOWED_TARGETS: dict[str, dict[str, Any]] = {
    "smart_charge.max_charge_w": {
        "scope": "device",
        "min": 50, "max": 3000,
    },
    "smart_charge.target_sunrise_soc_pct": {
        "scope": "device",
        "min": 15, "max": 60,
    },
    "smart_charge.max_on_duration_minutes": {
        "scope": "device",
        "min": 30, "max": 720,
    },
}


def _resolve_key() -> str | None:
    return anthropic_creds.load() or os.environ.get("ANTHROPIC_API_KEY") or None


def has_usable_key() -> bool:
    return _resolve_key() is not None


def _format_starter_bundle(bundle: dict) -> str:
    """Compact opener — current state + 14d accuracy summary + smart-charge
    history. Claude uses query tools to drill into specifics."""
    lines: list[str] = []
    lines.append("# Algorithm review")
    lines.append("")
    lines.append(f"Window: {bundle['window_label']}")
    lines.append(f"Device: {bundle['device_label']} (SN tail …{bundle['device_sn'][-6:]})")
    lines.append(f"Capacity: {bundle['capacity_wh']} Wh "
                 f"({bundle.get('pack_count', 0)} expansion packs)")
    if bundle.get("system_soc_pct") is not None:
        lines.append(f"Current SOC: main {bundle['main_soc_pct']}%, "
                     f"system {bundle['system_soc_pct']}%")
    lines.append("")
    lines.append("## Current smart-charge config")
    for k, v in (bundle.get("smart_charge_config") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    overhead_w = bundle.get("fitted_idle_overhead_w")
    overhead_n = bundle.get("fitted_idle_overhead_n_windows", 0)
    if overhead_w is not None:
        lines.append("## Auto-fitted parasitic overhead")
        lines.append(
            f"- idle_overhead_w (used in load model): {overhead_w} W "
            f"(fit from {overhead_n} clean discharge windows)"
        )
        if overhead_n < 5:
            lines.append("  ⚠ Few windows — value is still the population "
                         "default. Wait until more discharge data accumulates "
                         "before flagging it as wrong.")
        lines.append("")

    # Recent code changes that re-define what historical data means.
    # Without this hint the advisor re-flags the same bug every review
    # because the 48h window still contains predictions/decisions from
    # before the fix shipped.
    changes = bundle.get("recent_code_changes") or []
    if changes:
        lines.append("## Recent code changes (read carefully)")
        lines.append(
            "The forecaster + smart-charge code was modified at the "
            "timestamps below. Historical samples / predictions / "
            "decisions older than the relevant timestamp were generated "
            "by the OLD code. Treat patterns visible only in pre-fix "
            "data as already-addressed and do NOT re-suggest the same "
            "fix. Only flag a problem if you can show it in data "
            "produced AFTER the relevant fix timestamp."
        )
        for c in changes:
            lines.append(f"- {c.get('ts_iso')} [{c.get('subsystem')}]: "
                         f"{c.get('summary')}")
        lines.append("")

    accuracy = bundle.get("forecast_accuracy_summary") or {}
    if accuracy:
        lines.append("## Forecast accuracy by lead time (last 14d)")
        for bucket, stats in accuracy.items():
            lines.append(f"- {bucket}: n={stats.get('n')}, MAE {stats.get('mae')} pp")
        lines.append("")

    decisions = bundle.get("recent_decisions") or []
    if decisions:
        lines.append("## Recent smart-charge decisions (last 7d)")
        for d in decisions:
            lines.append(
                f"- {d.get('decided_iso')} {d.get('action', '?').upper()} "
                f"[{d.get('mode')}] "
                f"pred_sunrise={d.get('predicted_sunrise_soc_pct')}% → "
                f"actual={d.get('actual_sunrise_soc_pct')}% "
                f"target={d.get('target_sunrise_soc_pct')}% "
                f"reason: {d.get('reason')}"
            )
        lines.append("")

    lines.append("## Your tools")
    lines.append("Use the query_* tools to investigate specific windows at "
                 "the resolution you choose. Then call submit_algorithm_review "
                 "when you have a final diagnosis.")
    lines.append("")
    lines.append("Reasonable starting points if you want hints:")
    lines.append("- query_predictions on the last 48h to see error patterns")
    lines.append("- query_samples on overnight windows where SOC drained "
                 "(use bucket_s=300 for 5-min, 60 for 1-min)")
    lines.append("- query_weather to correlate solar misses with cloud cover")

    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You are a careful, honest algorithm advisor for a residential "
        "solar-battery dashboard. The user runs a deterministic forecaster "
        "+ smart-charge controller against real telemetry; you review "
        "yesterday's results to suggest tunable improvements.\n\n"
        "How to investigate:\n"
        " - You have query_* tools to fetch any window of data at any "
        "resolution. Use them — the starter context is intentionally a "
        "summary, not the full dataset. Drill into anomalies, "
        "predicted-vs-actual gaps, and overnight drain patterns.\n"
        " - Combine queries: when prediction error is high at a specific "
        "target hour, query the actual samples + weather for that window "
        "to diagnose the cause (load model wrong? solar regression "
        "off? brief spike vs sustained drain?).\n"
        " - Reconcile SOC drain with reported power. SOC change x capacity "
        "should match the integrated power delta. Big mismatches = "
        "measurement gap (inverter overhead, SOC drift, unmeasured loads).\n"
        "\n"
        "Hard rules for output:\n"
        " - Only suggest changes to parameters in the submit tool's enum. "
        "Code/model bugs go in `anomalies` with severity 'warn'.\n"
        " - Each suggestion: numeric current_value + numeric proposed_value "
        "+ reasoning citing specific data + confidence (high/medium/low).\n"
        " - High confidence = systematic, multi-day, clear cause. "
        "Medium = plausible signal but few datapoints. Low = speculative.\n"
        " - Returning zero suggestions is FINE if the system is tracking "
        "well. Say so in `summary`.\n"
        " - target_sunrise_soc_pct must be in [15, 60].\n"
        " - When done, call submit_algorithm_review. Don't keep querying "
        "indefinitely; commit to a diagnosis once the data supports one.\n"
    )


# ---- query tools (executed server-side via the dispatcher) ----

QUERY_TOOLS: list[dict] = [
    {
        "name": "query_samples",
        "description": (
            "Time-bucketed power flow + SOC samples. Returns one row per "
            "bucket: ts (ISO), soc, in_w_avg (integrated W during the "
            "bucket = true average power), out_w_avg, solar_w_avg, "
            "ac_input_w_avg, plus _instant fields (last sample seen, "
            "useful for spotting spikes the average smooths out)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string",
                              "description": "ISO 8601 UTC. e.g. '2026-04-29T06:00:00'"},
                "end_iso": {"type": "string"},
                "bucket_s": {
                    "type": "integer",
                    "enum": [60, 300, 900, 3600, 86400],
                    "description": "60=1min, 300=5min, 900=15min, 3600=1h, 86400=daily",
                },
            },
            "required": ["start_iso", "end_iso", "bucket_s"],
        },
    },
    {
        "name": "query_predictions",
        "description": (
            "Predicted-vs-actual SOC pairs for past forecast targets. Each "
            "row: made_at (when prediction was made), target (when it "
            "was for), lead_time_h, predicted_soc, actual_soc, error pp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string",
                              "description": "Filter: target time >= this"},
                "end_iso": {"type": "string"},
                "max_lead_h": {"type": "integer",
                               "description": "Optional: only include "
                                              "predictions with lead time ≤ this"},
            },
            "required": ["start_iso", "end_iso"],
        },
    },
    {
        "name": "query_decisions",
        "description": (
            "Smart-charge decision history joined to actual sunrise SOC. "
            "Each row: decided_at, action (on/off/skip), mode, "
            "predicted_sunrise_soc_pct, actual_sunrise_soc_pct, "
            "target_sunrise_soc_pct, reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
            },
            "required": ["start_iso", "end_iso"],
        },
    },
    {
        "name": "query_weather",
        "description": (
            "Hourly weather observations (Open-Meteo). Each row: hour, "
            "ghi_w_m2, cloud_cover_pct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
            },
            "required": ["start_iso", "end_iso"],
        },
    },
    {
        "name": "query_battery_packs",
        "description": (
            "Latest per-expansion-battery snapshot (5000 Plus + packs). "
            "Each row: pack_sn, soc_pct, input_w, output_w, "
            "internal_temp_c."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _review_tool() -> dict:
    return {
        "name": "submit_algorithm_review",
        "description": "Submit your final review. Call this exactly once when you have a diagnosis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1-3 sentence plain-English overview of what the data shows.",
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


# Type alias: the query callback the server passes in.
# Signature: query_fn(tool_name, tool_input) -> dict (JSON-serializable result).
QueryFn = Callable[[str, dict], Awaitable[dict]]


async def review(bundle: dict, *, query_fn: QueryFn) -> dict:
    """Multi-turn agent loop. The server provides a compact starter
    bundle and a query_fn that executes Claude's tool calls against the
    DB. Returns the final review payload (summary, suggestions,
    anomalies) plus diagnostic metadata (turn count, tool calls)."""
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
    tools = [*QUERY_TOOLS, _review_tool()]
    messages: list[dict] = [
        {"role": "user", "content": _format_starter_bundle(bundle)},
    ]
    tool_calls_made = 0
    turn = 0
    final_payload: dict | None = None
    last_text = ""

    while turn < MAX_TURNS:
        turn += 1
        log.info("advisor turn %d/%d (tool calls so far: %d)",
                 turn, MAX_TURNS, tool_calls_made)
        try:
            # MUST use streaming for long-running requests. The SDK
            # rejects non-streaming calls that may exceed 10 min with
            # a hard ValueError; with thinking + 32k max_tokens + tool
            # use, every advisor turn trips that heuristic.
            #
            # Thinking API differs between model generations:
            #   - Older Opus/Sonnet: {"type": "enabled", "budget_tokens": N}
            #   - Opus 4.7+: {"type": "adaptive"} + extra_body output_config
            # The model rejects the wrong shape. JACKERY_ADVISOR_THINKING_MODE
            # selects between them; default "adaptive" matches Opus 4.7.
            kwargs: dict = dict(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                tools=tools,
                tool_choice={"type": "auto"},
                system=_system_prompt(),
                messages=messages,
                extra_headers={"anthropic-beta": CONTEXT_1M_HEADER},
            )
            if THINKING_MODE == "adaptive":
                kwargs["thinking"] = {"type": "adaptive"}
                # output_config is a top-level body field on newer models,
                # not a kwarg on the SDK call — pass via extra_body so the
                # SDK forwards it untouched.
                kwargs["extra_body"] = {
                    "output_config": {"effort": THINKING_EFFORT},
                }
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET,
                }
            async with client.messages.stream(**kwargs) as stream:
                async for _ in stream:
                    pass
                resp = await stream.get_final_message()
        except Exception as e:
            msg = str(e)
            if len(msg) > 300:
                msg = msg[:300] + "…"
            log.warning("advisor: Claude call failed at turn %d: %s: %s",
                        turn, type(e).__name__, msg)
            return {"summary": last_text or "", "config_suggestions": [],
                    "anomalies": [],
                    "skipped_reason": f"api_error: {type(e).__name__}: {msg}",
                    "turns": turn, "tool_calls": tool_calls_made}

        # Capture text + tool_use blocks. Thinking blocks pass through
        # unchanged in the messages list (required for follow-up turns).
        tool_calls_in_turn: list = []
        text_blocks: list[str] = []
        for block in (resp.content or []):
            btype = getattr(block, "type", None)
            if btype == "text":
                text = getattr(block, "text", "")
                if text:
                    text_blocks.append(text)
            elif btype == "tool_use":
                if block.name == "submit_algorithm_review":
                    final_payload = dict(block.input or {})
                    break
                tool_calls_in_turn.append(block)
        if text_blocks:
            last_text = " ".join(text_blocks).strip()

        if final_payload is not None:
            log.info("advisor: submit_algorithm_review called at turn %d", turn)
            break

        if not tool_calls_in_turn:
            # No tools called and no submit — model decided to stop.
            # We'll treat the last text as the summary.
            log.info("advisor: model stopped without submit at turn %d "
                     "(stop_reason=%s)", turn,
                     getattr(resp, "stop_reason", "?"))
            break

        # Execute each tool call and accumulate results.
        tool_results = []
        for tc in tool_calls_in_turn:
            tool_calls_made += 1
            try:
                result = await query_fn(tc.name, dict(tc.input or {}))
            except Exception as e:
                log.warning("advisor: tool %s failed: %s: %s",
                            tc.name, type(e).__name__, e)
                result = {"error": f"{type(e).__name__}: {e}"}
            # Compact the result if oversized — Claude will see it
            # truncated and can request a narrower window.
            content = json.dumps(result, default=str)
            if len(content) > 80000:
                content = content[:80000] + "…(truncated; narrow your query)"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": content,
            })

        # Append assistant turn (must include thinking blocks unchanged)
        # and the user turn carrying tool_results.
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

    if final_payload is None:
        # Hit MAX_TURNS without commit. Surface whatever text we got.
        log.warning("advisor: hit MAX_TURNS=%d without submit; "
                    "tool_calls=%d", MAX_TURNS, tool_calls_made)
        return {"summary": last_text[:1000] if last_text else
                "Review hit turn cap without final commit.",
                "config_suggestions": [], "anomalies": [],
                "skipped_reason": "turn_cap_reached",
                "turns": turn, "tool_calls": tool_calls_made}

    raw_suggestions = final_payload.get("config_suggestions") or []
    valid: list[dict] = []
    for s in raw_suggestions:
        ok, why = _validate_suggestion(s)
        if ok:
            valid.append(s)
        else:
            log.info("advisor: rejected suggestion %s: %s", s, why)

    return {
        "summary": final_payload.get("summary") or "",
        "config_suggestions": valid,
        "anomalies": final_payload.get("anomalies") or [],
        "model": MODEL,
        "thinking_used": True,
        "turns": turn,
        "tool_calls": tool_calls_made,
    }


# Re-export the whitelist for server-side validation symmetry.
__all__ = ["ALLOWED_TARGETS", "QueryFn", "has_usable_key", "review"]
