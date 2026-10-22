import importlib
import json
import os
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def lg(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKERY_AT_REST_KEY_FILE", str(tmp_path / ".key"))
    monkeypatch.setenv("JACKERY_LG_THINQ_CREDS_FILE", str(tmp_path / "lg.json"))
    monkeypatch.setenv("JACKERY_SMART_CHARGE_FILE", str(tmp_path / "smart.json"))
    for name in ("crypto_util", "smart_charge"):
        importlib.reload(importlib.import_module(name))
    import lg_thinq

    return importlib.reload(lg_thinq)


@pytest.mark.parametrize(
    "state", ["RUNNING", "DRYING", "COOLING", "PAUSE", "WRINKLE_PREVENT", "DELAYED_START"]
)
def test_active_appliance_phases_never_release_interlock(lg, state):
    result = lg.classify_state({"runState": {"currentState": state}})
    assert result["activity"] != "off"


@pytest.mark.parametrize("state", ["OFF", "POWER_OFF"])
def test_explicit_off_state_releases_interlock(lg, state):
    assert lg.classify_state({"runState": {"currentState": state}})["activity"] == "off"


def test_generic_air_conditioner_and_multi_compartment_oven(lg):
    assert (
        lg.classify_state({"operation": {"airConOperationMode": "POWER_OFF"}})["activity"] == "off"
    )
    assert (
        lg.classify_state({"operation": {"airConOperationMode": "POWER_ON"}})["activity"]
        == "active"
    )
    oven = {
        "upper": {"runState": {"currentState": "POWER_OFF"}},
        "lower": {"runState": {"currentState": "RUNNING"}},
    }
    assert lg.classify_state(oven)["activity"] == "active"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"runState": {}},
        {"runState": {"currentState": "NEW_STATE"}},
        {"remoteControlEnable": {"remoteControlEnabled": False}},
    ],
)
def test_unknown_or_unrelated_fields_cannot_mean_idle(lg, payload):
    assert lg.classify_state(payload)["activity"] == "unknown"


def test_unknown_compartment_does_not_get_hidden_by_off(lg):
    payload = {
        "top": {"runState": {"currentState": "POWER_OFF"}},
        "bottom": {"runState": {"currentState": "NEW_STATE"}},
    }
    assert lg.classify_state(payload)["activity"] == "unknown"


@pytest.mark.parametrize("incomplete", [{}, None, {"otherField": "OFF"}])
def test_missing_compartment_state_does_not_release_interlock(lg, incomplete):
    payload = {
        "top": {"runState": {"currentState": "POWER_OFF"}},
        "bottom": {"runState": incomplete},
    }
    assert lg.classify_state(payload)["activity"] == "unknown"


def test_credentials_encrypted_private_and_client_id_stable(lg):
    token = "thinqpat_" + "unit-test-credential" * 3
    first = lg.save_credentials(token, "US")
    assert lg.load_credentials()["token"] == token
    file = lg.credentials_path()
    assert token not in file.read_text()
    assert json.loads(file.read_text())["alg"] == "AES-256-GCM"
    assert os.stat(file).st_mode & 0o777 == 0o600
    second = lg.save_credentials(token + "replacement", "US")
    assert first["client_id"] == second["client_id"]
    assert "token" not in lg.monitor.public_status()


