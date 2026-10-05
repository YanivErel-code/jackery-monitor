"""Home/EU protocol contracts, exercised entirely through MockTransport."""

import json

import httpx
import pytest

import home_cloud_client as home
from cloud_client import CloudAuthError, SessionContestedError

SYSTEMS = [
    {
        "id": 17,
        "systemId": "17",
        "name": "Home A",
        "systemNo": "SN-A",
        "factoryModel": "JAKS-IN1K5-BA2K-EUA1",
    },
    {"systemId": "B", "systemName": "Home B", "deviceNo": "SN-B"},
]
MONITOR = {
    "systemVO": {"batteryCapacity": "4.096"},
    "energyFlowChartVO": {
        "emsGwVO": {
            "soc": "51.5",
            "energyRemain": "2.1",
            "powerPackList": [
                {"deviceSn": "bms1_SN-A", "soc": 60},
                {"deviceSn": "bms2_SN-A", "soc": 43, "power": 20},
                {"deviceSn": "unclassified", "soc": 50},
                {"soc": 30},
            ],
        },
        "pvInfo": {"pvPower": "250"},
        "gridVO": {"gridPower": -72},
        "acInfo": {"epsLoadPower": -20},
        "otherLoadVO": {"otherLoadPower": 35},
        "acMainVO": {"acMainPower": -60},
    },
}


@pytest.fixture
def fake_client(monkeypatch):
    requests = []
    replies = {}
    clock = [1000.0]
    monkeypatch.setattr(home.time, "time", lambda: clock[0])

    def handler(request):
        requests.append(request)
        assert str(request.url).startswith(home.HOME_BASE_URL + "/")
        path = request.url.path.removeprefix("/geneverse-iot-gateway")
        if path in replies:
            response = replies[path]
            if isinstance(response, Exception):
                raise response
            if isinstance(response, httpx.Response):
                return response
            return httpx.Response(200, json=response)
        if path == home.LOGIN_PATH:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "accessToken": "fake-token",
                        "userId": "fake-user",
                        "tokenPrefix": "Bearer",
                    },
                },
            )
        assert request.headers["authorization"] == "Bearer fake-token"
        if path == home.SYSTEMS_PATH:
            return httpx.Response(200, json={"success": True, "result": SYSTEMS})
        assert path == home.MONITOR_PATH
        assert request.method == "POST"
        assert json.loads(request.content)["systemId"] in ("17", "B")
        return httpx.Response(200, json={"success": True, "result": MONITOR})

    client = home.JackeryHomeCloudClient("fake@example.invalid", "fake-secret", "EU")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, requests, replies, clock


async def test_login_home_contract_and_stable_phone_identity(fake_client):
    client, requests, _, _ = fake_client
    assert await client.login() == "fake-token"
    assert client.user_id == "fake-user"
    request = requests[0]
    assert request.method == "POST"
    assert request.headers["userend"] == "HOME"
    payload = json.loads(request.content)
    assert payload == {
        "encrypted": False,
        "userEnd": "HOME",
        "userType": "2",
        "account": "fake@example.invalid",
        "password": "fake-secret",
        "phoneUid": client.phone_uid,
        "loginType": 1,
        "rememberMe": False,
        "clientType": "APP",
    }
    other = home.JackeryHomeCloudClient("fake@example.invalid", "fake-secret", "EU")
    assert other.phone_uid == client.phone_uid
    assert "fake@example.invalid" not in client.phone_uid
    await client.aclose()


async def test_system_discovery_preserves_multiple_systems_and_source_ids(fake_client):
    client, _, _, _ = fake_client
    devices = await client.fetch_devices()
    assert [(d.device_id, d.device_sn, d.name) for d in devices] == [
        ("17", "SN-A", "Home A"),
        ("B", "SN-B", "Home B"),
    ]
    assert devices[0].model_code == 0
    assert devices[0].model_name == "HomePower 2000 Ultra"
    assert client.read_only is True
    assert client.supports_realtime is False
    assert not hasattr(client, "publish_command")
    assert not hasattr(client, "set_output")
    await client.aclose()


async def test_monitor_preserves_system_scope_capacity_and_signed_flows(fake_client):
    client, requests, _, _ = fake_client
    await client.fetch_devices()
    props = await client.fetch_properties("17")
    assert props["rb"] == 51.5
    assert props["_soc_scope"] == "system"
    assert props["_installed_capacity_wh"] == 4096
    assert props["_home_solar_power_w"] == 250
    assert props["_home_grid_power_w"] == -72
    assert props["_home_ac_socket_power_w"] == -20
    assert props["_home_household_power_w"] == 35
    assert props["_home_ac_main_power_w"] == -60
    assert props["_home_energy_remaining_wh"] == 2100
    assert props["_home_battery_count"] == 4
    assert not set(props).intersection({"ip", "op", "bt", "oac", "odc", "ec"})
    assert json.loads(requests[-1].content) == {"systemId": "17"}
    await client.aclose()


