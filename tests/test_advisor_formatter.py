"""Smoke tests for claude_advisor._format_starter_bundle.

Catches name-drift bugs like the one shipped 2026-05-05: the advisor
function was renamed `_format_data_bundle -> _format_starter_bundle`
but `/api/algorithm/preview` still imported the old name. The endpoint
500'd silently on every "Show context sent to Claude" click — and we
had no test to catch it. These tests make sure both the function name
and the bundle shape stay aligned.
"""
from __future__ import annotations

import claude_advisor


def _bundle_minimal() -> dict:
    """Smallest viable bundle the formatter should accept without
    crashing. Mirrors the keys server._build_advisor_bundle emits when
    nothing is fitted yet."""
    return {
        "window_label": "last 48h",
        "device_label": "Tester",
        "device_sn": "SN-FORMATTER",
        "capacity_wh": 5040,
        "pack_count": 0,
        "main_soc_pct": 80.0,
        "system_soc_pct": 80.0,
        "smart_charge_config": {"mode": "off"},
        "fitted_parasitic_w": None,
        "fitted_inverter_overhead_pct": None,
        "fitted_drain_n_windows": 0,
        "forecast_accuracy_summary": {},
        "recent_samples": [],
        "recent_weather": [],
        "recent_predictions": [],
        "recent_decisions": [],
        "recent_code_changes": [],
    }


def test_format_starter_bundle_is_exported():
    """Regression for the rename bug: `_format_starter_bundle` must
    exist on the module surface (the API endpoint imports it by name)."""
    assert hasattr(claude_advisor, "_format_starter_bundle"), (
        "claude_advisor._format_starter_bundle is the symbol "
        "/api/algorithm/preview imports — don't rename without "
        "updating server.py"
    )


def test_preview_endpoint_symbols_exist():
    """The `/api/algorithm/preview` endpoint touches three symbols on
    claude_advisor: _format_starter_bundle, _get_model, THINKING_BUDGET.
    Lock them in so a future rename breaks this test instead of 500-ing
    the endpoint silently like the 2026-05-05 incident."""
    for name in ("_format_starter_bundle", "_get_model", "THINKING_BUDGET"):
        assert hasattr(claude_advisor, name), (
            f"claude_advisor.{name} is referenced by /api/algorithm/preview "
            "in server.py — keep these symbols stable or update both."
        )
    # _get_model must be callable (it's invoked at request time) and
    # return a non-empty string.
    model = claude_advisor._get_model()
    assert isinstance(model, str) and model


def test_format_starter_bundle_renders_minimal_input():
    out = claude_advisor._format_starter_bundle(_bundle_minimal())
    assert isinstance(out, str)
    assert "Algorithm review" in out
    assert "Tester" in out
    assert "5040 Wh" in out


def test_format_starter_bundle_renders_with_drain_fit():
    bundle = _bundle_minimal() | {
        "fitted_parasitic_w": 95.0,
        "fitted_inverter_overhead_pct": 0.10,
        "fitted_drain_n_windows": 12,
    }
    out = claude_advisor._format_starter_bundle(bundle)
    assert "parasitic_w: 95.0 W" in out
    assert "inverter_overhead_pct: 0.1" in out
    assert "fit windows: 12" in out


def test_openai_tools_use_flattened_responses_api_shape():
    """The Responses API (/v1/responses) takes {type, name, description,
    parameters} directly. The nested Chat Completions shape
    ({"function": {...}}) is what broke on gpt-5.x ("use /v1/responses").
    Guard the conversion so a refactor can't silently regress it."""
    tools = claude_advisor._to_openai_tools()
    assert tools, "no tools converted"
    names = set()
    for t in tools:
        assert t["type"] == "function"
        assert "function" not in t, "nested Chat Completions shape detected"
        assert isinstance(t["name"], str) and t["name"]
        assert isinstance(t["parameters"], dict)
        names.add(t["name"])
    assert "submit_algorithm_review" in names
    assert "query_samples" in names