def test_planning_waits_for_latest_selected_finish_and_keeps_real_interlock(lg, monkeypatch):
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "testing" * 8, "US")
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {
            "device_id": "oven",
            "activity": "active",
            "states": ["COOKING"],
            "remaining_minutes": 20,
            "observed_at": 1000,
        },
        "ac": {
            "device_id": "ac",
            "activity": "active",
            "states": ["RUNNING"],
            "remaining_minutes": 30,
            "observed_at": 1000,
        },
        "dryer": {
            "device_id": "dryer",
            "activity": "active",
            "states": ["RUNNING"],
            "remaining_minutes": 120,
            "observed_at": 1000,
        },
    }
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["oven", "ac"]}
    context = lg.planning_context(config, now_ts=1000)
    assert context["expected_finish_ts"] == 2800
    assert context["charge_not_before_ts"] == 3100
    assert context["blocked_now"] is True
    assert not context["timing_uncertain"]
    assert lg.block_reason(config)
    lg.monitor.devices["ac"]["remaining_minutes"] = 90
    assert lg.planning_context(config, now_ts=1000)["expected_finish_ts"] == 6400
    lg.monitor.devices["ac"]["remaining_minutes"] = 30
    lg.monitor.devices["ac"]["states"] = ["PAUSED"]
    assert lg.planning_context(config, now_ts=1000)["timing_uncertain"]
    lg.monitor.devices["ac"]["states"] = ["RUNNING"]
    lg.monitor.devices["ac"]["observed_at"] = 900
    assert lg.planning_context(config, now_ts=1000)["expected_finish_ts"] is None
    config["lg_manual_hold"] = True
    assert lg.planning_context(config, now_ts=1000)["timing_uncertain"]


def test_nested_appliance_timers_use_longest_remaining_time(lg):
    state = lg.classify_state(
        {
            "upper": {
                "runState": {"currentState": "COOKING"},
                "timer": {"remainHour": 0, "remainMinute": 20},
            },
            "lower": {
                "runState": {"currentState": "COOKING"},
                "timer": {"remainHour": 0, "remainMinute": 40},
            },
        }
    )
    assert state["remaining_minutes"] == 40


def test_soc_at_finish_interpolates_only_inside_forecast(lg):
    forecast = {
        "forecast": [
            {"ts": 4600, "duration_h": 1, "predicted_soc": 40},
            {"ts": 8200, "duration_h": 1, "predicted_soc": 30},
        ]
    }
    assert lg.soc_at_finish(forecast, 50, now_ts=1000, finish_ts=2800) == 45
    assert lg.soc_at_finish(forecast, 50, now_ts=1000, finish_ts=9000) is None


def test_elapsed_cycle_timer_never_releases_running_appliance(lg, monkeypatch):
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "testing" * 8, "US")
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {
            "device_id": "oven",
            "activity": "active",
            "states": ["RUNNING"],
            "remaining_minutes": 0,
            "observed_at": 1000,
        }
    }
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["oven"]}
    assert lg.planning_context(config, now_ts=1000)["timing_uncertain"]
    assert lg.block_reason(config)


def test_untimed_running_compartment_cannot_borrow_another_compartments_timer(lg):
    state = lg.classify_state(
        {
            "upper": {
                "runState": {"currentState": "COOKING"},
                "timer": {"remainHour": 0, "remainMinute": 20},
            },
            "lower": {"runState": {"currentState": "COOKING"}},
        }
    )
    assert state["remaining_minutes"] is None


def test_enabled_guard_fails_closed_without_configuration_or_cloud(lg):
    assert lg.block_reason({}) is None
    assert lg.block_reason({"lg_interlock_enabled": True})
    assert lg.block_reason({"lg_interlock_enabled": True, "lg_device_ids": ["oven"]})


def test_multiple_selected_appliances_stale_states_and_manual_hold(lg, monkeypatch):
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "testing" * 8, "US")
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {"device_id": "oven", "alias": "Oven", "activity": "off", "observed_at": 999},
        "ac": {
            "device_id": "ac",
            "alias": "Air conditioner",
            "activity": "active",
            "observed_at": 999,
        },
    }
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["oven", "ac"]}
    assert "Air conditioner" in lg.block_reason(config)
    lg.monitor.devices["ac"]["activity"] = "off"
    assert lg.block_reason(config) is None
    lg.monitor.devices["ac"]["observed_at"] = 900
    assert lg.block_reason(config)
    config["lg_manual_hold"] = True
    assert "manual" in lg.block_reason(config).lower()


