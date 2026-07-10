"""
Optional AI narration of smart-charge decisions.

Each smart-charge tick that fires (mode != off) computes a Plan. When the
user has both saved an API key for the active provider AND enabled
`claude_enabled` in the per-device config, this module turns that Plan
into a 1-2 sentence explanation attached to the persisted decision row.

Provider (anthropic | openai) is selected in Settings — see
anthropic_prefs.get_provider(). Key resolution per provider:
  1. saved key (UI Settings page) — anthropic_creds / openai_creds
  2. ANTHROPIC_API_KEY / OPENAI_API_KEY env var (ops parity)

Cost: one API call per fired decision (typically 1-3/day). Defaults are
the cheap/fast small models per provider.

Validation: validate_key() lets the UI sanity-check a key on save.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import anthropic_creds
import anthropic_prefs
import openai_creds

log = logging.getLogger("claude_narrator")

MAX_TOKENS = 200

_SYSTEM = (
    "You explain solar-battery automation decisions in 1-2 plain English "
    "sentences for a homeowner. No jargon, no preamble. Lead with the "
    "action and the reason. Mention the relevant numbers (predicted SOC, "
    "target, deficit) only when they clarify the decision."
)


def _get_model() -> str:
    # Provider-aware: returns the active provider's narrator model.
    return anthropic_prefs.get_model("narrator")


def _resolve_key(provider: str | None = None) -> str | None:
    """UI-saved key takes precedence; env var is the ops fallback."""
    provider = provider or anthropic_prefs.get_provider()
    if provider == "openai":
        return openai_creds.load() or os.environ.get("OPENAI_API_KEY") or None
    return anthropic_creds.load() or os.environ.get("ANTHROPIC_API_KEY") or None


def has_usable_key() -> bool:
    """Whether the ACTIVE provider has a usable key."""
    return _resolve_key() is not None


async def validate_key(api_key: str, provider: str = "anthropic") -> tuple[bool, str]:
    """Smoke-test a candidate API key for `provider` with a tiny one-shot
    call. Returns (ok, message). Used by the Settings save endpoint so we
    refuse to persist a key that won't actually work."""
    if provider == "openai":
        return await _validate_openai(api_key)
    return await _validate_anthropic(api_key)


async def _validate_anthropic(api_key: str) -> tuple[bool, str]:
    if not api_key or not api_key.startswith("sk-ant-"):
        return False, "Anthropic API keys start with `sk-ant-`. Check the value you pasted."
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return False, "Anthropic SDK not installed in this image — rebuild required."
    client = AsyncAnthropic(api_key=api_key)
    try:
        await client.messages.create(
            model=anthropic_prefs.get_model("narrator", provider="anthropic"),
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, "ok"
    except Exception as e:
        msg = str(e)
        return False, (msg[:200] + "…") if len(msg) > 200 else msg


async def _validate_openai(api_key: str) -> tuple[bool, str]:
    if not api_key or not api_key.startswith("sk-"):
        return False, "OpenAI API keys start with `sk-`. Check the value you pasted."
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return False, "OpenAI SDK not installed in this image — rebuild required."
    client = AsyncOpenAI(api_key=api_key)
    try:
        # Cheapest confirm of auth + model access. max_completion_tokens is
        # the modern param accepted across current chat models.
        await client.chat.completions.create(
            model=anthropic_prefs.get_model("narrator", provider="openai"),
            max_completion_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, "ok"
    except Exception as e:
        msg = str(e)
        return False, (msg[:200] + "…") if len(msg) > 200 else msg


async def narrate_smart_charge(plan: Any) -> str:
    """Render a 1-2 sentence explanation of the given Plan. Returns "" on
    any failure (missing key, SDK not installed, network blip, model
    error) — narration is purely additive; the decision still stands."""
    provider = anthropic_prefs.get_provider()
    api_key = _resolve_key(provider)
    if not api_key:
        return ""
    facts = _plan_to_prompt(plan)
    try:
        if provider == "openai":
            return await _narrate_openai(api_key, facts)
        return await _narrate_anthropic(api_key, facts)
    except Exception as e:
        log.debug("narration failed (%s): %s", provider, e)
        return ""


async def _narrate_anthropic(api_key: str, facts: str) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key)
    resp = await client.messages.create(
        model=_get_model(),
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": facts}],
    )
    parts = [getattr(b, "text", "") for b in (resp.content or [])]
    return " ".join(p for p in parts if p).strip()[:512]


async def _narrate_openai(api_key: str, facts: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    resp = await client.chat.completions.create(
        model=_get_model(),
        max_completion_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": facts},
        ],
    )
    return (resp.choices[0].message.content or "").strip()[:512]


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
