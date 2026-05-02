"""KasaRegistry probe-result tracking + offline detection."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    """Reload kasa_devices so the module-level DEVICES_PATH picks up
    our tmp env var. Same pattern as test_smart_charge.py."""
    monkeypatch.setenv("JACKERY_KASA_DEVICES_FILE",
                       str(tmp_path / "kasa.json"))
    import kasa_devices
    importlib.reload(kasa_devices)
    return kasa_devices.KasaRegistry()


def test_status_unknown_for_never_probed_device(reg):
    d = reg.upsert("192.168.1.50", alias="Test")
    assert reg.status_of(d) == "unknown"
    assert not reg.is_online(d)
    assert reg.offline_count() == 0  # unknown != offline


def test_update_probe_success_marks_online(reg):
    reg.upsert("192.168.1.50")
    d = reg.update_probe("192.168.1.50", success=True, is_on=True,
                         model="EP10")
    assert d is not None
    assert reg.status_of(d) == "online"
    assert reg.is_online(d)
    assert d["consecutive_failures"] == 0
    assert d["last_seen_ts"] is not None
    assert d["last_known_is_on"] is True
    assert d["model"] == "EP10"
    assert d["last_error"] is None


def test_update_probe_failure_marks_offline_and_increments(reg):
    reg.upsert("192.168.1.50")
    reg.update_probe("192.168.1.50", success=False, error="conn refused")
    d = reg.update_probe("192.168.1.50", success=False,
                         error="timeout after 15s")
    assert reg.status_of(d) == "offline"
    assert d["consecutive_failures"] == 2
    assert "timeout" in d["last_error"]
    assert reg.offline_count() == 1


def test_success_after_failure_resets_counter(reg):
    reg.upsert("192.168.1.50")
    reg.update_probe("192.168.1.50", success=False, error="boom")
    reg.update_probe("192.168.1.50", success=False, error="boom2")
    d = reg.update_probe("192.168.1.50", success=True, is_on=False)
    assert d["consecutive_failures"] == 0
    assert d["last_error"] is None
    assert reg.status_of(d) == "online"
    assert reg.offline_count() == 0


def test_update_probe_returns_none_for_unknown_host(reg):
    assert reg.update_probe("10.0.0.1", success=True) is None


def test_alias_is_not_overwritten_by_probe(reg):
    """User-set alias should survive a probe even if the device reports
    a different name."""
    reg.upsert("192.168.1.50", alias="Garage")
    d = reg.update_probe("192.168.1.50", success=True,
                         alias="HS103(US)")
    assert d["alias"] == "Garage"


def test_error_is_truncated(reg):
    reg.upsert("192.168.1.50")
    long_err = "x" * 1000
    d = reg.update_probe("192.168.1.50", success=False, error=long_err)
    assert len(d["last_error"]) <= 240


def test_offline_count_across_multiple_devices(reg):
    reg.upsert("192.168.1.50")
    reg.upsert("192.168.1.51")
    reg.upsert("192.168.1.52")
    reg.update_probe("192.168.1.50", success=True)
    reg.update_probe("192.168.1.51", success=False, error="x")
    # 192.168.1.52 never probed → unknown, NOT offline.
    assert reg.offline_count() == 1


def test_probe_state_persists_across_reload(reg):
    reg.upsert("192.168.1.50")
    reg.update_probe("192.168.1.50", success=False, error="boom")
    # Re-instantiate (using the same module that the fixture reloaded)
    # and confirm the offline state survives a fresh load from disk.
    import kasa_devices
    reg2 = kasa_devices.KasaRegistry()
    d = reg2.get("192.168.1.50")
    assert d["consecutive_failures"] == 1
    assert d["last_error"] == "boom"
    assert reg2.status_of(d) == "offline"