async def test_central_kasa_guard_blocks_all_on_paths_but_never_off(lg, monkeypatch):
    import kasa_client
    import smart_charge

    smart_charge.set_config(
        {
            "lg_interlock_enabled": True,
            "lg_device_ids": ["oven"],
            "kasa_device_host": "charge-plug",
        },
        "MAIN",
    )
    device = AsyncMock()
    device.features = {}
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    monkeypatch.setattr(kasa_client, "_describe", lambda *args: {"is_on": False})
    with pytest.raises(lg.InterlockBlocked):
        await kasa_client.set_state("charge-plug", True)
    kasa_client._connect.assert_not_awaited()
    await kasa_client.set_state("charge-plug", False)
    device.turn_off.assert_awaited_once()
    device.turn_on.assert_not_awaited()


async def test_guard_is_rechecked_after_async_plug_connection(lg, monkeypatch):
    import kasa_client

    checks = iter([None, "LG 240 V interlock: appliance active"])
    monkeypatch.setattr(lg, "block_reason_for_host", lambda host: next(checks))
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    with pytest.raises(lg.InterlockBlocked):
        await kasa_client.set_state("charge-plug", True)
    device.turn_on.assert_not_awaited()


async def test_fresh_cloud_recheck_can_block_previously_allowed_charging(lg, monkeypatch):
    import kasa_client

    monkeypatch.setattr(
        lg, "block_reason_for_host", lambda host: None if not refreshed else "LG still running"
    )
    refreshed = []

    async def refresh(host):
        refreshed.append(host)

    monkeypatch.setattr(lg, "refresh_for_host", refresh, raising=False)
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    with pytest.raises(lg.InterlockBlocked):
        await kasa_client.set_state("charge-plug", True)
    assert refreshed == ["charge-plug"]
    device.turn_on.assert_not_awaited()


async def test_changed_lg_timer_wakes_replanning_but_unchanged_snapshot_does_not(lg, monkeypatch):
    import asyncio

    import smart_charge

    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "testing" * 8, "US")
    smart_charge.set_config({"lg_interlock_enabled": True, "lg_device_ids": ["oven"]}, "MAIN")
    remaining = iter([30, 90, 90])
    wakes = []

    async def refresh(selected):
        lg.monitor.credential_stamp = lg.credential_stamp(credentials)
        lg.monitor.devices = {
            "oven": {
                "device_id": "oven",
                "activity": "active",
                "states": ["RUNNING"],
                "remaining_minutes": next(remaining),
                "observed_at": 1000,
            }
        }

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(lg.monitor, "refresh", refresh)
    monkeypatch.setattr(lg.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await lg.poll_loop(lambda: wakes.append(True))
    assert len(wakes) == 2


def test_unselected_active_appliance_does_not_block_and_mappings_are_per_jackery(lg, monkeypatch):
    import smart_charge

    credentials = lg.save_credentials("thinqpat_" + "testing" * 8, "US")
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {"device_id": "oven", "activity": "off", "observed_at": 999},
        "ac": {"device_id": "ac", "activity": "active", "observed_at": 999},
    }
    smart_charge.set_config(
        {"lg_interlock_enabled": True, "lg_device_ids": ["oven"], "kasa_device_host": "first-plug"},
        "FIRST",
    )
    smart_charge.set_config(
        {"lg_interlock_enabled": True, "lg_device_ids": ["ac"], "kasa_device_host": "second-plug"},
        "SECOND",
    )
    assert lg.block_reason_for_host("first-plug") is None
    assert lg.block_reason_for_host("second-plug")
    assert lg.block_reason_for_host("unrelated-plug") is None
    smart_charge.set_config({"kasa_device_host": "first-plug"}, "SECOND")
    assert lg.block_reason_for_host("first-plug")


