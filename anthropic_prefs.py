"""User preferences for which Claude model to use per role.

Two roles, picked per Anthropic call site:
  - advisor:  daily heavy review by claude_advisor.py (multi-turn,
              extended thinking, 1M-context). Reasoning quality matters
              more than cost; default Opus.
  - narrator: per-decision 1-2 sentence rationale by claude_narrator.py.
              Cheap + fast matters; default Haiku.

Storage: /data/anthropic-prefs.json (plain JSON; non-sensitive). The
encrypted-at-rest API key lives separately in anthropic_creds.py.

Reading: claude_advisor and claude_narrator call get_model() at request
time, NOT at module load — so a UI change applies on the next tick
without a container restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger("anthropic_prefs")

PREFS_PATH = os.environ.get(
    "JACKERY_ANTHROPIC_PREFS_FILE", "/data/anthropic-prefs.json"
)

# Sensible defaults preserved from the pre-prefs era so that an existing
# deploy without the file behaves identically until the user opts in.
DEFAULTS: dict[str, str] = {
    "advisor_model": "claude-opus-4-7",
    "narrator_model": "claude-haiku-4-5",
}

# Roles the UI surfaces. Keep small; new roles need a corresponding
# get_model() caller and a default above.
ROLES: tuple[str, ...] = ("advisor", "narrator")

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


def get_all() -> dict[str, str]:
    """Snapshot of all per-role model preferences with defaults filled
    in. Used by the Settings UI to populate the dropdowns."""
    return {role: get_model(role) for role in ROLES}


def set_models(*, advisor_model: str | None = None,
               narrator_model: str | None = None) -> dict[str, str]:
    """Persist whichever of {advisor_model, narrator_model} is provided.
    Empty / None values are ignored (use this to update one without
    touching the other). Returns the resulting full snapshot."""
    with _lock:
        data = _load()
        if advisor_model and advisor_model.strip():
            data["advisor_model"] = advisor_model.strip()
        if narrator_model and narrator_model.strip():
            data["narrator_model"] = narrator_model.strip()
        _save(data)
    return get_all()
