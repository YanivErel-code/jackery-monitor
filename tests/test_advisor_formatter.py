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
