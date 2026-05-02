"""
Optional Claude narration of smart-charge decisions.

Each smart-charge tick that fires (mode != off) computes a Plan. When the
user has both saved an Anthropic API key AND enabled `claude_enabled` in
the per-device config, this module turns that Plan into a 1-2 sentence
explanation that gets attached to the persisted decision row.

API key resolution order:
  1. saved key from anthropic_creds (UI Settings page)
  2. ANTHROPIC_API_KEY env var (for ops parity with other secrets)

Cost: one API call per fired decision (typically 1-3/day for an active-
mode setup). Haiku 4.5 — pennies per month at this rate.

Validation: validate_key() lets the UI sanity-check a key on save without
having to wait for the next tick to discover it's wrong.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import anthropic_creds
import anthropic_prefs

log = logging.getLogger("claude_narrator")

# Resolved at call time (not module load) so a Settings-tab change
# applies on the next tick without a container restart. Default is
# Haiku — cheap + fast for the per-decision narration use case.
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 200


def _get_model() -> str:
    try:
        return anthropic_prefs.get_model("narrator")
    except Exception:
        return DEFAULT_MODEL


def _resolve_key() -> str | None:
    """UI-saved key takes precedence; env var is the ops-controlled fallback."""
    return anthropic_creds.load() or os.environ.get("ANTHROPIC_API_KEY") or None


def has_usable_key() -> bool:
    return _resolve_key() is not None


async def validate_key(api_key: str) -> tuple[bool, str]:
    """Smoke-test a candidate API key by making a tiny one-shot call.
    Returns (ok, message). Used by the Settings save endpoint so we
    can refuse to persist a key that won't actually work."""
    if not api_key or not api_key.startswith("sk-ant-"):
        return False, "Anthropic API keys start with `sk-ant-`. Check the value you pasted."
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return False, "Anthropic SDK not installed in this image — rebuild required."
    client = AsyncAnthropic(api_key=api_key)
    try:
        # 1-token completion is the cheapest way to confirm auth + model access.
        await client.messages.create(
            model=_get_model(),
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, "ok"
    except Exception as e:
        msg = str(e)
        # Truncate verbose tracebacks to something the UI can show inline.
        if len(msg) > 200:
            msg = msg[:200] + "…"
        return False, msg


async def narrate_smart_charge(plan: Any) -> str:
    """Render a 1-2 sentence explanation of the given Plan dataclass.
    Returns "" on any failure (missing key, SDK not installed, network
    blip, model error). Failures are logged at debug — never raised —
    because narration is purely additive; the decision still stands."""
    api_key = _resolve_key()
    if not api_key:
        return ""
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        log.debug("anthropic SDK not installed: %s", e)
        return ""

    facts = _plan_to_prompt(plan)
    client = AsyncAnthropic(api_key=api_key)
    try:
        resp = await client.messages.create(
            model=_get_model(),
            max_tokens=MAX_TOKENS,
            system=(
                "You explain solar-battery automation decisions in 1-2 plain "
                "English sentences for a homeowner. No jargon, no preamble. "
                "Lead with the action and the reason. Mention the relevant "
                "numbers (predicted SOC, target, deficit) only when they "
                "clarify the decision."
            ),
            messages=[{"role": "user", "content": facts}],
        )
    except Exception as e:
        log.debug("claude narration failed: %s", e)
        return ""

    parts = []
    for block in (resp.content or []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts).strip()[:512]  # match DB column cap


def _plan_to_prompt(plan: Any) -> str:
    """Turn the Plan dataclass / dict into a compact prompt body."""
    d = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan or {})
    fields = []
    fields.append(f"Action chosen: {d.get('action', '?').upper()}")
    fields.append(f"Mode: {d.get('mode', '?')}")
    fields.append(f"Engine reason: {d.get('reason') or 'n/a'}")
    if d.get("current_soc_pct") is not None:
        fields.append(f"Current SOC: {round(d['current_soc_pct'], 1)}%")
    if d.get("predicted_sunrise_soc_pct") is not None:
        fields.append(
            f"Predicted SOC at next sunrise: "
            f"{round(d['predicted_sunrise_soc_pct'], 1)}%"
        )
    if d.get("target_sunrise_soc_pct") is not None:
        fields.append(
            f"Target SOC at sunrise: {round(d['target_sunrise_soc_pct'], 1)}%"
        )
    if d.get("deficit_kwh"):
        fields.append(f"Energy deficit: {round(d['deficit_kwh'], 2)} kWh")
    if d.get("cheapest_rate") is not None:
        fields.append(f"Cheapest rate in charge window: ${d['cheapest_rate']:.3f}/kWh")
    return "\n".join(fields)
