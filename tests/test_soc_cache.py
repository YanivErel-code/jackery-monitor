"""SOC timestamps must describe receipt, and stale pack caches must expire."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

SN = "TEST-MAIN"


@pytest.fixture
def bridge_state(isolated_data, monkeypatch):
    import bridge

    importlib.reload(bridge)
    bridge.state = bridge.State()
    clock = [1000.0]
    monkeypatch.setattr(bridge.time, "time", lambda: clock[0])

    async def no_upgrade_lookup(sn, packs):
        return packs

    monkeypatch.setattr(bridge, "_annotate_pack_upgrades", no_upgrade_lookup)
    return bridge, clock


class FakePackClient:
    def __init__(self, packs=None, error=None, during_fetch=None):
        self.packs = packs or [{"deviceSn": "TEST-PACK", "rb": 30}]
        self.error = error
        self.during_fetch = during_fetch
        self.calls = 0
        self.pack_cache_by_sn = {}
        self.pack_cache_ts_by_sn = {}
        self.pack_cache_source_by_sn = {}
        self.pack_cache_revision_by_sn = {}

    async def fetch_battery_packs(self, sn, *, force_refresh=False):
        self.calls += 1
        assert force_refresh, "bridge refresh must bypass the client's MQTT cache"
        if self.during_fetch:
            await self.during_fetch()
        if self.error:
            raise RuntimeError(self.error)
        return self.packs


def seed_old_packs(bridge, ts=800.0):
    bridge.state.battery_packs_by_sn[SN] = [{"deviceSn": "TEST-PACK", "rb": 10}]
    bridge.state.packs_ts_by_sn[SN] = ts


async def test_expired_bridge_pack_cache_fetches_http(bridge_state):
    bridge, clock = bridge_state
    seed_old_packs(bridge)
    client = FakePackClient()
    bridge.state.cloud_client = client
    result = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert client.calls == 1
    assert result["packs"][0]["rb"] == 30
    assert result["fetched_at"] == clock[0]
    assert result["source"] == "http"
    assert result["stale"] is False


async def test_fresh_http_pack_cache_keeps_its_receipt_source(bridge_state):
    bridge, clock = bridge_state
    bridge.state.cloud_client = FakePackClient()
    first = await bridge.handle("get_battery_packs", {"device_sn": SN})
    clock[0] += 10
    second = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert second["source"] == "http"
    assert second["fetched_at"] == first["fetched_at"]
    assert second["stale"] is False
    assert bridge.state.cloud_client.calls == 1


async def test_failed_pack_refresh_preserves_receipt_and_throttles_retry(bridge_state):
    bridge, clock = bridge_state
    seed_old_packs(bridge)
    client = FakePackClient(error="unavailable")
    bridge.state.cloud_client = client
    first = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert client.calls == 1
    assert first["packs"][0]["rb"] == 10
    assert first["fetched_at"] == 800
    assert first["stale"] is True
    assert "RuntimeError" in first["error"]
    assert "unavailable" not in first["error"]
    clock[0] += 1
    second = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert client.calls == 1
    assert second["fetched_at"] == 800
    assert second["stale"] is True
    clock[0] += 30
    await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert client.calls == 2


async def test_pack_http_reply_cannot_replace_newer_mqtt(bridge_state):
    bridge, clock = bridge_state
    seed_old_packs(bridge)

    async def mqtt_arrives():
        clock[0] += 1
        bridge._cache_pack_snapshot(
            SN, [{"deviceSn": "TEST-PACK", "rb": 44}],
            source="mqtt", received_at=clock[0],
        )

    bridge.state.cloud_client = FakePackClient(during_fetch=mqtt_arrives)
    result = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert result["packs"][0]["rb"] == 44
    assert result["fetched_at"] == 1001
    assert result["source"] == "mqtt"
    assert result["stale"] is False


async def test_concurrent_expired_pack_calls_share_one_refresh(bridge_state):
    bridge, clock = bridge_state
    seed_old_packs(bridge)

    async def yield_to_second_caller():
        await asyncio.sleep(0)

    client = FakePackClient(during_fetch=yield_to_second_caller)
    bridge.state.cloud_client = client
    results = await asyncio.gather(*[
        bridge.handle("get_battery_packs", {"device_sn": SN}) for _ in range(2)
    ])
    assert client.calls == 1
    assert all(r["packs"][0]["rb"] == 30 for r in results)


async def test_cloud_client_does_not_keep_expired_mqtt_packs(monkeypatch):
    import cloud_client

    client = cloud_client.JackeryCloudClient("test@example.invalid", "test")
    client.pack_cache_by_sn[SN] = [{"deviceSn": "TEST-PACK", "rb": 10}]
    client.pack_cache_ts_by_sn = {SN: 800.0}
    monkeypatch.setattr(cloud_client.time, "time", lambda: 1000.0)
    calls = []

    async def fake_get(path, params):
        calls.append((path, params))
        return {"code": 0, "data": [{"deviceSn": "TEST-PACK", "rb": 30}]}

    monkeypatch.setattr(client, "_authed_get", fake_get)
    packs = await client.fetch_battery_packs(SN)
    assert len(calls) == 1
    assert packs[0]["rb"] == 30


def test_power_push_does_not_refresh_soc_provenance(bridge_state):
    bridge, clock = bridge_state
    bridge._merge_cloud_properties(SN, {"rb": 25, "op": 525}, source="http")
    first = bridge.state.telemetry_by_sn[SN]
    assert first["soc_source_ts"] == 1000
    assert first["soc_source"] == "http"
    clock[0] += 300
    bridge._merge_cloud_properties(SN, {"op": 526}, source="mqtt")
    second = bridge.state.telemetry_by_sn[SN]
    assert second["battery_percent"] == 25
    assert second["soc_source_ts"] == 1000
    assert second["soc_source"] == "http"
    assert bridge.state.ts_by_sn[SN] == 1300
    mirror = bridge.merged_poll()["cloud"]["devices_telemetry"][SN]
    assert mirror["soc_source_ts"] == 1000
    assert mirror["soc_source"] == "http"


def test_mqtt_rb_push_updates_soc_provenance(bridge_state):
    bridge, clock = bridge_state
    bridge._merge_cloud_properties(SN, {"rb": 25}, source="http")
    clock[0] += 1
    bridge._merge_cloud_properties(SN, {"rb": 23, "op": 525}, source="mqtt")
    telemetry = bridge.state.telemetry_by_sn[SN]
    assert telemetry["battery_percent"] == 23
    assert telemetry["soc_source_ts"] == 1001
    assert telemetry["soc_source"] == "mqtt"


def test_delayed_http_rb_does_not_replace_newer_mqtt_rb(bridge_state):
    bridge, clock = bridge_state
    bridge._merge_cloud_properties(SN, {"rb": 25}, source="http")
    request_started_at = clock[0] + 1
    clock[0] += 2
    bridge._merge_cloud_properties(SN, {"rb": 23}, source="mqtt")
    clock[0] += 1
    bridge._merge_cloud_properties(
        SN, {"rb": 24, "oac": 1}, source="http", request_started_at=request_started_at,
    )
    telemetry = bridge.state.telemetry_by_sn[SN]
    assert telemetry["battery_percent"] == 23
    assert telemetry["soc_source_ts"] == 1002
    assert telemetry["soc_source"] == "mqtt"
    assert telemetry["ac_on"] is True


async def test_signout_clears_per_device_soc_and_pack_caches(bridge_state, monkeypatch):
    bridge, clock = bridge_state
    bridge.state.props_raw_by_sn[SN] = {"rb": 25}
    bridge.state.telemetry_by_sn[SN] = {"battery_percent": 25}
    bridge.state.ts_by_sn[SN] = 1000
    seed_old_packs(bridge)
    monkeypatch.setattr(bridge, "clear_cloud_credentials", lambda: (True, "test"))
    await bridge.handle("clear_credentials", {})
    assert not bridge.state.props_raw_by_sn
    assert not bridge.state.telemetry_by_sn
    assert not bridge.state.ts_by_sn
    assert not bridge.state.battery_packs_by_sn
    assert not bridge.state.packs_ts_by_sn


@pytest.mark.parametrize("rb", [None, "invalid", -1, 101, True, float("nan")])
def test_invalid_rb_does_not_replace_last_valid_soc(bridge_state, rb):
    bridge, clock = bridge_state
    bridge._merge_cloud_properties(SN, {"rb": 25}, source="http")
    clock[0] += 300
    bridge._merge_cloud_properties(SN, {"rb": rb, "op": 526}, source="mqtt")
    telemetry = bridge.state.telemetry_by_sn[SN]
    assert telemetry["battery_percent"] == 25
    assert telemetry["soc_source_ts"] == 1000
    assert telemetry["soc_source"] == "http"


async def test_forced_pack_refresh_bypasses_fresh_cache(bridge_state):
    bridge, clock = bridge_state
    client = FakePackClient()
    bridge.state.cloud_client = client
    await bridge.handle("get_battery_packs", {"device_sn": SN})
    clock[0] += 1
    await bridge.handle("get_battery_packs", {"device_sn": SN, "force_refresh": True})
    assert client.calls == 2


def test_same_clock_mqtt_soc_wins_over_inflight_http(bridge_state):
    bridge, clock = bridge_state
    bridge._merge_cloud_properties(SN, {"rb": 25}, source="http")
    revision = bridge.state.soc_revision_by_sn[SN]
    bridge._merge_cloud_properties(SN, {"rb": 23}, source="mqtt")
    bridge._merge_cloud_properties(
        SN, {"rb": 24}, source="http", request_started_at=clock[0],
        request_soc_revision=revision,
    )
    assert bridge.state.telemetry_by_sn[SN]["battery_percent"] == 23


async def test_cache_reset_during_http_cannot_restore_old_packs(bridge_state):
    bridge, clock = bridge_state
    seed_old_packs(bridge)

    async def reset_cache():
        bridge._clear_device_caches()

    bridge.state.cloud_client = FakePackClient(during_fetch=reset_cache)
    result = await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert result["stale"] is True
    assert result["packs"] == []
    assert not bridge.state.battery_packs_by_sn


@pytest.mark.parametrize("cooldown_field", ["pause_until", "contested_until"])
@pytest.mark.parametrize("force_refresh", [False, True])
async def test_pack_fallback_honors_cloud_pause(bridge_state, cooldown_field, force_refresh):
    bridge, clock = bridge_state
    seed_old_packs(bridge)
    client = FakePackClient()
    bridge.state.cloud_client = client
    setattr(bridge.state, cooldown_field, clock[0] + 600)
    result = await bridge.handle("get_battery_packs", {
        "device_sn": SN, "force_refresh": force_refresh,
    })
    assert client.calls == 0
    assert result["stale"] is True
    assert result["fetched_at"] == 800
    assert result["packs"][0]["rb"] == 10
    clock[0] += 601
    await bridge.handle("get_battery_packs", {"device_sn": SN})
    assert client.calls == 1


@pytest.mark.parametrize("cooldown_field", ["pause_until", "contested_until"])
async def test_upgrade_lookup_honors_cloud_pause(bridge_state, cooldown_field, monkeypatch):
    bridge, clock = bridge_state
    # Restore the real annotation function replaced by the fixture.
    importlib.reload(bridge)
    bridge.state = bridge.State()
    clock[0] = 10000
    setattr(bridge.state, cooldown_field, clock[0] + 600)
    calls = []

    async def fake_upgrade_lookup(sn):
        calls.append(sn)
        return {"TEST-PACK": True}

    client = FakePackClient()
    client.fetch_pack_upgrade_flags = fake_upgrade_lookup
    bridge.state.cloud_client = client
    packs = [{"deviceSn": "TEST-PACK", "rb": 25}]
    await bridge._annotate_pack_upgrades(SN, packs)
    assert not calls
    clock[0] += 601
    await bridge._annotate_pack_upgrades(SN, packs)
    assert calls == [SN]
    assert packs[0]["needUpgrade"] is True


async def test_signout_with_live_poller_clears_caches(bridge_state, monkeypatch):
    bridge, clock = bridge_state
    seed_old_packs(bridge)
    bridge._merge_cloud_properties(SN, {"rb": 25}, source="http")
    monkeypatch.setattr(bridge, "clear_cloud_credentials", lambda: (True, "test"))
    started = asyncio.Event()

    async def poller():
        started.set()
        await asyncio.Future()

    child = asyncio.create_task(poller())
    bridge.state.cloud_task = child
    await started.wait()
    result = await bridge.handle("clear_credentials", {})
    assert result["ok"] is True
    assert child.cancelled()
    assert bridge.state.cloud_task is None
    assert not bridge.state.battery_packs_by_sn
    assert not bridge.state.soc_source_ts_by_sn
    assert not bridge.state.telemetry_by_sn


async def test_new_credentials_replace_live_poller(bridge_state, monkeypatch):
    import cloud_client

    bridge, clock = bridge_state
    started = asyncio.Event()

    async def poller():
        started.set()
        try:
            await asyncio.Future()
        finally:
            # A last queued receipt during shutdown belongs to the old account.
            bridge._merge_cloud_properties(SN, {"rb": 25}, source="mqtt")
            bridge.state.cloud_devices = [{"device_sn": SN, "name": "old account"}]

    class FakeProbe:
        def __init__(self, **kwargs):
            pass

        async def login(self):
            return "test"

        async def aclose(self):
            pass

    monkeypatch.setattr(cloud_client, "JackeryCloudClient", FakeProbe)
    monkeypatch.setattr(bridge, "save_cloud_credentials", lambda *args: (True, "test"))
    monkeypatch.setattr(bridge, "cloud_loop", poller)
    old = asyncio.create_task(poller())
    bridge.state.cloud_task = old
    await started.wait()
    try:
        result = await bridge.handle("set_credentials", {
            "email": "test@example.invalid", "password": "test", "region": "US",
        })
        assert result["ok"] is True
        assert old.cancelled()
        assert bridge.state.cloud_task is not old
        assert not bridge.state.cloud_task.done()
        assert not bridge.state.telemetry_by_sn
        assert not bridge.state.soc_source_ts_by_sn
        assert bridge.state.cloud_devices == []
    finally:
        task = bridge.state.cloud_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def test_credential_cleanup_does_not_swallow_callers_cancellation(bridge_state, monkeypatch):
    bridge, clock = bridge_state
    monkeypatch.setattr(bridge, "clear_cloud_credentials", lambda: (True, "test"))
    started = asyncio.Event()
    child_stopping = asyncio.Event()

    async def poller():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            child_stopping.set()
            await asyncio.Future()  # simulate an in-progress async shutdown

    child = asyncio.create_task(poller())
    bridge.state.cloud_task = child
    await started.wait()
    request = asyncio.create_task(bridge.handle("clear_credentials", {}))
    await child_stopping.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert child.cancelled()


async def capture_mqtt_callbacks(bridge, monkeypatch):
    import cloud_client

    subscribed = asyncio.Event()

    class FakeLoopClient(FakePackClient):
        token = "test"

        async def fetch_devices(self):
            return [SimpleNamespace(device_id="TEST-ID", device_sn=SN, name="Test",
                                    model_code=13, model_name="Test Model")]

        async def fetch_properties(self, device_id):
            return {"rb": 25, "op": 525}

        async def subscribe_realtime(self, property_callback, *, on_pack_change):
            self.property_callback = property_callback
            self.pack_callback = on_pack_change
            subscribed.set()

    client = FakeLoopClient()
    bridge.state.cloud_creds = {"email": "test@example.invalid", "password": "test"}
    monkeypatch.setattr(cloud_client, "JackeryCloudClient", lambda **kwargs: client)
    task = asyncio.create_task(bridge.cloud_loop())
    await asyncio.wait_for(subscribed.wait(), 1)
    return task, client


async def test_retired_mqtt_callbacks_cannot_change_caches(bridge_state, monkeypatch):
    bridge, clock = bridge_state

    async def no_inverter_action(*args):
        pass

    monkeypatch.setattr(bridge, "_inverter_protect_check", no_inverter_action)
    task, client = await capture_mqtt_callbacks(bridge, monkeypatch)
    bridge.state.cloud_client = None
    bridge.state.cloud_state = "needs-credentials"
    before = bridge.state.telemetry_by_sn[SN].copy()
    try:
        await client.property_callback({"rb": 5, "op": 0}, SN)
        await client.pack_callback([{"deviceSn": "TEST-PACK", "rb": 99}], SN)
        assert bridge.state.telemetry_by_sn[SN] == before
        assert not bridge.state.battery_packs_by_sn
        assert bridge.state.cloud_state == "needs-credentials"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_property_callback_cannot_mirror_state_after_retirement(bridge_state, monkeypatch):
    bridge, clock = bridge_state

    async def retire_client_during_inverter_check(*args):
        bridge.state.cloud_client = None
        bridge._clear_device_caches()
        bridge.state.cloud_telemetry = None
        bridge.state.cloud_props_raw = {}
        bridge.state.cloud_ts = None
        bridge.state.cloud_state = "needs-credentials"

    monkeypatch.setattr(bridge, "_inverter_protect_check", retire_client_during_inverter_check)
    task, client = await capture_mqtt_callbacks(bridge, monkeypatch)
    try:
        await client.property_callback({"rb": 23, "op": 525}, SN)
        assert bridge.state.cloud_telemetry is None
        assert not bridge.state.cloud_props_raw
        assert bridge.state.cloud_ts is None
        assert bridge.state.cloud_state == "needs-credentials"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