async def test_pack_inventory_filters_main_and_does_not_invent_measurements(fake_client):
    client, requests, _, clock = fake_client
    await client.fetch_devices()
    await client.fetch_properties("17")
    count = len(requests)
    packs = await client.fetch_battery_packs("SN-A")
    assert len(requests) == count
    assert [p["deviceSn"] for p in packs] == ["bms2_SN-A"]
    assert packs[0]["_home_battery_role"] == "expansion"
    assert not set(packs[0]).intersection({"rb", "ip", "op", "bt", "it", "ot", "ec"})
    assert client.pack_cache_ts_by_sn["SN-A"] == clock[0]
    assert client.pack_cache_source_by_sn["SN-A"] == "http"
    assert client.pack_inventory_meta_by_sn["SN-A"] == {
        "reported_count": 4,
        "main_count": 1,
        "identified_expansion_count": 1,
        "unclassified_count": 1,
        "missing_identity_count": 1,
    }
    clock[0] += home.PACK_CACHE_TTL_S - 1
    await client.fetch_battery_packs("SN-A")
    assert len(requests) == count
    clock[0] += 1
    await client.fetch_battery_packs("SN-A")
    assert len(requests) == count + 1
    assert client.pack_cache_ts_by_sn["SN-A"] == clock[0]
    await client.fetch_battery_packs("SN-A", force_refresh=True)
    assert len(requests) == count + 2
    await client.aclose()


async def test_failed_pack_refresh_keeps_original_receipt(fake_client):
    client, _, replies, clock = fake_client
    await client.fetch_devices()
    await client.fetch_properties("17")
    original = client.pack_cache_ts_by_sn["SN-A"]
    clock[0] += home.PACK_CACHE_TTL_S
    replies[home.MONITOR_PATH] = {"success": True, "result": {}}
    with pytest.raises(CloudAuthError):
        await client.fetch_battery_packs("SN-A")
    assert client.pack_cache_ts_by_sn["SN-A"] == original
    await client.aclose()


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        "secret",
        [{"name": "no ids"}],
        [{"systemId": True, "systemNo": "SN"}],
        [{"systemId": "A"}],
    ],
)
async def test_invalid_discovery_is_not_an_empty_account(fake_client, result):
    client, _, replies, _ = fake_client
    replies[home.SYSTEMS_PATH] = {"success": True, "result": result}
    with pytest.raises(CloudAuthError):
        await client.fetch_devices()
    assert client.devices == []
    await client.aclose()


async def test_genuinely_empty_system_list(fake_client):
    client, _, replies, _ = fake_client
    replies[home.SYSTEMS_PATH] = {"success": True, "result": []}
    assert await client.fetch_devices() == []
    await client.aclose()


@pytest.mark.parametrize("value", [None, True, "NaN", "inf", -1, 101, {}, []])
async def test_invalid_soc_capacity_and_power_are_absent(fake_client, value):
    client, _, replies, _ = fake_client
    await client.fetch_devices()
    replies[home.MONITOR_PATH] = {
        "success": True,
        "result": {
            "systemVO": {"batteryCapacity": None},
            "energyFlowChartVO": {"emsGwVO": {"soc": value}, "pvInfo": {"pvPower": "NaN"}},
        },
    }
    props = await client.fetch_properties("17")
    assert "rb" not in props
    assert "_installed_capacity_wh" not in props
    assert "_home_solar_power_w" not in props
    await client.aclose()


@pytest.mark.parametrize("value", [None, True, "NaN", "inf", 0, -1, 1e308])
async def test_invalid_or_overflowing_energy_is_absent(fake_client, value):
    client, _, replies, _ = fake_client
    await client.fetch_devices()
    replies[home.MONITOR_PATH] = {
        "success": True,
        "result": {
            "systemVO": {"batteryCapacity": value},
            "energyFlowChartVO": {"emsGwVO": {"energyRemain": value}},
        },
    }
    props = await client.fetch_properties("17")
    assert "_installed_capacity_wh" not in props
    if value != 0:
        assert "_home_energy_remaining_wh" not in props
    else:
        assert props["_home_energy_remaining_wh"] == 0
    await client.aclose()


async def test_same_system_monitor_requests_cannot_complete_out_of_order(fake_client):
    import asyncio

    client, _, _, _ = fake_client
    await client.fetch_devices()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content)["systemId"])
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"success": True, "result": MONITOR})

    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = asyncio.create_task(client.fetch_properties("17"))
    await entered.wait()
    second = asyncio.create_task(client.fetch_properties("17"))
    try:
        await asyncio.sleep(0)
        assert calls == ["17"]
    finally:
        release.set()
        await asyncio.gather(first, second)
        await client.aclose()


