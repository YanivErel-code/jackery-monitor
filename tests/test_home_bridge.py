"""Explicit Home discovery remains separate from portable auth and control."""
from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(isolated_data, monkeypatch):
    monkeypatch.delenv("JACKERY_EMAIL", raising=False)
    monkeypatch.delenv("JACKERY_PASSWORD", raising=False)
    monkeypatch.delenv("JACKERY_API_FAMILY", raising=False)
    import bridge
    importlib.reload(bridge)
    bridge.state = bridge.State()
    return bridge


class HomeFake:
    read_only = True
    supports_realtime = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.token = None
        self.user_id = None
        self.devices = [SimpleNamespace(device_id="system-1", device_sn="home-1",
                                        name="Home", model_code=0,
                                        model_name="HomePower 2000 Ultra")]
        self.pack_cache_by_sn = {}
        self.pack_cache_ts_by_sn = {}
        self.pack_cache_source_by_sn = {}
        self.pack_cache_revision_by_sn = {}
        self.calls = []
        self.receipt = asyncio.Event()

    async def login(self):
        self.calls.append("login")
        self.token = "test-token"

    async def fetch_devices(self):
        self.calls.append("discover")
        return self.devices

    async def fetch_properties(self, device_id):
        self.calls.append("monitor")
        self.receipt.set()
        return {"rb": 40, "_soc_scope": "system", "_installed_capacity_wh": 4096,
                "_home_solar_power_w": 250, "_home_grid_power_w": -10}

    async def subscribe_realtime(self, *args, **kwargs):
        self.calls.append("mqtt")

    async def fetch_battery_packs(self, sn, *, force_refresh=False):
        self.calls.append("packs")
        self.pack_cache_by_sn[sn] = [{"deviceSn": "pack-1", "soc": 38}]
        self.pack_cache_ts_by_sn[sn] = 950.0
        self.pack_cache_source_by_sn[sn] = "http"
        return self.pack_cache_by_sn[sn]

    async def fetch_pack_upgrade_flags(self, sn):
        raise AssertionError("Home must not use portable upgrade endpoints")

    async def publish_command(self, *args):
        raise AssertionError("Home must not send control commands")

    async def probe_endpoints(self, *args, **kwargs):
        raise AssertionError("Home must not probe portable endpoints")

    async def aclose(self):
        self.calls.append("close")


def _select_home(bridge, client=None):
    bridge.state.cloud_creds = {"email": "home@example.invalid", "password": "pw",
                                "region": "EU", "api_family": "home"}
    bridge.state.cloud_client = client


def test_factory_uses_explicit_family_only(bridge, monkeypatch):
    import cloud_client

    portable = object()
    monkeypatch.setattr(cloud_client, "JackeryCloudClient", lambda **kwargs: portable)
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=HomeFake))
    assert bridge._make_cloud_client("p@example.invalid", "pw") is portable
    home = bridge._make_cloud_client("h@example.invalid", "pw", "EU", "home")
    assert isinstance(home, HomeFake)
    assert home.kwargs == {"email": "h@example.invalid", "password": "pw", "region": "EU"}
    with pytest.raises(ValueError, match="EU"):
        bridge._make_cloud_client("h@example.invalid", "pw", "US", "home")
    with pytest.raises(ValueError, match="api_family"):
        bridge._make_cloud_client("h@example.invalid", "pw", "EU", "unknown")


@pytest.mark.parametrize("method", ["set_output", "cloud_probe"])
async def test_home_blocks_control_and_portable_probe(bridge, method):
    client = HomeFake()
    _select_home(bridge, client)
    bridge.state.cloud_device_id = "system-1"
    bridge.state.cloud_device = {"device_sn": "home-1"}
    result = await bridge.handle(method, {"port": "ac", "on": True})
    assert result["ok"] is False
    assert "read-only" in result["error"]
    assert not client.calls


async def test_home_auth_status_and_poll_expose_capabilities(bridge):
    _select_home(bridge)
    auth = await bridge.handle("auth_status", {})
    assert auth["api_family"] == "home"
    assert auth["read_only"] is True
    cloud = bridge.merged_poll()["cloud"]
    assert cloud["api_family"] == "home"
    assert cloud["read_only"] is True


