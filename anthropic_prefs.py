"""User preferences for the Anthropic-powered Claude integrations.

Two roles, picked per call site:
  - advisor:  daily heavy review by claude_advisor.py (multi-turn,
              extended thinking, optional 1M context). Quality matters
              more than cost; default Opus 4.7 + 1M + high effort.
  - narrator: per-decision 1-2 sentence rationale by claude_narrator.py.
              Cheap + fast matters; default Haiku 4.5.

Per-role knobs:
  - advisor:  model, 1m_context (bool), thinking_effort
              (low|medium|high — adaptive thinking on Opus 4.7+)
  - narrator: model only

Storage: /data/anthropic-prefs.json (plain JSON; non-sensitive). The
encrypted-at-rest API key lives separately in anthropic_creds.py.

Reading: claude_advisor and claude_narrator call the getters at request
time, NOT at module load — so a UI change applies on the next tick
without a container restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

log = logging.getLogger("anthropic_prefs")

PREFS_PATH = os.environ.get(
    "JACKERY_ANTHROPIC_PREFS_FILE", "/data/anthropic-prefs.json"
)

# Sensible defaults preserved from the pre-prefs era so that an existing
# deploy without the file behaves identically until the user opts in.
# advisor_1m_context=True matches the unconditional behavior of the
# old code, which always sent the context-1m beta header.
DEFAULTS: dict[str, Any] = {
    "advisor_model": "claude-opus-4-7",
    "advisor_1m_context": True,
    "advisor_thinking_effort": "high",
    "narrator_model": "claude-haiku-4-5",
}

# Roles the UI surfaces. Keep small; new roles need a corresponding
# getter caller and a default above.
ROLES: tuple[str, ...] = ("advisor", "narrator")

# Adaptive-thinking effort levels per the Anthropic API spec. Higher
# = the model self-allocates a larger thinking budget. Reject other
# values to avoid silent typos misconfiguring the daily review.
VALID_THINKING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(PREFS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("anthropic prefs file unreadable (%s); using defaults", e)
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PREFS_PATH) or ".", exist_ok=True)
        tmp = PREFS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PREFS_PATH)
    except Exception as e:
        log.error("failed to save anthropic prefs: %s", e)


def get_model(role: str) -> str:
    """Return the configured model for `role` ('advisor' | 'narrator').
    Falls back to DEFAULTS when no value is persisted, so callers never
    see None and the system runs out-of-the-box without prefs being
    set."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    key = f"{role}_model"
    with _lock:
        data = _load()
    val = data.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return DEFAULTS[key]


def get_1m_context() -> bool:
    """Whether the advisor should opt into the 1M-context beta header
    on its next call. Only applies to the advisor — narrator outputs
    are tiny so 1M is never relevant there."""
    with _lock:
        data = _load()
    val = data.get("advisor_1m_context")
    if isinstance(val, bool):
        return val
    return bool(DEFAULTS["advisor_1m_context"])


def get_thinking_effort() -> str:
    """Adaptive-thinking effort for the advisor. Returns one of
    VALID_THINKING_EFFORTS, falling back to DEFAULT when an unknown
    value is persisted (defensive against schema drift)."""
    with _lock:
        data = _load()
    val = data.get("advisor_thinking_effort")
    if isinstance(val, str) and val.strip().lower() in VALID_THINKING_EFFORTS:
        return val.strip().lower()
    return DEFAULTS["advisor_thinking_effort"]


def get_all() -> dict[str, Any]:
    """Snapshot of all preferences with defaults filled in.

    Flat shape (matches the keys the UI/API contract expects); the
    UI reads `advisor_model`, `advisor_1m_context`,
    `advisor_thinking_effort`, `narrator_model` directly."""
    return {
        "advisor_model": get_model("advisor"),
        "advisor_1m_context": get_1m_context(),
        "advisor_thinking_effort": get_thinking_effort(),
        "narrator_model": get_model("narrator"),
    }


def set_models(*,
               advisor_model: str | None = None,
               advisor_1m_context: bool | None = None,
               advisor_thinking_effort: str | None = None,
               narrator_model: str | None = None) -> dict[str, Any]:
    """Persist any non-None field; leave the rest untouched. Returns
    the resulting full snapshot. Invalid thinking_effort values are
    silently dropped (no half-saves) so the file never holds a value
    the advisor would reject at API call time."""
    with _lock:
        data = _load()
        if advisor_model and advisor_model.strip():
            data["advisor_model"] = advisor_model.strip()
        if advisor_1m_context is not None:
            data["advisor_1m_context"] = bool(advisor_1m_context)
        if advisor_thinking_effort:
            v = str(advisor_thinking_effort).strip().lower()
            if v in VALID_THINKING_EFFORTS:
                data["advisor_thinking_effort"] = v
        if narrator_model and narrator_model.strip():
            data["narrator_model"] = narrator_model.strip()
        _save(data)
    return get_all()