async def test_pinned_sdk_contract_only_uses_get_endpoints(lg, monkeypatch):
    import aiohttp

    credentials = lg.save_credentials("thinqpat_" + "contract-test" * 4, "US")
    async with aiohttp.ClientSession() as session:
        api = lg.make_api(session, credentials)
        request = AsyncMock(return_value=[])
        monkeypatch.setattr(api, "async_request", request)
        await api.async_get_device_list(timeout=8)
        await api.async_get_device_status("oven", timeout=8)
        assert [
            (call.kwargs["method"], call.kwargs["endpoint"]) for call in request.call_args_list
        ] == [("GET", "devices"), ("GET", "devices/oven/state")]


async def test_sdk_requests_are_read_only_and_errors_are_sanitized(lg, monkeypatch):
    credentials = lg.save_credentials("thinqpat_" + "secret-for-test" * 3, "US")
    api = AsyncMock()
    api.async_get_device_list.return_value = [
        {"deviceId": "oven", "deviceInfo": {"alias": "Oven", "deviceType": "DEVICE_OVEN"}},
        {"deviceId": "dryer", "deviceInfo": {"alias": "Dryer", "deviceType": "DEVICE_DRYER"}},
    ]
    api.async_get_device_status.side_effect = [
        {"runState": {"currentState": "POWER_OFF"}},
        RuntimeError(credentials["token"]),
    ]
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    public = lg.monitor.public_status()
    assert credentials["token"] not in json.dumps(public)
    assert lg.monitor.devices["oven"]["activity"] == "off"
    assert lg.monitor.devices["dryer"]["activity"] == "unknown"
    assert not [call for call in api.mock_calls if "post" in str(call) or "control(" in str(call)]


async def test_rotation_during_refresh_discards_old_account_results(lg, monkeypatch):
    lg.save_credentials("thinqpat_" + "old-account" * 5, "US")
    api = AsyncMock()

    async def rotate(*args, **kwargs):
        lg.save_credentials("thinqpat_" + "new-account" * 5, "US")
        return [{"deviceId": "old-oven", "deviceInfo": {"alias": "Old oven"}}]

    api.async_get_device_list.side_effect = rotate
    api.async_get_device_status.return_value = {"runState": {"currentState": "POWER_OFF"}}
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    assert not lg.monitor.devices


def test_legacy_settings_saves_do_not_disable_existing_protection(lg):
    import smart_charge

    smart_charge.set_config(
        {
            "mode": "active",
            "lg_interlock_enabled": True,
            "lg_device_ids": ["oven"],
            "lg_manual_hold": True,
        },
        "MAIN",
    )
    saved = smart_charge.set_config({"mode": "active", "max_charge_w": 1400}, "MAIN")
    assert saved["lg_interlock_enabled"] is True
    assert saved["lg_device_ids"] == ["oven"]
    assert saved["lg_manual_hold"] is True


async def test_independent_guard_switches_only_eligible_charging_plug_off(lg, monkeypatch):
    import kasa_client
    import smart_charge

    for serial, host in (("MAIN", "charge-plug"), ("HOME", "home-plug")):
        smart_charge.set_config(
            {"kasa_device_host": host, "lg_interlock_enabled": True, "lg_device_ids": ["oven"]},
            serial,
        )
    toggle = AsyncMock()
    monkeypatch.setattr(kasa_client, "set_state", toggle)
    last_off = {}
    await lg.enforce_once(lambda serial: serial == "MAIN", last_off)
    toggle.assert_awaited_once_with("charge-plug", False)
    await lg.enforce_once(lambda serial: serial == "MAIN", last_off)
    toggle.assert_awaited_once()


async def test_failed_off_command_is_retried(lg, monkeypatch):
    import kasa_client
    import smart_charge

    smart_charge.set_config(
        {"kasa_device_host": "charge-plug", "lg_interlock_enabled": True}, "MAIN"
    )
    toggle = AsyncMock(side_effect=[RuntimeError("Unavailable"), {"is_on": False}])
    monkeypatch.setattr(kasa_client, "set_state", toggle)
    last_off = {}
    await lg.enforce_once(lambda serial: True, last_off)
    assert not last_off
    await lg.enforce_once(lambda serial: True, last_off)
    assert "charge-plug" in last_off