async def test_portable_capabilities_default_without_migration(bridge):
    bridge.state.cloud_creds = {"email": "old@example.invalid", "password": "pw"}
    auth = await bridge.handle("auth_status", {})
    assert auth["api_family"] == "portable"
    assert auth["read_only"] is False


async def test_home_login_discovers_before_persisting(bridge, monkeypatch):
    import cloud_client

    client = HomeFake()
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=lambda **kwargs: client))
    monkeypatch.setattr(cloud_client, "JackeryCloudClient", lambda **kwargs: client)
    stored = []
    def save(*args):
        assert client.calls == ["login", "discover", "close"]
        stored.append(args)
        return True, "test store"
    monkeypatch.setattr(bridge, "save_cloud_credentials", save)
    async def idle():
        await asyncio.Future()
    monkeypatch.setattr(bridge, "cloud_loop", idle)
    result = await bridge.handle("set_credentials", {
        "email": "home@example.invalid", "password": "pw", "region": "EU",
        "api_family": "home",
    })
    try:
        assert result["ok"]
        assert stored == [("home@example.invalid", "pw", "EU", "home")]
        assert bridge.state.cloud_creds["api_family"] == "home"
        assert result["read_only"] is True
    finally:
        await bridge._stop_cloud_poller()


async def test_empty_home_discovery_does_not_replace_credentials(bridge, monkeypatch):
    client = HomeFake()
    client.devices = []
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=lambda **kwargs: client))
    old = {"email": "old@example.invalid", "password": "pw", "region": "US"}
    bridge.state.cloud_creds = old
    monkeypatch.setattr(bridge, "save_cloud_credentials",
                        lambda *args: pytest.fail("empty Home discovery must not be persisted"))
    result = await bridge.handle("set_credentials", {
        "email": "home@example.invalid", "password": "pw", "region": "EU",
        "api_family": "home",
    })
    assert result["ok"] is False
    assert "no systems" in result["error"]
    assert client.calls == ["login", "discover", "close"]
    assert bridge.state.cloud_creds is old


async def test_home_poller_uses_http_without_mqtt_and_preserves_unknowns(bridge, monkeypatch):
    import cloud_client

    client = HomeFake()
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=lambda **kwargs: client))
    monkeypatch.setattr(cloud_client, "JackeryCloudClient", lambda **kwargs: client)
    _select_home(bridge)
    task = asyncio.create_task(bridge.cloud_loop())
    try:
        await asyncio.wait_for(client.receipt.wait(), 1)
        assert "mqtt" not in client.calls
        tele = bridge.state.cloud_telemetry
        assert tele["battery_percent"] == 40
        assert tele["soc_scope"] == "system"
        assert tele["capacity_wh"] == 4096
        assert tele["input_power_w"] is None
        assert tele["output_power_w"] is None
        assert tele["battery_temp_c"] is None
        assert tele["ac_on"] is None
        assert tele["error_code"] is None
        assert tele["home_solar_power_w"] == 250
        assert tele["home_grid_power_w"] == -10
        assert bridge.state.cloud_device["api_family"] == "home"
        assert bridge.state.cloud_devices[0]["read_only"] is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_home_missing_soc_remains_unknown_without_fresh_soc_receipt(bridge):
    _select_home(bridge)
    bridge._merge_cloud_properties("home-1", {"_home_solar_power_w": 200}, source="http")
    tele = bridge.state.telemetry_by_sn["home-1"]
    assert tele["battery_percent"] is None
    assert tele["soc_source_ts"] is None
    assert tele["capacity_wh"] is None


async def test_home_cached_pack_receipt_is_not_restamped(bridge, monkeypatch):
    client = HomeFake()
    _select_home(bridge, client)
    monkeypatch.setattr(bridge.time, "time", lambda: 1000.0)
    first = await bridge.handle("get_battery_packs", {"device_sn": "home-1"})
    second = await bridge.handle("get_battery_packs", {"device_sn": "home-1", "force_refresh": True})
    assert first["ok"] and second["ok"]
    assert first["fetched_at"] == second["fetched_at"] == 950.0
    assert first["source"] == second["source"] == "http"


