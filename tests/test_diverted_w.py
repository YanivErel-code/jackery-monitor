"""Tests for `_solar_charge_current_diverted_w` — the function that
decides what value goes into solar_charge_diverted_w each telemetry tick.

The bug this exercises: before reading the plug's actual emeter, the
function inferred diversion from `output_w − verify_pre_output_w`. Any
house-load drift (or inverter idle climbing after baseline capture)
got mis-attributed as "diverted", inflating today_diverted_kwh by
multiple kWh on overnight sessions where the car wasn't plugged in.
"""
from __future__ import annotations

import importlib
import os
import time

import pytest

os.environ["BACKEND"] = "mock"


@pytest.fixture()
def server_mod(isolated_data, monkeypatch, tmp_path):
    monkeypatch.setenv("JACKERY_MOCK", "1")
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    monkeypatch.setenv("JACKERY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JACKERY_BACKUP_CREDS_FILE",
                       str(tmp_path / "backup-creds.json"))
    # solar_charge persists per-device config to /data — redirect so tests
    # don't try to write the real path (read-only on dev machines).
    monkeypatch.setenv("JACKERY_SOLAR_CHARGE_FILE",
                       str(tmp_path / "solar_charge.json"))
    monkeypatch.setenv("JACKERY_SOLAR_CHARGE_OVERLOAD_FILE",
                       str(tmp_path / "solar_charge_overload.json"))
    import crypto_util
    importlib.reload(crypto_util)
    for name in (
        "auth", "settings", "automation", "location", "smart_charge",
        "cost", "anthropic_creds", "anthropic_prefs",
        "kasa_creds", "kasa_devices", "backup_creds", "energy_db",
        "solar_charge",
    ):
        mod = importlib.import_module(name)
        importlib.reload(mod)
    import server as server_mod
    importlib.reload(server_mod)
    # Reset solar_charge runtime so previous test state doesn't bleed.
    import solar_charge
    solar_charge.reset_runtime()
    return server_mod


def _arm_active_plug(server_mod, sn="SN1", *, on=True, baseline=80.0):
    """Configure an active solar_charge plug for `sn` and prime runtime
    so the function under test takes the 'plug is on' path."""
    import solar_charge
    cfg = dict(solar_charge.DEFAULT_CONFIG)
    cfg["mode"] = "active"
    cfg["kasa_device_host"] = "192.168.0.55"
    cfg["car_load_w"] = 1400
    solar_charge.set_config(cfg, device_sn=sn)
    solar_charge._update_runtime(
        sn,
        plug_is_on=on,
        verify_pre_output_w=baseline,
        plug_power_w=None,
        plug_power_ts=0.0,
    )


def test_returns_zero_when_mode_off(server_mod):
    """mode=off short-circuits regardless of plug state — never log
    diversion for a controller the user has disabled."""
    import solar_charge
    solar_charge.set_config({**solar_charge.DEFAULT_CONFIG, "mode": "off"},
                            device_sn="SN1")
    assert server_mod._solar_charge_current_diverted_w("SN1", 1500) == 0.0


def test_returns_zero_when_plug_off(server_mod):
    """plug_is_on=False short-circuits — the controller can't be
    diverting through a plug that's electrically OFF."""
    _arm_active_plug(server_mod, on=False)
    assert server_mod._solar_charge_current_diverted_w("SN1", 1500) == 0.0


def test_uses_kasa_truth_when_fresh(server_mod):
    """The whole point of the fix: when the plug reports its own
    power and the reading is fresh, return that value verbatim. House
    load drift on the Jackery output doesn't matter."""
    import solar_charge
    _arm_active_plug(server_mod, baseline=80.0)
    solar_charge._update_runtime(
        "SN1", plug_power_w=1310.0, plug_power_ts=time.time())
    # output_w is irrelevant on the Kasa-truth path:
    assert server_mod._solar_charge_current_diverted_w("SN1", 9999) == 1310.0


def test_kasa_truth_zero_when_no_car_connected(server_mod):
    """The bug repro: plug ON since midnight, nothing plugged in, the
    plug reports near zero. We MUST record diverted=0, not the
    output_w − baseline guess."""
    import solar_charge
    _arm_active_plug(server_mod, baseline=80.0)
    solar_charge._update_runtime(
        "SN1", plug_power_w=0.4, plug_power_ts=time.time())
    # Even if Jackery output drifted to 280W (lights, etc.), diverted = 0.
    assert server_mod._solar_charge_current_diverted_w("SN1", 280) == 0.0


def test_kasa_noise_floor_clamps_low_readings(server_mod):
    """Kasa plugs draw ~1-3W for their own relay even with nothing
    connected. Anything below the noise floor counts as zero."""
    import solar_charge
    _arm_active_plug(server_mod, baseline=80.0)
    solar_charge._update_runtime(
        "SN1", plug_power_w=2.7, plug_power_ts=time.time())
    assert server_mod._solar_charge_current_diverted_w("SN1", 80) == 0.0


def test_falls_back_to_delta_when_cache_stale(server_mod):
    """Cache older than the freshness window can't be trusted (Kasa
    has been unreachable for too long). Drop to the legacy delta
    estimator — same as plugs without emeter."""
    import solar_charge
    _arm_active_plug(server_mod, baseline=80.0)
    # 5 minutes old — well beyond the 90s freshness window.
    solar_charge._update_runtime(
        "SN1", plug_power_w=1310.0, plug_power_ts=time.time() - 300)
    # output_w=1380, baseline=80 → delta=1300
    assert server_mod._solar_charge_current_diverted_w("SN1", 1380) == 1300.0


def test_falls_back_to_delta_when_emeter_unsupported(server_mod):
    """Older HS103-class plugs never populate plug_power_w. We must
    keep the legacy estimator working for them."""
    _arm_active_plug(server_mod, baseline=80.0)
    # Don't set plug_power_w — leave it None as in the default state.
    assert server_mod._solar_charge_current_diverted_w("SN1", 1380) == 1300.0


def test_delta_clamps_negative_to_zero(server_mod):
    """House load DROPPED after baseline capture — the delta is
    negative. The function returns 0, not a negative diversion."""
    _arm_active_plug(server_mod, baseline=200.0)
    # output_w fell below baseline; no fresh Kasa reading
    assert server_mod._solar_charge_current_diverted_w("SN1", 150) == 0.0
