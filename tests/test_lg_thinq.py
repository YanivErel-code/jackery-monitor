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
    "state", ["INITIAL", "RUNNING", "DRYING", "COOLING", "PAUSE", "WRINKLE_PREVENT", "DELAYED_START"]
)
def test_active_appliance_phases_never_release_interlock(lg, state):
    result = lg.classify_state({"runState": {"currentState": state}})
    assert result["activity"] == "active"


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
    context = lg.planning_context(config, now_ts=1000)
    assert context["expected_finish_ts"] == 2200
    assert context["warnings"]
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


@pytest.mark.parametrize(
    "failure", ["missing_token", "api_error", "device_error", "stale", "missing_device", "unknown", "no_selection"]
)
def test_monitoring_failures_warn_without_blocking_charging(lg, monkeypatch, failure):
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "synthetic-degraded" * 3, "US")
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {"device_id": "oven", "alias": "Oven", "activity": "off", "observed_at": 999}
    }
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["oven"]}
    if failure == "missing_token":
        lg.clear_credentials()
    elif failure == "api_error":
        lg.monitor.error = "LG token is invalid or expired"
    elif failure == "device_error":
        lg.monitor.devices["oven"].update(observed_at=0, error="LG API rate limit reached")
    elif failure == "stale":
        lg.monitor.devices["oven"]["observed_at"] = 900
    elif failure == "missing_device":
        config["lg_device_ids"] = ["missing"]
    elif failure == "unknown":
        lg.monitor.devices["oven"]["activity"] = "unknown"
    else:
        config["lg_device_ids"] = []
    assert lg.block_reason(config) is None
    context = lg.planning_context(config, now_ts=1000)
    assert context["warnings"] and context["monitoring_degraded"]
    assert not context["blocked_now"] and not context["timing_uncertain"]
    assert context["charge_not_before_ts"] is None
    assert lg.block_reason({**config, "lg_manual_hold": True})


def test_known_active_appliance_still_blocks_despite_another_appliance_error(lg, monkeypatch):
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    credentials = lg.save_credentials("thinqpat_" + "synthetic-degraded" * 3, "US")
    lg.monitor.credential_stamp = lg.credential_stamp(credentials)
    lg.monitor.devices = {
        "oven": {"device_id": "oven", "alias": "Oven", "activity": "active", "observed_at": 1000,
                 "states": ["COOKING"], "remaining_minutes": 20},
        "dryer": {"device_id": "dryer", "alias": "Dryer", "activity": "unknown", "observed_at": 0,
                  "error": "LG connection unavailable"},
    }
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["dryer", "oven"]}
    assert "Oven" in lg.block_reason(config)
    context = lg.planning_context(config, now_ts=1000)
    assert context["warnings"] and context["blocked_now"]
    assert not context["timing_uncertain"]
    assert context["charge_not_before_ts"] == 2500


async def test_unexpected_pre_charge_lg_error_warns_and_allows_mocked_plug_on(lg, monkeypatch):
    import kasa_client
    import smart_charge

    smart_charge.set_config(
        {"lg_interlock_enabled": True, "lg_device_ids": ["oven"], "kasa_device_host": "charge-plug"},
        "MAIN",
    )
    refresh = AsyncMock(side_effect=RuntimeError("private-token-must-not-appear"))
    monkeypatch.setattr(lg.monitor, "refresh", refresh)
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    monkeypatch.setattr(kasa_client, "_describe", lambda *args: {"is_on": True})
    result = await kasa_client.set_state("charge-plug", True)
    assert result["is_on"] is True
    refresh.assert_awaited_once_with({"oven"})
    device.turn_on.assert_awaited_once()
    assert lg.monitor.public_status()["warnings"]
    assert "private-token-must-not-appear" not in json.dumps(lg.monitor.public_status())


@pytest.mark.parametrize("failure", ["1103", "1305", "1306", "timeout", "device_list"])
async def test_lg_read_errors_allow_mocked_charging_without_watchdog_off(lg, monkeypatch, failure):
    from thinqconnect.thinq_api import ThinQAPIException

    import kasa_client
    import smart_charge

    lg.save_credentials("thinqpat_" + "synthetic-degraded" * 3, "US")
    smart_charge.set_config(
        {"lg_interlock_enabled": True, "lg_device_ids": ["oven"], "kasa_device_host": "charge-plug"},
        "MAIN",
    )
    api = AsyncMock()
    api.async_get_device_list.return_value = [{"deviceId": "oven", "deviceInfo": {"alias": "Oven"}}]
    if failure == "device_list":
        api.async_get_device_list.side_effect = ThinQAPIException("1103", "Invalid token", {})
    else:
        api.async_get_device_status.side_effect = (
            TimeoutError() if failure == "timeout" else ThinQAPIException(failure, "Unavailable", {})
        )
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    monkeypatch.setattr(kasa_client, "_describe", lambda *args: {"is_on": True})
    result = await kasa_client.set_state("charge-plug", True)
    assert result["is_on"] is True
    device.turn_on.assert_awaited_once()
    await lg.enforce_once(lambda serial: True, {})
    device.turn_off.assert_not_awaited()
    assert lg.monitor.public_status()["warnings"]
    if failure in {"1103", "device_list"}:
        assert "invalid or expired" in " ".join(lg.monitor.public_status()["warnings"])