async def test_concurrent_unauthenticated_reads_share_an_explicit_login(fake_client):
    import asyncio

    client, _, _, _ = fake_client
    entered = asyncio.Event()
    release = asyncio.Event()
    login_calls = []

    async def handler(request):
        if request.url.path.endswith(home.LOGIN_PATH):
            login_calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(
                200, json={"success": True, "result": {"accessToken": "fake-token"}}
            )
        assert request.headers["authorization"] == "Bearer fake-token"
        return httpx.Response(200, json={"success": True, "result": SYSTEMS})

    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    explicit_login = asyncio.create_task(client.login())
    await entered.wait()
    first = asyncio.create_task(client.fetch_devices())
    second = asyncio.create_task(client.fetch_devices())
    try:
        await asyncio.sleep(0)
        assert len(login_calls) == 1
    finally:
        release.set()
        await asyncio.gather(explicit_login, first, second)
        await client.aclose()


async def test_old_auth_response_cannot_erase_a_new_session(fake_client):
    import asyncio

    client, _, _, _ = fake_client
    client.token = "token-A"
    entered = asyncio.Event()
    release = asyncio.Event()
    old_calls = []

    async def handler(request):
        if request.url.path.endswith(home.LOGIN_PATH):
            return httpx.Response(200, json={"success": True, "result": {"accessToken": "token-B"}})
        if request.headers["authorization"] == "Bearer token-A":
            old_calls.append(request)
            if len(old_calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(401)
        return httpx.Response(200, json={"success": True, "result": SYSTEMS})

    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    old_request = asyncio.create_task(client.fetch_devices())
    await entered.wait()
    try:
        with pytest.raises(SessionContestedError):
            await client.fetch_devices()
        assert client.token is None
        await client.login()
        assert client.token == "token-B"
        release.set()
        with pytest.raises(SessionContestedError) as error:
            await old_request
        assert client.token == "token-B"
        assert error.value.stale_auth_response is True
    finally:
        release.set()
        await asyncio.gather(old_request, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="fake-secret fake-token"),
        httpx.Response(403, json={"msg": "fake-secret"}),
        {"success": False, "code": 401, "msg": "fake-secret"},
        {"success": False, "code": 500, "msg": "token expired fake-token"},
    ],
)
async def test_authenticated_expiry_uses_cooldown_error_without_secrets(fake_client, response):
    client, requests, replies, _ = fake_client
    await client.login()
    count = len(requests)
    replies[home.SYSTEMS_PATH] = response
    with pytest.raises(SessionContestedError) as error:
        await client.fetch_devices()
    assert client.token is None
    assert len(requests) == count + 1
    assert "fake-secret" not in str(error.value)
    assert "fake-token" not in str(error.value)
    await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="fake-secret"),
        httpx.Response(200, text="fake-secret"),
        {"success": "true", "result": "fake-secret"},
        {"success": False, "code": "fake-secret", "msg": "fake-secret"},
        ["fake-secret"],
        httpx.ConnectError("fake-secret"),
    ],
)
async def test_bad_login_response_does_not_leak_credentials(fake_client, response, caplog):
    client, _, replies, _ = fake_client
    replies[home.LOGIN_PATH] = response
    with pytest.raises(CloudAuthError) as error:
        await client.login()
    assert client.token is None
    assert "fake-secret" not in str(error.value)
    assert "fake-secret" not in caplog.text
    await client.aclose()


async def test_unknown_system_or_pack_does_not_issue_monitor_request(fake_client):
    client, requests, _, _ = fake_client
    await client.fetch_devices()
    count = len(requests)
    with pytest.raises(CloudAuthError):
        await client.fetch_properties("unknown")
    with pytest.raises(CloudAuthError):
        await client.fetch_battery_packs("unknown")
    assert len(requests) == count
    await client.aclose()


async def test_close_clears_account_data_and_cache(fake_client):
    client, _, _, _ = fake_client
    await client.fetch_devices()
    await client.fetch_properties("17")
    await client.aclose()
    assert client.token is None
    assert client.user_id is None
    assert client.devices == []
    assert client.pack_cache_by_sn == {}
    assert client.pack_cache_ts_by_sn == {}
    assert client.pack_inventory_meta_by_sn == {}


def test_region_is_explicit_and_tls_verification_is_enabled(monkeypatch):
    with pytest.raises(ValueError, match="EU"):
        home.JackeryHomeCloudClient("fake", "fake", "US")
    options = {}

    def build_http(**kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(home.httpx, "AsyncClient", build_http)
    client = home.JackeryHomeCloudClient("fake", "fake", "EU")
    import asyncio

    asyncio.run(client._client())
    assert options["verify"] is True
    assert options["follow_redirects"] is False