def test_home_missing_soc_replaces_old_snapshot_without_zero(bridge):
    _select_home(bridge)
    bridge._merge_cloud_properties("home-1", {"rb": 40, "_installed_capacity_wh": 4096},
                                   source="http")
    bridge._merge_cloud_properties("home-1", {"_home_solar_power_w": 250}, source="http")
    tele = bridge.state.telemetry_by_sn["home-1"]
    assert tele["battery_percent"] is None
    assert tele["capacity_wh"] is None
    assert tele["soc_source_ts"] is None


def test_older_home_response_cannot_erase_newer_soc_receipt(bridge, monkeypatch):
    _select_home(bridge)
    monkeypatch.setattr(bridge.time, "time", lambda: 1000.0)
    bridge._merge_cloud_properties("home-1", {"rb": 40}, source="http")
    bridge._merge_cloud_properties("home-1", {"rb": 20}, source="http",
                                   request_started_at=900.0, request_soc_revision=0)
    tele = bridge.state.telemetry_by_sn["home-1"]
    assert tele["battery_percent"] == 40
    assert tele["soc_source_ts"] == 1000.0


async def test_cancelled_home_credential_probe_closes_without_persist(bridge, monkeypatch):
    client = HomeFake()
    started = asyncio.Event()
    async def discover():
        started.set()
        await asyncio.Future()
    client.fetch_devices = discover
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=lambda **kwargs: client))
    monkeypatch.setattr(bridge, "save_cloud_credentials",
                        lambda *args: pytest.fail("cancelled credentials must not be persisted"))
    task = asyncio.create_task(bridge.handle("set_credentials", {
        "email": "home@example.invalid", "password": "pw", "region": "EU",
        "api_family": "home",
    }))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.calls == ["login", "close"]
    assert bridge.state.cloud_creds is None


@pytest.mark.parametrize("family,region", [("home", "US"), ("home", "CN"),
                                           ("unknown", "EU"), ({"home": True}, "EU")])
async def test_invalid_home_selection_cannot_login_or_save(bridge, monkeypatch, family, region):
    monkeypatch.setitem(sys.modules, "home_cloud_client", SimpleNamespace(
        JackeryHomeCloudClient=lambda **kwargs: pytest.fail("invalid backend must not create client")))
    monkeypatch.setattr(bridge, "save_cloud_credentials",
                        lambda *args: pytest.fail("invalid backend must not save"))
    result = await bridge.handle("set_credentials", {
        "email": "home@example.invalid", "password": "pw", "region": region,
        "api_family": family,
    })
    assert result["ok"] is False
    assert bridge.state.cloud_creds is None


async def test_home_does_not_control_kasa_through_fast_trip(bridge, monkeypatch):
    _select_home(bridge)
    monkeypatch.setattr(bridge, "_inverter_protect_cfg", lambda sn:
                        {"mode": "auto", "kasa_device_host": "fixture-host"})
    monkeypatch.setattr(bridge.solar_charge, "stamp_overload",
                        lambda *args, **kwargs: pytest.fail("Home must not trigger control path"))
    await bridge._inverter_protect_check("home-1", 5000)


async def test_old_home_auth_failure_preserves_new_token_and_avoids_cooldown(bridge, monkeypatch):
    from cloud_client import SessionContestedError

    client = HomeFake()
    client.token = "old-token"
    reported = asyncio.Event()
    original = client.fetch_properties
    async def fetch_properties(device_id):
        if not reported.is_set():
            # Another reader completed login while this old-token request
            # was waiting for its rejection response.
            client.token = "new-token"
            reported.set()
            error = SessionContestedError("obsolete request authentication failed")
            error.stale_auth_response = True
            raise error
        return await original(device_id)
    client.fetch_properties = fetch_properties
    monkeypatch.setitem(sys.modules, "home_cloud_client",
                        SimpleNamespace(JackeryHomeCloudClient=lambda **kwargs: client))
    _select_home(bridge)
    task = asyncio.create_task(bridge.cloud_loop())
    try:
        await asyncio.wait_for(reported.wait(), 1)
        assert client.token == "new-token"
        assert bridge.state.contested_until == 0.0
        assert bridge.state.contested_consecutive == 0
        await asyncio.wait_for(client.receipt.wait(), 1)
        assert bridge.state.cloud_state == "connected"
        assert "login" not in client.calls
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
