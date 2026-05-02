"""anthropic_prefs: per-role Claude model selection."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def prefs(tmp_path, monkeypatch):
    """Reload anthropic_prefs so PREFS_PATH picks up the tmp env var."""
    monkeypatch.setenv("JACKERY_ANTHROPIC_PREFS_FILE",
                       str(tmp_path / "anthropic-prefs.json"))
    import anthropic_prefs
    importlib.reload(anthropic_prefs)
    return anthropic_prefs


def test_defaults_when_unset(prefs):
    assert prefs.get_model("advisor") == prefs.DEFAULTS["advisor_model"]
    assert prefs.get_model("narrator") == prefs.DEFAULTS["narrator_model"]
    snap = prefs.get_all()
    assert snap["advisor_model"] == prefs.DEFAULTS["advisor_model"]
    assert snap["narrator_model"] == prefs.DEFAULTS["narrator_model"]
    assert snap["advisor_1m_context"] is True  # default-on for compat
    assert snap["advisor_thinking_effort"] == "high"


def test_unknown_role_raises(prefs):
    with pytest.raises(ValueError):
        prefs.get_model("orchestrator")


def test_set_and_round_trip(prefs):
    out = prefs.set_models(advisor_model="claude-sonnet-4-7")
    assert out["advisor_model"] == "claude-sonnet-4-7"
    # narrator untouched → falls back to default
    assert out["narrator_model"] == prefs.DEFAULTS["narrator_model"]
    # Persist + re-read → still there
    importlib.reload(prefs)
    assert prefs.get_model("advisor") == "claude-sonnet-4-7"


def test_partial_update_preserves_other_role(prefs):
    prefs.set_models(advisor_model="claude-sonnet-4-7",
                     narrator_model="claude-haiku-4-5")
    prefs.set_models(narrator_model="claude-sonnet-4-7")
    snap = prefs.get_all()
    assert snap["advisor_model"] == "claude-sonnet-4-7"  # untouched
    assert snap["narrator_model"] == "claude-sonnet-4-7"


def test_empty_or_none_values_ignored(prefs):
    prefs.set_models(advisor_model="claude-opus-4-7")
    # Empty string / None should NOT clobber the saved value.
    prefs.set_models(advisor_model="")
    assert prefs.get_model("advisor") == "claude-opus-4-7"
    prefs.set_models(advisor_model=None)
    assert prefs.get_model("advisor") == "claude-opus-4-7"


def test_whitespace_stripped(prefs):
    prefs.set_models(advisor_model="  claude-opus-4-7  ")
    assert prefs.get_model("advisor") == "claude-opus-4-7"


def test_corrupt_file_falls_back_to_defaults(prefs, tmp_path):
    """An unreadable / non-dict prefs file should not crash; it should
    log a warning and return defaults."""
    import os
    path = os.environ["JACKERY_ANTHROPIC_PREFS_FILE"]
    with open(path, "w") as f:
        f.write("not valid json{")
    assert prefs.get_model("advisor") == prefs.DEFAULTS["advisor_model"]


# ---- 1M context + thinking effort fields ----

def test_default_1m_context_preserves_old_always_on_behavior(prefs):
    """Pre-prefs code unconditionally sent the 1M beta header, so the
    default has to be True or we'd silently regress installs that
    upgraded without touching the Settings tab."""
    assert prefs.get_1m_context() is True


def test_default_thinking_effort(prefs):
    assert prefs.get_thinking_effort() == "high"


def test_set_and_read_1m_context(prefs):
    prefs.set_models(advisor_1m_context=False)
    assert prefs.get_1m_context() is False
    prefs.set_models(advisor_1m_context=True)
    assert prefs.get_1m_context() is True


def test_set_thinking_effort_validates(prefs):
    prefs.set_models(advisor_thinking_effort="medium")
    assert prefs.get_thinking_effort() == "medium"
    # Garbage values should be silently dropped (no half-saves) so the
    # advisor never sees something it'd reject at API call time.
    prefs.set_models(advisor_thinking_effort="extreme")
    assert prefs.get_thinking_effort() == "medium"  # unchanged


def test_thinking_effort_normalizes_case_and_whitespace(prefs):
    prefs.set_models(advisor_thinking_effort="  HIGH  ")
    assert prefs.get_thinking_effort() == "high"


def test_get_all_returns_flat_keys_matching_api_contract(prefs):
    """The UI/API contract expects flat keys (advisor_model, ...);
    nesting them under {advisor: {...}, narrator: {...}} would silently
    break the current frontend."""
    snap = prefs.get_all()
    assert "advisor_model" in snap
    assert "advisor_1m_context" in snap
    assert "advisor_thinking_effort" in snap
    assert "narrator_model" in snap


def test_partial_update_preserves_other_advisor_fields(prefs):
    """Updating just the model shouldn't reset 1m_context or effort."""
    prefs.set_models(advisor_model="claude-opus-4-7",
                     advisor_1m_context=True,
                     advisor_thinking_effort="medium")
    prefs.set_models(advisor_model="claude-sonnet-4-7")
    assert prefs.get_1m_context() is True
    assert prefs.get_thinking_effort() == "medium"
