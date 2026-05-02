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
    assert snap == {
        "advisor": prefs.DEFAULTS["advisor_model"],
        "narrator": prefs.DEFAULTS["narrator_model"],
    }


def test_unknown_role_raises(prefs):
    with pytest.raises(ValueError):
        prefs.get_model("orchestrator")


def test_set_and_round_trip(prefs):
    out = prefs.set_models(advisor_model="claude-sonnet-4-7")
    assert out["advisor"] == "claude-sonnet-4-7"
    # narrator untouched → falls back to default
    assert out["narrator"] == prefs.DEFAULTS["narrator_model"]
    # Persist + re-read → still there
    importlib.reload(prefs)
    assert prefs.get_model("advisor") == "claude-sonnet-4-7"


def test_partial_update_preserves_other_role(prefs):
    prefs.set_models(advisor_model="claude-sonnet-4-7",
                     narrator_model="claude-haiku-4-5")
    prefs.set_models(narrator_model="claude-sonnet-4-7")
    snap = prefs.get_all()
    assert snap["advisor"] == "claude-sonnet-4-7"  # untouched
    assert snap["narrator"] == "claude-sonnet-4-7"


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
