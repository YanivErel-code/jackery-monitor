"""Home REST telemetry must not enter portable energy or control paths."""

import importlib
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.fixture
def home_server(isolated_data, monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    for name in ("crypto_util", "settings", "automation", "energy_db"):
        importlib.reload(importlib.import_module(name))
    import server

    importlib.reload(server)
    server.state.device = server.DeviceInfo("Home", "cloud", 0, None, "HOME-1", "home")
    telemetry = {
        "battery_percent": 42,
        "soc_scope": "system",
        "capacity_wh": 4096,
        "input_power_w": None,
        "output_power_w": None,
    }
    server.state.last_status = telemetry
    server.state.last_cloud_meta = {
        "api_family": "home",
        "read_only": True,
        "selected_device_id": "system-1",
        "devices": [
            {
                "device_id": "system-1",
                "device_sn": "HOME-1",
                "name": "Home",
                "api_family": "home",
                "read_only": True,
                "device_type": "home",
            },
            {
                "device_id": "system-2",
                "device_sn": "HOME-2",
                "name": "Other Home",
                "api_family": "home",
                "read_only": True,
                "device_type": "home",
            },
        ],
        "devices_telemetry": {
            "HOME-1": {"telemetry": telemetry},
            "HOME-2": {"telemetry": {**telemetry, "battery_percent": None, "capacity_wh": None}},
        },
    }
    server.state.battery_packs_by_sn["HOME-1"] = [{"sn": "bms2", "rb": 10}]
    return server


def test_system_soc_and_capacity_are_already_aggregated(home_server):
    assert home_server._system_soc_pct(42, "HOME-1") == 42
    assert home_server._total_capacity_wh("HOME-1") == 4096
    assert home_server._total_capacity_wh("HOME-2") is None
    status = home_server.serialize_status()
    assert status["device"]["read_only"] is True
    assert status["telemetry"]["capacity_wh"] == 4096
    assert "main_soc_pct" not in status["telemetry"]
    assert status["energy"] is None


def test_secondary_home_view_preserves_unknowns_and_capabilities(home_server):
    status = home_server.serialize_status("system-2")
    assert status["device"]["device_type"] == "home"
    assert status["device"]["api_family"] == "home"
    assert status["device"]["read_only"] is True
    assert status["telemetry"]["battery_percent"] is None
    assert status["telemetry"]["capacity_wh"] is None


async def test_home_cannot_send_output_commands(home_server, monkeypatch):
    setter = AsyncMock()
    monkeypatch.setattr(home_server.state.client, "set_output", setter)
    with pytest.raises(HTTPException) as exc:
        await home_server.api_set_output({"port": "ac", "on": True, "device_sn": "HOME-2"})
    assert exc.value.status_code == 501
    setter.assert_not_awaited()


async def test_saved_portable_controllers_are_never_evaluated(home_server, monkeypatch):
    monkeypatch.setattr(
        home_server.smart_charge, "get_config", lambda *a: pytest.fail("portable smart config read")
    )
    monkeypatch.setattr(
        home_server.solar_charge, "get_config", lambda *a: pytest.fail("portable solar config read")
    )
    assert await home_server._smart_charge_evaluate(device_sn="HOME-1") is None
    assert await home_server._solar_charge_evaluate(device_sn="HOME-2") is None
    home_server.state.energy.set_device_param(
        "HOME-1", "inverter_watchdog_enabled", 1, source="user"
    )
    assert home_server._inverter_watchdog_enabled("HOME-1", None) is False
    forecast = await home_server._build_and_record_forecast("HOME-1")
    assert forecast["supported"] is False


async def test_home_login_routes_explicit_family_and_rejects_us(home_server, monkeypatch):
    setter = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(home_server.state.client, "set_credentials", setter, raising=False)
    monkeypatch.setattr(home_server, "connect_device", AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await home_server.api_set_credentials(
            {"email": "home@example.com", "password": "test", "api_family": "home", "region": "US"}
        )
    assert exc.value.status_code == 400
    setter.assert_not_awaited()
    await home_server.api_set_credentials(
        {"email": "home@example.com", "password": "test", "api_family": "home", "region": "EU"}
    )
    setter.assert_awaited_once_with("home@example.com", "test", "EU", api_family="home")


@pytest.mark.parametrize("soc", [None, 42])
async def test_home_poll_never_records_fake_energy_or_runs_kasa_rules(
    home_server, monkeypatch, soc, caplog
):
    import asyncio

    s = home_server.state
    s.last_status["battery_percent"] = soc
    cloud = s.last_cloud_meta
    frame = {**s.last_status, "_source": "cloud", "_cloud": cloud}
    monkeypatch.setattr(s.client, "poll", AsyncMock(return_value=frame))
    monkeypatch.setattr(s.client, "_connected", True)
    monkeypatch.setattr(s.energy, "record", lambda *a, **kw: pytest.fail("Home energy integrated"))
    monkeypatch.setattr(
        s.automation, "evaluate", AsyncMock(side_effect=AssertionError("Home rules evaluated"))
    )
    monkeypatch.setattr(home_server, "_refresh_packs_for", AsyncMock())
    monkeypatch.setattr(home_server, "broadcast_status", AsyncMock())
    monkeypatch.setattr(
        home_server.rescue, "decide", lambda *a: pytest.fail("Home rescue evaluated")
    )

    async def stop_after_tick(*a):
        raise asyncio.CancelledError

    monkeypatch.setattr(home_server.asyncio, "sleep", stop_after_tick)
    with pytest.raises(asyncio.CancelledError):
        await home_server.poll_loop()
    assert "Poll loop error" not in caplog.text
    assert s.last_update_ts is not None
    s.automation.evaluate.assert_not_awaited()
    assert list(s.history) == []
    assert s.last_status["battery_percent"] == soc


def test_home_reported_capacity_wins_over_old_portable_overrides(home_server):
    s = home_server.state
    s.energy.upsert_device("HOME-1", "Home", None, None)
    s.energy.set_capacity_override("HOME-1", 30240)
    s.energy.set_device_param("HOME-1", "battery_capacity_wh", 30240, source="user")
    assert home_server.resolve_device_param("HOME-1", "battery_capacity_wh")["value"] == 4096


async def test_home_startup_without_telemetry_cannot_reset_kasa_plugs(home_server, monkeypatch):
    s = home_server.state
    s.device = None
    s.last_status = None
    s.last_cloud_meta = None
    monkeypatch.setattr(s.client, "backend_name", "bridge")
    monkeypatch.setattr(
        s.client,
        "_last_status",
        {"cloud": {"api_family": "home", "read_only": True}},
        raising=False,
    )
    monkeypatch.setattr(
        home_server.solar_charge,
        "get_all_configs",
        lambda: {"HOME-1": {"mode": "active", "kasa_device_host": "test.invalid"}},
    )
    setter = AsyncMock()
    monkeypatch.setattr(home_server.kasa_client, "set_state", setter)
    await home_server._solar_charge_hydrate_runtime()
    setter.assert_not_awaited()


async def test_home_credential_switch_discards_old_portable_snapshot():
    from device_client import BridgeDeviceClient

    client = BridgeDeviceClient("127.0.0.1", 1)
    client._last_status = {"cloud": {"api_family": "portable", "devices": [{"device_sn": "OLD"}]}}
    client._rpc = AsyncMock(return_value={"ok": True})
    await client.set_credentials("home@example.com", "test", "EU", "home")
    assert client.last_status is None
    client._last_status = {"cloud": {"api_family": "home"}}
    await client.clear_credentials()
    assert client.last_status is None


async def test_bridge_capabilities_survive_connect_without_monitor():
    from device_client import BridgeDeviceClient

    client = BridgeDeviceClient("127.0.0.1", 1)
    snapshot = {
        "device": None,
        "telemetry": None,
        "cloud": {"api_family": "home", "state": "logging-in"},
    }
    client._rpc = AsyncMock(return_value=snapshot)
    ok, info, _ = await client.connect()
    assert ok and info is None
    assert client.last_status is snapshot


@pytest.mark.parametrize("controller", ["_smart_charge_evaluate", "_solar_charge_evaluate"])
async def test_account_switch_during_weather_stops_old_controller(
    home_server, monkeypatch, controller
):
    s = home_server.state
    s.device.device_type = "portable"
    s.device.model_code = 13
    s.last_cloud_meta = {
        "api_family": "portable",
        "devices": [{"device_sn": "HOME-1", "api_family": "portable"}],
    }
    monkeypatch.setattr(home_server.smart_charge, "get_config", lambda *a: {"mode": "active"})
    monkeypatch.setattr(home_server.solar_charge, "get_config", lambda *a: {"mode": "active"})
    monkeypatch.setattr(home_server.device_location, "get", lambda: {"latitude": 1, "longitude": 2})

    async def switch_to_home(*a):
        s.last_cloud_meta = {"api_family": "home", "read_only": True}
        return {"hourly": [], "utc_offset_seconds": 0}

    monkeypatch.setattr(home_server.weather_client, "fetch_irradiance", switch_to_home)
    monkeypatch.setattr(
        s.energy,
        "history",
        lambda *a, **kw: pytest.fail("old portable model continued after Home switch"),
    )
    setter = AsyncMock()
    monkeypatch.setattr(home_server.kasa_client, "set_state", setter)
    assert await getattr(home_server, controller)(device_sn="HOME-1") is None
    setter.assert_not_awaited()
