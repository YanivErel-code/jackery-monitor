"""Per-device recovery defaults and intentional OFF during a pending cycle."""

from __future__ import annotations

import asyncio
import importlib

import pytest

SN = "TEST-RECOVERY"


@pytest.fixture
def recovery_server(isolated_data, monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_MOCK", "1")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    monkeypatch.setenv("JACKERY_DATA_DIR", str(tmp_path))
    for name in ("crypto_util", "settings", "automation", "energy_db"):
        importlib.reload(importlib.import_module(name))
    import server

    importlib.reload(server)
    server.inverter_watchdog.reset_state()
    server.state.device = server.DeviceInfo(
        name="Test",
        address="mock",
        rssi=0,
        model_code=13,
        device_sn=SN,
        device_type="portable",
    )
    calls = []

    async def fake_set_output(port, on, *, device_sn=None):
        calls.append((port, on, device_sn))

    monkeypatch.setattr(server.state.client, "set_output", fake_set_output)
    clock = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    return server, calls, clock


@pytest.mark.parametrize("model_code", [None, 0, 8, 19, 99])
async def test_non_5000_models_do_not_auto_enable_ac(recovery_server, model_code):
    server, calls, clock = recovery_server
    for dt in (0, 2, 12, 62, 600):
        clock[0] = 1000 + dt
        await server._inverter_watchdog_tick(
            SN, {"ac_on": False, "output_power_w": 0}, model_code, clock[0]
        )
    assert calls == []


@pytest.mark.parametrize("model_code", [13, 22])
async def test_5000_plus_defaults_still_auto_recover(recovery_server, model_code):
    server, calls, clock = recovery_server
    await server._inverter_watchdog_tick(
        SN, {"ac_on": False, "output_power_w": 0}, model_code, clock[0]
    )
    assert calls == [("ac", True, SN)]


async def test_disabled_recovery_persists_and_respects_manual_off(recovery_server):
    server, calls, clock = recovery_server
    cfg = server.api_set_inverter_watchdog_config({"device_sn": SN, "enabled": False})
    assert cfg["enabled"] is False
    assert cfg["source"] == "user"
    # A fresh DB handle sees the persisted preference too.
    server.state.energy = server.EnergyDB(server.state.energy.path)
    await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
    clock[0] += 600
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert calls == [("ac", False, SN)]


async def test_enabled_recovery_honors_then_expires_manual_off_grace(recovery_server):
    server, calls, clock = recovery_server
    await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
    clock[0] += 59
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert calls == [("ac", False, SN)]
    clock[0] += 2
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert calls[-1] == ("ac", True, SN)


@pytest.mark.parametrize(
    "user_action",
    [
        "none",
        "manual_off",
        "disable_recovery",
        "disable_reenable",
        "reset_state",
        "dismiss",
        "other_device_off",
        "replace_client",
        "pending_manual_off",
    ],
)
async def test_pending_cycle_does_not_override_user_action(
    recovery_server,
    monkeypatch,
    user_action,
):
    server, calls, clock = recovery_server
    server.user_settings.update({"inverter_trip_recovery_min_w": 100})
    paused = asyncio.Event()
    resume = asyncio.Event()

    async def pause_cycle(seconds):
        assert seconds == 2
        paused.set()
        await resume.wait()

    monkeypatch.setattr(server.asyncio, "sleep", pause_cycle)
    # Real state-machine signature: high output, then three fresh zero
    # readings over six seconds while the hardware still reports AC on.
    for offset, output in ((0, 900), (2, 0), (4, 0), (8, 0)):
        clock[0] = 1000 + offset
        await server._inverter_watchdog_tick(
            SN, {"ac_on": True, "output_power_w": output}, 13, clock[0]
        )
    await asyncio.wait_for(paused.wait(), 1)
    assert calls == [("ac", False, SN)]
    pending_off = None
    finish_off = asyncio.Event()
    try:
        if user_action == "manual_off":
            await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
        elif user_action in ("disable_recovery", "disable_reenable"):
            server.api_set_inverter_watchdog_config({"device_sn": SN, "enabled": False})
            if user_action == "disable_reenable":
                server.api_set_inverter_watchdog_config({"device_sn": SN, "enabled": True})
        elif user_action == "reset_state":
            server.inverter_watchdog.reset_state(SN)
        elif user_action == "dismiss":
            server.api_inverter_watchdog_dismiss(SN)
        elif user_action == "other_device_off":
            await server.api_set_output({"device_sn": "OTHER", "port": "ac", "on": False})
        elif user_action == "replace_client":
            server.state.client = object()
        elif user_action == "pending_manual_off":
            off_started = asyncio.Event()

            async def delayed_manual_off(port, on, *, device_sn=None):
                calls.append((port, on, device_sn))
                off_started.set()
                await finish_off.wait()

            monkeypatch.setattr(server.state.client, "set_output", delayed_manual_off)
            pending_off = asyncio.create_task(
                server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
            )
            await asyncio.wait_for(off_started.wait(), 1)
    finally:
        resume.set()
        await asyncio.gather(*list(server._WATCHDOG_CYCLE_TASKS), return_exceptions=True)
        finish_off.set()
        if pending_off:
            await pending_off
    if user_action in ("none", "other_device_off"):
        assert calls[-1] == ("ac", True, SN)
    else:
        assert not any(on for port, on, sn in calls)


async def test_failed_manual_off_restores_previous_grace_stamp(recovery_server, monkeypatch):
    from fastapi import HTTPException

    server, calls, clock = recovery_server
    watchdog = server.inverter_watchdog.get_state(SN)
    watchdog.last_user_off_ts = 900

    async def failed_set_output(*args, **kwargs):
        raise server.DeviceClientError("fake command failure")

    monkeypatch.setattr(server.state.client, "set_output", failed_set_output)
    with pytest.raises(HTTPException):
        await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
    assert watchdog.last_user_off_ts == 900


@pytest.mark.parametrize("user_action", ["manual_off", "disable_in_worker", "dismiss"])
async def test_user_action_cancels_on_queued_behind_bridge_request(
    recovery_server,
    monkeypatch,
    user_action,
):
    server, calls, clock = recovery_server
    server.user_settings.update({"inverter_trip_recovery_min_w": 100})
    on_queued = asyncio.Event()
    dispatch_on = asyncio.Event()

    async def queued_setter(port, on, *, device_sn=None):
        if on:
            on_queued.set()
            await dispatch_on.wait()
        calls.append((port, on, device_sn))

    async def immediate_sleep(seconds):
        assert seconds == 2

    monkeypatch.setattr(server.state.client, "set_output", queued_setter)
    monkeypatch.setattr(server.asyncio, "sleep", immediate_sleep)
    for offset, output in ((0, 900), (2, 0), (4, 0), (8, 0)):
        clock[0] = 1000 + offset
        await server._inverter_watchdog_tick(
            SN, {"ac_on": True, "output_power_w": output}, 13, clock[0]
        )
    await asyncio.wait_for(on_queued.wait(), 1)
    pending = list(server._WATCHDOG_CYCLE_TASKS)
    try:
        if user_action == "manual_off":
            await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
        elif user_action == "disable_in_worker":
            await asyncio.to_thread(
                server.api_set_inverter_watchdog_config, {"device_sn": SN, "enabled": False}
            )
        else:
            server.api_inverter_watchdog_dismiss(SN)
    finally:
        dispatch_on.set()
        await asyncio.gather(*pending, return_exceptions=True)
    assert not any(on for port, on, sn in calls)


@pytest.mark.parametrize("outcomes", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("finish_order", [(0, 1), (1, 0)])
async def test_overlapping_manual_off_requests_keep_only_confirmed_grace(
    recovery_server,
    monkeypatch,
    outcomes,
    finish_order,
):
    server, calls, clock = recovery_server
    watchdog = server.inverter_watchdog.get_state(SN)
    watchdog.last_user_off_ts = 900
    started = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]
    requests = []

    async def delayed_off(port, on, *, device_sn=None):
        index = len(requests)
        requests.append(index)
        started[index].set()
        await finish[index].wait()
        if not outcomes[index]:
            raise server.DeviceClientError("fake command failure")

    monkeypatch.setattr(server.state.client, "set_output", delayed_off)
    tasks = []
    for index in range(2):
        tasks.append(
            asyncio.create_task(server.api_set_output({"device_sn": SN, "port": "ac", "on": False}))
        )
        await asyncio.wait_for(started[index].wait(), 1)
    # Two simultaneous intents suppress recovery before either RPC finishes.
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert len(requests) == 2
    for index in finish_order:
        finish[index].set()
        await asyncio.gather(tasks[index], return_exceptions=True)
    assert not watchdog.pending_user_off_intents
    assert watchdog.last_user_off_ts == (1000 if any(outcomes) else 900)


async def test_cancelled_manual_off_clears_only_its_pending_intent(recovery_server, monkeypatch):
    server, calls, clock = recovery_server
    started = asyncio.Event()
    never = asyncio.Event()

    async def delayed_off(*args, **kwargs):
        started.set()
        await never.wait()

    monkeypatch.setattr(server.state.client, "set_output", delayed_off)
    task = asyncio.create_task(server.api_set_output({"device_sn": SN, "port": "ac", "on": False}))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    watchdog = server.inverter_watchdog.get_state(SN)
    assert not watchdog.pending_user_off_intents
    assert watchdog.last_user_off_ts == 0


async def test_reset_keeps_pending_off_intent_until_request_finishes(recovery_server, monkeypatch):
    server, calls, clock = recovery_server
    started = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_off(port, on, *, device_sn=None):
        calls.append((port, on, device_sn))
        if not on:
            started.set()
            await finish.wait()

    monkeypatch.setattr(server.state.client, "set_output", delayed_off)
    task = asyncio.create_task(server.api_set_output({"device_sn": SN, "port": "ac", "on": False}))
    await asyncio.wait_for(started.wait(), 1)
    server.inverter_watchdog.reset_state(SN)
    try:
        await server._inverter_watchdog_tick(
            SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0]
        )
        assert calls == [("ac", False, SN)]
    finally:
        finish.set()
        await task
    watchdog = server.inverter_watchdog.get_state(SN)
    assert not watchdog.pending_user_off_intents
    assert watchdog.last_user_off_ts == 1000


