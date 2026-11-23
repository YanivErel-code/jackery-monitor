"""Apple push: the signing key, the phone registry and what is sent to Apple."""

from __future__ import annotations

import base64
import importlib
import json

import httpx
import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

_KEY = ECC.generate(curve="P-256")
PEM = _KEY.export_key(format="PEM", use_pkcs8=True)
TOKEN_A, TOKEN_B = "a1" * 32, "b2" * 32


@pytest.fixture()
def apns(isolated_data):
    import crypto_util
    importlib.reload(crypto_util)
    import apns as module
    importlib.reload(module)
    return module


@pytest.fixture()
def ready(apns):
    apns.save_key("TEAM123456", "KEY1234567", "com.example.jackery", PEM)
    apns.register_device("install-0001", {"name": "Phone", "environment": "production",
                                          "push_token": TOKEN_A, "start_token": TOKEN_B})
    return apns


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _capture(responses=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if responses:
            return responses.pop(0)
        return httpx.Response(200)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- key ----

def test_key_is_validated_and_stored_encrypted(apns, isolated_data):
    assert apns.is_configured() is False
    for team, key_id, bundle, pem in [
        ("short", "KEY1234567", "com.example.jackery", PEM),
        ("TEAM123456", "nope", "com.example.jackery", PEM),
        ("TEAM123456", "KEY1234567", "bad bundle", PEM),
        ("TEAM123456", "KEY1234567", "com.example.jackery", "not a key"),
        ("TEAM123456", "KEY1234567", "com.example.jackery", _KEY.public_key().export_key(format="PEM")),
    ]:
        with pytest.raises(ValueError):
            apns.save_key(team, key_id, bundle, pem)
    assert apns.is_configured() is False

    apns.save_key("TEAM123456", "KEY1234567", "com.example.jackery", PEM)
    stored = (isolated_data / "apns.json").read_text()
    assert "PRIVATE KEY" not in stored and json.loads(stored)["key"]["alg"] == "AES-256-GCM"
    status = apns.public_status()
    assert status["configured"] and status["key_id"] == "KEY1234567" and "key" not in status
    apns.remove_key()
    assert apns.is_configured() is False


def test_provider_token_is_a_valid_es256_jwt_and_is_reused(ready):
    config = ready._config()
    token = ready.provider_token(config, now=1_000_000)
    head, body, signature = token.split(".")
    assert json.loads(_unb64(head)) == {"alg": "ES256", "kid": "KEY1234567"}
    assert json.loads(_unb64(body)) == {"iss": "TEAM123456", "iat": 1_000_000}
    raw = _unb64(signature)
    assert len(raw) == 64                                    # raw r||s, as JWS requires
    DSS.new(_KEY.public_key(), "fips-186-3", encoding="binary").verify(SHA256.new(f"{head}.{body}".encode()), raw)

    assert ready.provider_token(config, now=1_000_000 + 49 * 60) == token
    assert ready.provider_token(config, now=1_000_000 + 51 * 60) != token


# ---- phones ----

def test_register_merges_tokens_and_rejects_junk(apns):
    apns.register_device("install-0001", {"name": "Phone", "environment": "development", "push_token": TOKEN_A})
    apns.register_device("install-0001", {"start_token": TOKEN_B})
    device = apns.devices()["install-0001"]
    assert device["push_token"] == TOKEN_A and device["start_token"] == TOKEN_B and device["environment"] == "development"
    apns.register_device("install-0001", {"push_token": ""})
    assert apns.devices()["install-0001"]["push_token"] is None
    for bad_id, patch in [("x", {}), ("install-0001", {"push_token": "zz"}), ("install-0001", {"environment": "staging"}),
                          ("../../etc", {})]:
        with pytest.raises(ValueError):
            apns.register_device(bad_id, patch)
    status = apns.public_status()["devices"]
    assert status == [{"install_id": "install-0001", "name": "Phone", "environment": "development",
                       "updated_ts": status[0]["updated_ts"], "alerts": False, "live_activity": True, "widgets": False}]
    assert TOKEN_B not in json.dumps(status)
    assert apns.remove_device("install-0001") and not apns.remove_device("install-0001")


# ---- sending ----

async def test_alert_push_is_what_apple_expects(ready):
    seen, client = _capture()
    async with client:
        error = await ready.send_alert("Battery low", "At 19%.", priority="high", rule="low_battery", client=client)
    assert error is None and len(seen) == 1
    request = seen[0]
    assert str(request.url) == f"https://api.push.apple.com/3/device/{TOKEN_A}"
    assert request.headers["apns-topic"] == "com.example.jackery"
    assert request.headers["apns-push-type"] == "alert" and request.headers["apns-priority"] == "10"
    assert request.headers["apns-collapse-id"] == "low_battery"
    assert request.headers["authorization"].startswith("bearer ") and request.headers["authorization"].count(".") == 2
    assert json.loads(request.content) == {"aps": {
        "alert": {"title": "Battery low", "body": "At 19%."}, "sound": "default",
        "interruption-level": "time-sensitive", "thread-id": "low_battery"}}


async def test_development_builds_go_to_the_sandbox(ready):
    ready.register_device("install-0001", {"environment": "development"})
    seen, client = _capture()
    async with client:
        await ready.send_alert("t", "m", client=client)
    assert seen[0].url.host == "api.sandbox.push.apple.com"
    assert json.loads(seen[0].content)["aps"]["interruption-level"] == "active"


async def test_a_dead_token_is_dropped_and_other_failures_are_kept(ready):
    seen, client = _capture([httpx.Response(500, json={"reason": "InternalServerError"})])
    async with client:
        error = await ready.send_alert("t", "m", client=client)
    assert "InternalServerError" in error and ready.devices()["install-0001"]["push_token"] == TOKEN_A

    seen, client = _capture([httpx.Response(410, json={"reason": "Unregistered"})])
    async with client:
        error = await ready.send_alert("t", "m", client=client)
    assert "Unregistered" in error
    assert ready.devices()["install-0001"]["push_token"] is None
    assert ready.devices()["install-0001"]["start_token"] == TOKEN_B


async def test_one_phone_accepting_counts_as_delivered(ready):
    ready.register_device("install-0002", {"push_token": "c3" * 32})
    seen, client = _capture([httpx.Response(400, json={"reason": "BadDeviceToken"}), httpx.Response(200)])
    async with client:
        assert await ready.send_alert("t", "m", client=client) is None
    assert len(seen) == 2


async def test_nothing_is_sent_without_a_key_or_a_phone(apns):
    seen, client = _capture()
    async with client:
        assert "key is missing" in await apns.send_alert("t", "m", client=client)
        apns.save_key("TEAM123456", "KEY1234567", "com.example.jackery", PEM)
        assert "No phone is registered" in await apns.send_alert("t", "m", client=client)

        def drop(request):
            raise httpx.ConnectError("no route")

        apns.register_device("install-0001", {"push_token": TOKEN_A})
        async with httpx.AsyncClient(transport=httpx.MockTransport(drop)) as offline:
            assert "Could not reach Apple" in await apns.send_alert("t", "m", client=offline)
    assert seen == []


CONTENT = {"socPct": 42, "state": "charging", "status": "Charging", "title": "Charging until 3 PM.", "tone": "ok",
           "gridW": 1200, "solarW": 0, "outputW": 430, "untilTs": 5000.0}


async def test_live_activity_start_update_and_end(ready):
    ready.register_device("install-0001", {"activity_token": "d4" * 40})
    seen, client = _capture()
    async with client:
        assert await ready.live_start(CONTENT, device_name="Explorer", alert=("Charging", "Until 3 PM."),
                                      now=1000, client=client) is None
        # a new activity makes the old activity token useless
        assert ready.devices()["install-0001"]["activity_token"] is None
        assert "No phone is registered" in await ready.live_update(CONTENT, now=1060, client=client)
        ready.register_device("install-0001", {"activity_token": "e5" * 40})
        assert await ready.live_update(CONTENT, now=1120, client=client) is None
        assert await ready.live_end({**CONTENT, "status": "Charging finished"}, now=1180, client=client) is None
    start, update, end = seen
    assert str(start.url).endswith(TOKEN_B) and str(update.url).endswith("e5" * 40)
    for request in seen:
        assert request.headers["apns-topic"] == "com.example.jackery.push-type.liveactivity"
        assert request.headers["apns-push-type"] == "liveactivity"
    assert [r.headers["apns-priority"] for r in seen] == ["10", "5", "10"]
    assert json.loads(start.content) == {"aps": {
        "timestamp": 1000, "event": "start", "content-state": CONTENT,
        "attributes-type": "ChargingAttributes", "attributes": {"deviceName": "Explorer"},
        "alert": {"title": "Charging", "body": "Until 3 PM."}, "stale-date": 1000 + 30 * 60}}
    assert json.loads(update.content) == {"aps": {
        "timestamp": 1120, "event": "update", "content-state": CONTENT, "stale-date": 1120 + 30 * 60}}
    ended = json.loads(end.content)["aps"]
    assert ended["event"] == "end" and ended["dismissal-date"] == 1180 + 15 * 60 and "stale-date" not in ended
    assert ready.devices()["install-0001"]["activity_token"] is None


async def test_widget_reload_push_is_what_apple_expects(ready):
    assert "No phone is registered" in await ready.reload_widgets()
    ready.register_device("install-0001", {"widget_token": "f6" * 32})
    assert ready.public_status()["devices"][0]["widgets"] is True
    seen, client = _capture()
    async with client:
        assert await ready.reload_widgets(client=client) is None
    request = seen[0]
    assert str(request.url).endswith("f6" * 32)
    assert request.headers["apns-push-type"] == "widgets"
    assert request.headers["apns-topic"] == "com.example.jackery.push-type.widgets"
    assert json.loads(request.content) == {"aps": {"content-changed": True}}


async def test_a_low_priority_alert_is_silent_and_carries_its_page(ready):
    seen, client = _capture()
    async with client:
        await ready.send_alert("Backup failed", "smb", priority="low", rule="backup_failed", page="settings", client=client)
        await ready.send_alert("Battery low", "19%", priority="high", rule="low_battery", page="live", client=client)
    quiet, loud = (json.loads(r.content) for r in seen)
    assert quiet == {"aps": {"alert": {"title": "Backup failed", "body": "smb"}, "thread-id": "backup_failed",
                             "interruption-level": "passive"}, "page": "settings"}
    assert "sound" not in quiet["aps"] and seen[0].headers["apns-priority"] == "5"
    assert loud["aps"]["sound"] == "default" and loud["page"] == "live" and seen[1].headers["apns-priority"] == "10"


def test_api_token_is_stored_hashed_replaced_on_reissue_and_revoked_with_the_phone(apns, isolated_data):
    token = apns.issue_api_token("install-0001")
    assert len(token) >= 40 and apns.verify_api_token(token)
    assert token not in (isolated_data / "app-devices.json").read_text()
    assert not apns.verify_api_token("") and not apns.verify_api_token(None) and not apns.verify_api_token(token + "x")
    newer = apns.issue_api_token("install-0001")
    assert apns.verify_api_token(newer) and not apns.verify_api_token(token)
    apns.register_device("install-0001", {"push_token": TOKEN_A})           # registering keeps the token
    assert apns.verify_api_token(newer)
    apns.remove_device("install-0001")
    assert not apns.verify_api_token(newer)
    with pytest.raises(ValueError):
        apns.issue_api_token("x")


async def test_restart_only_reaches_phones_without_a_card(ready):
    ready.register_device("install-0001", {"activity_token": "d4" * 40})
    ready.register_device("install-0002", {"start_token": "c3" * 32})
    seen, client = _capture()
    async with client:
        assert await ready.live_start(CONTENT, device_name="E", alert=("a", "b"), now=1, only_missing=True, client=client) is None
    assert [str(r.url)[-64:] for r in seen] == ["c3" * 32]
    assert ready.devices()["install-0001"]["activity_token"] == "d4" * 40        # the showing card is left alone


async def test_a_new_card_first_removes_the_one_that_just_ended(ready):
    """Charging stops and five minutes later the washer starts a hold: the
    finished card must not sit beside the new one."""
    ready.register_device("install-0001", {"activity_token": "d4" * 40})
    seen, client = _capture()
    async with client:
        await ready.live_end({**CONTENT, "status": "Charging finished"}, now=1000, client=client)
        assert ready.devices()["install-0001"]["activity_token"] is None
        await ready.live_start(CONTENT, device_name="E", alert=("a", "b"), now=1300, client=client)
    ended, removed, started = (json.loads(r.content)["aps"] for r in seen)
    assert [str(r.url)[-80:] for r in seen[:2]] == ["d4" * 40, "d4" * 40] and str(seen[2].url).endswith(TOKEN_B)
    assert ended["event"] == "end" and ended["dismissal-date"] == 1000 + 15 * 60
    assert removed["event"] == "end" and removed["dismissal-date"] == 1300         # gone at once
    assert removed["content-state"]["status"] == "Charging finished"
    assert started["event"] == "start"
    assert "lingering" not in ready.devices()["install-0001"]


async def test_a_card_that_has_already_left_the_lock_screen_is_not_chased(ready):
    ready.register_device("install-0001", {"activity_token": "d4" * 40})
    seen, client = _capture()
    async with client:
        await ready.live_end(CONTENT, now=1000, client=client)
        await ready.live_start(CONTENT, device_name="E", alert=("a", "b"), now=1000 + 16 * 60, client=client)
    assert [json.loads(r.content)["aps"]["event"] for r in seen] == ["end", "start"]