def test_enabled_guard_without_configuration_or_cloud_is_non_blocking(lg):
    assert lg.block_reason({}) is None
    assert lg.block_reason({"lg_interlock_enabled": True}) is None
    assert lg.block_reason({"lg_interlock_enabled": True, "lg_device_ids": ["oven"]}) is None


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
    assert lg.block_reason(config) is None
    assert lg.protection_warnings(config)
    config["lg_manual_hold"] = True
    assert "manual" in lg.block_reason(config).lower()


async def test_central_kasa_guard_blocks_all_on_paths_but_never_off(lg, monkeypatch):
    import kasa_client
    import smart_charge

    smart_charge.set_config(
        {
            "lg_interlock_enabled": True,
            "lg_manual_hold": True,
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

    async def refresh(selected=None):
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


@pytest.mark.parametrize("mapped", [False, True])
async def test_background_poll_keeps_every_discovered_offline_appliance_fresh(
    lg, monkeypatch, mapped
):
    import asyncio

    from thinqconnect.thinq_api import ThinQAPIException

    import smart_charge

    lg.save_credentials("thinqpat_" + "synthetic-polling" * 3, "US")
    if mapped:
        smart_charge.set_config(
            {"lg_interlock_enabled": True, "lg_device_ids": ["dryer"]}, "MAIN"
        )
    api = AsyncMock()
    appliance_ids = ("dryer", "washer", "styler", "refrigerator")
    api.async_get_device_list.return_value = [
        {"deviceId": device_id, "deviceInfo": {"alias": device_id}}
        for device_id in appliance_ids
    ]
    api.async_get_device_status.side_effect = ThinQAPIException("1222", "Disconnected", {})
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    clock = [1000]
    monkeypatch.setattr(lg.time, "time", lambda: clock[0])
    await lg.monitor.refresh()
    snapshots = []

    async def sleep(seconds):
        snapshots.append(lg.monitor.public_status())
        clock[0] += seconds
        if len(snapshots) == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(lg.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await lg.poll_loop()
    assert len(snapshots[-1]["devices"]) == len(appliance_ids)
    assert all(
        device["fresh"] and device["activity"] == "off" and device["assumed_off"]
        for snapshot in snapshots
        for device in snapshot["devices"]
    )
    assert api.async_get_device_status.await_count == len(appliance_ids) * 6


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


@pytest.mark.parametrize("device_type", ["DEVICE_DRYER", "DEVICE_OVEN", "DEVICE_AIR_CONDITIONER"])
async def test_fresh_lg_disconnected_response_is_assumed_off_for_any_appliance(
    lg, monkeypatch, device_type
):
    from thinqconnect.thinq_api import ThinQAPIException

    credentials = lg.save_credentials("thinqpat_" + "synthetic-offline" * 3, "US")
    api = AsyncMock()
    api.async_get_device_list.return_value = [
        {"deviceId": "appliance", "deviceInfo": {"deviceType": device_type}}
    ]
    api.async_get_device_status.side_effect = ThinQAPIException(
        "1222", "Disconnected", {"Authorization": credentials["token"]}
    )
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    monkeypatch.setattr(lg.time, "time", lambda: 1000)
    await lg.monitor.refresh()
    device = lg.monitor.public_status()["devices"][0]
    assert device["fresh"] and device["activity"] == "off"
    assert device["assumed_off"] and device["disconnected"]
    assert device["states"] == ["DISCONNECTED"]
    assert device["remaining_minutes"] is None
    assert credentials["token"] not in json.dumps(device)
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["appliance"]}
    assert lg.block_reason(config) is None
    context = lg.planning_context(config, now_ts=1000)
    assert not context["blocked_now"] and not context["timing_uncertain"]
    assert context["appliances"][0]["assumed_off"]
    assert lg.block_reason({**config, "lg_manual_hold": True})
    monkeypatch.setattr(lg.time, "time", lambda: 1076)
    assert lg.block_reason(config) is None
    assert lg.protection_warnings(config)
    assert lg.monitor.public_status()["devices"][0]["activity"] == "unknown"


@pytest.mark.parametrize("failure", ["timeout", "1103", "1305", "1306", "impostor"])
async def test_unavailable_requests_never_reuse_an_offline_assumption(lg, monkeypatch, failure):
    from thinqconnect.thinq_api import ThinQAPIException

    lg.save_credentials("thinqpat_" + "synthetic-offline" * 3, "US")
    api = AsyncMock()
    api.async_get_device_list.return_value = [{"deviceId": "appliance", "deviceInfo": {}}]
    api.async_get_device_status.side_effect = ThinQAPIException("1222", "Disconnected", {})
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["appliance"]}
    assert lg.block_reason(config) is None
    if failure == "timeout":
        error = TimeoutError()
    elif failure == "impostor":
        error = RuntimeError()
        error.code = "1222"
    else:
        error = ThinQAPIException(failure, "Unavailable", {})
    api.async_get_device_status.side_effect = error
    await lg.monitor.refresh()
    device = lg.monitor.public_status()["devices"][0]
    assert device["activity"] == "unknown" and not device["fresh"]
    assert not device["assumed_off"] and not device["disconnected"]
    assert lg.block_reason(config) is None
    assert lg.protection_warnings(config)


@pytest.mark.parametrize("state", ["RUNNING", "INITIAL"])
async def test_offline_appliance_becoming_active_blocks_fresh_pre_charge_check(lg, monkeypatch, state):
    from thinqconnect.thinq_api import ThinQAPIException

    import kasa_client
    import smart_charge

    lg.save_credentials("thinqpat_" + "synthetic-offline" * 3, "US")
    smart_charge.set_config(
        {
            "lg_interlock_enabled": True,
            "lg_device_ids": ["appliance"],
            "kasa_device_host": "charge-plug",
        },
        "MAIN",
    )
    api = AsyncMock()
    api.async_get_device_list.return_value = [{"deviceId": "appliance", "deviceInfo": {}}]
    api.async_get_device_status.side_effect = [
        ThinQAPIException("1222", "Disconnected", {}),
        {"runState": {"currentState": state}, "timer": {"remainHour": 0, "remainMinute": 20}},
    ]
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    assert lg.block_reason_for_host("charge-plug") is None
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    with pytest.raises(lg.InterlockBlocked):
        await kasa_client.set_state("charge-plug", True)
    device.turn_on.assert_not_awaited()
    record = lg.monitor.public_status()["devices"][0]
    assert record["activity"] == "active" and not record["assumed_off"]


async def test_device_list_failure_is_not_an_offline_assumption(lg, monkeypatch):
    from thinqconnect.thinq_api import ThinQAPIException

    lg.save_credentials("thinqpat_" + "synthetic-offline" * 3, "US")
    api = AsyncMock()
    api.async_get_device_list.side_effect = ThinQAPIException("1222", "Disconnected", {})
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    config = {"lg_interlock_enabled": True, "lg_device_ids": ["appliance"]}
    assert lg.block_reason(config) is None
    assert lg.protection_warnings(config)


async def test_reported_offline_appliance_allows_charging_after_another_fresh_check(
    lg, monkeypatch
):
    from thinqconnect.thinq_api import ThinQAPIException

    import kasa_client
    import smart_charge

    lg.save_credentials("thinqpat_" + "synthetic-offline" * 3, "US")
    smart_charge.set_config(
        {
            "lg_interlock_enabled": True,
            "lg_device_ids": ["appliance"],
            "kasa_device_host": "charge-plug",
        },
        "MAIN",
    )
    api = AsyncMock()
    api.async_get_device_list.return_value = [{"deviceId": "appliance", "deviceInfo": {}}]
    api.async_get_device_status.side_effect = ThinQAPIException("1222", "Disconnected", {})
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    await lg.monitor.refresh()
    device = AsyncMock()
    monkeypatch.setattr(kasa_client, "_connect", AsyncMock(return_value=device))
    monkeypatch.setattr(kasa_client, "_describe", lambda *args: {"is_on": True})
    result = await kasa_client.set_state("charge-plug", True)
    assert result["is_on"] is True
    device.turn_on.assert_awaited_once()
    assert api.async_get_device_status.await_count == 2


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
            {"kasa_device_host": host, "lg_interlock_enabled": True, "lg_device_ids": ["oven"],
             "lg_manual_hold": True},
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
        {"kasa_device_host": "charge-plug", "lg_interlock_enabled": True, "lg_manual_hold": True}, "MAIN"
    )
    toggle = AsyncMock(side_effect=[RuntimeError("Unavailable"), {"is_on": False}])
    monkeypatch.setattr(kasa_client, "set_state", toggle)
    last_off = {}
    await lg.enforce_once(lambda serial: True, last_off)
    assert not last_off
    await lg.enforce_once(lambda serial: True, last_off)
    assert "charge-plug" in last_off