async def test_slow_manual_off_still_gets_full_grace_after_acknowledgement(
    recovery_server,
    monkeypatch,
):
    server, calls, clock = recovery_server
    started = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_off(port, on, *, device_sn=None):
        calls.append((port, on, device_sn))
        if not on:
            started.set()
            await finish.wait()

    monkeypatch.setattr(server.state.client, "set_output", delayed_off)
    task = asyncio.create_task(server.api_set_output({"device_sn": SN, "port": "ac", "on": False}))
    await asyncio.wait_for(started.wait(), 1)
    clock[0] += 61
    finish.set()
    await task
    clock[0] += 59
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert calls == [("ac", False, SN)]
    clock[0] += 2
    await server._inverter_watchdog_tick(SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0])
    assert calls[-1] == ("ac", True, SN)


@pytest.mark.parametrize(
    "user_action",
    [
        "manual_off",
        "disable_in_worker",
        "dismiss",
        "cancel_poller",
        "other_device_off",
    ],
)
async def test_plain_recovery_on_is_cancelled_without_stopping_poller(
    recovery_server,
    monkeypatch,
    user_action,
):
    server, calls, clock = recovery_server
    on_queued = asyncio.Event()
    dispatch_on = asyncio.Event()
    poll_continued = []

    async def queued_setter(port, on, *, device_sn=None):
        if on:
            on_queued.set()
            await dispatch_on.wait()
        calls.append((port, on, device_sn))

    async def poll_tick():
        await server._inverter_watchdog_tick(
            SN, {"ac_on": False, "output_power_w": 0}, 13, clock[0]
        )
        poll_continued.append(True)

    monkeypatch.setattr(server.state.client, "set_output", queued_setter)
    poll = asyncio.create_task(poll_tick())
    await asyncio.wait_for(on_queued.wait(), 1)
    try:
        if user_action == "manual_off":
            await server.api_set_output({"device_sn": SN, "port": "ac", "on": False})
        elif user_action == "disable_in_worker":
            await asyncio.to_thread(
                server.api_set_inverter_watchdog_config, {"device_sn": SN, "enabled": False}
            )
        elif user_action == "cancel_poller":
            poll.cancel()
        elif user_action == "other_device_off":
            await server.api_set_output({"device_sn": "OTHER", "port": "ac", "on": False})
        else:
            server.api_inverter_watchdog_dismiss(SN)
    finally:
        dispatch_on.set()
        if user_action == "cancel_poller":
            with pytest.raises(asyncio.CancelledError):
                await poll
        else:
            await poll
    assert any(on for port, on, sn in calls) == (user_action == "other_device_off")
    assert poll_continued == ([] if user_action == "cancel_poller" else [True])
