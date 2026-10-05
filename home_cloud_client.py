"""Read-only Jackery Home/EU HTTP monitoring, explicitly selected by the user.

Protocol reference: https://github.com/iLLixM/jackery_home_cloud-ha, commit
e17099aa2a11f6497bfef42eb0fc002754b5f348. REST monitor SOC and capacity describe
the complete system. Grid, household and socket power have different boundaries
from portable battery input/output power, so they retain separate signed fields.
All timestamps below are local HTTP receipt times, not hardware sample times.

The reference's request constants and protocol are used under its MIT license:
Copyright (c) 2026 iLLixM
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from typing import Any

import httpx

from cloud_client import PACK_CACHE_TTL_S, CloudAuthError, CloudDevice, SessionContestedError

HOME_BASE_URL = "https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway"
LOGIN_PATH = "/geneverse-iot-home/v1/home/auth/login"
SYSTEMS_PATH = "/geneverse-iot-home/v1/system/listByUserV2"
MONITOR_PATH = "/geneverse-iot-home/v1/app/monitor/"
HOME_HEADERS = {
    "user-agent": "Dart/3.11 (dart:io)",
    "accept-language": "en-US",
    "model": "Phone",
    "accept-encoding": "gzip",
    "x-app-name": "Custom-Phone",
    "content-type": "application/json;charset=UTF-8",
    "x-app-version": "home_android_v2.10.22",
    "sdkint": "34",
    "id": "UP1A.231105.003.A1",
    "userend": "HOME",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value).strip()
    return result if result and not any(ord(c) < 32 for c in result) else None


class JackeryHomeCloudClient:
    """Separate protocol client: no portable inheritance, MQTT or controls."""

    read_only = True
    supports_realtime = False

    def __init__(self, email: str, password: str, region: str = "EU") -> None:
        if region.upper() != "EU":
            raise ValueError("Jackery Home monitoring currently supports the EU region only")
        self.email = email
        self.password = password
        self.region = "EU"
        self.phone_uid = "jackery-monitor-" + hashlib.sha256(email.encode()).hexdigest()[:24]
        self.token: str | None = None
        self.user_id: str | None = None
        self.devices: list[CloudDevice] = []
        self._token_prefix = "Bearer"
        self._http: httpx.AsyncClient | None = None
        self._auth_lock = asyncio.Lock()
        self._systems_by_id: dict[str, dict] = {}
        self._monitor_locks: dict[str, asyncio.Lock] = {}
        self.monitor_receipt_ts_by_sn: dict[str, float] = {}
        self.pack_cache_by_sn: dict[str, list[dict[str, Any]]] = {}
        self.pack_cache_ts_by_sn: dict[str, float] = {}
        self.pack_cache_source_by_sn: dict[str, str] = {}
        self.pack_cache_revision_by_sn: dict[str, int] = {}
        self.pack_inventory_by_sn: dict[str, list] = {}
        self.pack_inventory_meta_by_sn: dict[str, dict[str, int]] = {}

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30.0, headers=HOME_HEADERS, verify=True, follow_redirects=False
            )
        return self._http

    async def _request(
        self, method: str, path: str, *, body: dict | None = None, authenticated: bool = True
    ) -> Any:
        if authenticated and not self.token:
            await self.login()
        request_token = self.token
        headers = dict(HOME_HEADERS)
        if authenticated:
            headers["authorization"] = f"{self._token_prefix} {self.token}"
        client = await self._client()
        try:
            response = await client.request(
                method, HOME_BASE_URL + path, headers=headers, json=body
            )
        except httpx.HTTPError:
            # Exceptions can contain URLs, server messages or echoed credentials.
            raise CloudAuthError("Home cloud connection failed") from None
        if response.status_code in (401, 403):
            if authenticated:
                raise self._auth_failure("Home cloud rejected authentication", request_token)
            raise CloudAuthError("Home cloud rejected authentication")
        if response.status_code != 200:
            raise CloudAuthError(f"Home cloud HTTP {response.status_code}")
        try:
            envelope = response.json()
        except ValueError:
            raise CloudAuthError("Home cloud returned invalid JSON") from None
        if not isinstance(envelope, dict):
            raise CloudAuthError("Home cloud returned an invalid response envelope")
        if envelope.get("success") is not True:
            message = envelope.get("msg")
            auth_error = envelope.get("code") in (401, 403) or (
                isinstance(message, str)
                and any(
                    marker in message.lower()
                    for marker in ("token", "login", "auth", "expired", "unauthorized")
                )
            )
            if authenticated and auth_error:
                raise self._auth_failure("Home cloud session is no longer valid", request_token)
            raise CloudAuthError("Home cloud request was rejected")
        if "result" not in envelope or envelope["result"] is None:
            raise CloudAuthError("Home cloud response is missing its result")
        return envelope["result"]

    def _auth_failure(self, message: str, request_token: str | None) -> SessionContestedError:
        # A delayed response for session A must not erase a newer login B.
        stale = self.token is not None and self.token != request_token
        if not stale:
            self.token = None
        error = SessionContestedError(message)
        error.stale_auth_response = stale
        return error

    async def login(self) -> str:
        # Polling and pack reads share one session. Concurrent readers must
        # not perform multiple logins that could invalidate one another.
        async with self._auth_lock:
            if self.token:
                return self.token
            return await self._login()

    async def _login(self) -> str:
        self.token = None
        result = await self._request(
            "POST",
            LOGIN_PATH,
            authenticated=False,
            body={
                "encrypted": False,
                "userEnd": "HOME",
                "userType": "2",
                "account": self.email,
                "password": self.password,
                "phoneUid": self.phone_uid,
                "loginType": 1,
                "rememberMe": False,
                "clientType": "APP",
            },
        )
        if not isinstance(result, dict):
            raise CloudAuthError("Home login returned an invalid result")
        token = result.get("accessToken")
        prefix = result.get("tokenPrefix", "Bearer")
        if (
            not isinstance(token, str)
            or not token.strip()
            or any(ord(c) < 32 for c in token)
            or not isinstance(prefix, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", prefix)
        ):
            raise CloudAuthError("Home login returned invalid token metadata")
        self.token = token
        self._token_prefix = prefix
        self.user_id = _identifier(result.get("userId"))
        return token

    async def fetch_devices(self) -> list[CloudDevice]:
        systems = await self._request("GET", SYSTEMS_PATH)
        if not isinstance(systems, list):
            raise CloudAuthError("Home system discovery returned an invalid list")
        devices = []
        sources = {}
        seen_serials = set()
        for system in systems:
            if not isinstance(system, dict):
                raise CloudAuthError("Home system discovery returned an invalid row")
            system_id = _identifier(system.get("systemId")) or _identifier(system.get("id"))
            serial = _identifier(system.get("systemNo")) or _identifier(system.get("deviceNo"))
            if not system_id or not serial or system_id in sources or serial in seen_serials:
                raise CloudAuthError("Home system discovery lacks unique system/device identifiers")
            factory_model = system.get("factoryModel")
            model = (
                "HomePower 2000 Ultra"
                if factory_model == "JAKS-IN1K5-BA2K-EUA1"
                else _identifier(factory_model) or "Jackery Home system"
            )
            name = _identifier(system.get("name")) or _identifier(system.get("systemName")) or model
            devices.append(CloudDevice(system_id, name, 0, model, serial))
            sources[system_id] = dict(system)
            seen_serials.add(serial)
        self.devices = devices
        self._systems_by_id = sources
        return devices

    async def fetch_properties(self, device_id: str) -> dict[str, Any]:
        device = next((d for d in self.devices if d.device_id == device_id), None)
        if device is None:
            raise CloudAuthError("Unknown Home system identifier")
        # A pack refresh and normal polling can overlap. Serialize only the
        # same system so an older response cannot overwrite a newer snapshot.
        lock = self._monitor_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            return await self._fetch_properties(device)

    async def _fetch_properties(self, device: CloudDevice) -> dict[str, Any]:
        device_id = device.device_id
        monitor = await self._request("POST", MONITOR_PATH, body={"systemId": device_id})
        if not isinstance(monitor, dict):
            raise CloudAuthError("Home monitor returned an invalid result")
        flow = monitor.get("energyFlowChartVO")
        if not isinstance(flow, dict) or not isinstance(flow.get("emsGwVO"), dict):
            raise CloudAuthError("Home monitor is missing its energy-flow snapshot")
        ems = flow["emsGwVO"]
        props: dict[str, Any] = {"_soc_scope": "system"}
        soc = _number(ems.get("soc"))
        if soc is not None and 0 <= soc <= 100:
            props["rb"] = soc
        system = monitor.get("systemVO")
        if system is not None and not isinstance(system, dict):
            raise CloudAuthError("Home monitor returned invalid system metadata")
        capacity = _number((system or {}).get("batteryCapacity"))
        if capacity is not None and capacity > 0 and math.isfinite(capacity * 1000):
            props["_installed_capacity_wh"] = capacity * 1000
        remaining = _number(ems.get("energyRemain"))
        if remaining is not None and remaining >= 0 and math.isfinite(remaining * 1000):
            props["_home_energy_remaining_wh"] = remaining * 1000
        for source, field, target in (
            ("pvInfo", "pvPower", "_home_solar_power_w"),
            ("gridVO", "gridPower", "_home_grid_power_w"),
            ("acInfo", "epsLoadPower", "_home_ac_socket_power_w"),
            ("otherLoadVO", "otherLoadPower", "_home_household_power_w"),
            ("acMainVO", "acMainPower", "_home_ac_main_power_w"),
        ):
            section = flow.get(source)
            value = _number(section.get(field)) if isinstance(section, dict) else None
            if value is not None and (target != "_home_solar_power_w" or value >= 0):
                props[target] = value
        inventory = ems.get("powerPackList")
        if inventory is not None and not isinstance(inventory, list):
            raise CloudAuthError("Home monitor returned an invalid battery inventory")
        received_at = time.time()
        self.monitor_receipt_ts_by_sn[device.device_sn] = received_at
        if isinstance(inventory, list):
            props["_home_battery_count"] = len(inventory)
            self._cache_pack_inventory(device.device_sn, inventory, received_at)
        return props

    def _cache_pack_inventory(self, device_sn: str, inventory: list, received_at: float) -> None:
        packs = []
        meta = {
            "reported_count": len(inventory),
            "main_count": 0,
            "identified_expansion_count": 0,
            "unclassified_count": 0,
            "missing_identity_count": 0,
        }
        seen = set()
        for row in inventory:
            serial = None
            if isinstance(row, dict):
                serial = (
                    _identifier(row.get("deviceSn"))
                    or _identifier(row.get("deviceNo"))
                    or _identifier(row.get("dev_sn"))
                )
            if not serial:
                meta["missing_identity_count"] += 1
                continue
            match = re.fullmatch(r"bms([1-9][0-9]*)_.+", serial)
            if serial == device_sn or (match and int(match[1]) == 1):
                meta["main_count"] += 1
                continue
            if not match or serial in seen:
                meta["unclassified_count"] += 1
                continue
            seen.add(serial)
            meta["identified_expansion_count"] += 1
            # The reference documents bms1 vs bms2+ roles, but no REST row
            # schema/units for individual SOC, temperature or power. Retain
            # identity alone; never fabricate portable battery readings.
            packs.append(
                {
                    "deviceSn": serial,
                    "parentDeviceSn": device_sn,
                    "deviceOrder": int(match[1]) - 1,
                    "_home_battery_role": "expansion",
                }
            )
        packs.sort(key=lambda p: p["deviceOrder"])
        self.pack_inventory_by_sn[device_sn] = list(inventory)
        self.pack_inventory_meta_by_sn[device_sn] = meta
        self.pack_cache_by_sn[device_sn] = packs
        self.pack_cache_ts_by_sn[device_sn] = received_at
        self.pack_cache_source_by_sn[device_sn] = "http"
        self.pack_cache_revision_by_sn[device_sn] = (
            self.pack_cache_revision_by_sn.get(device_sn, 0) + 1
        )

    async def fetch_battery_packs(
        self, device_sn: str, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        cached = self.pack_cache_by_sn.get(device_sn)
        receipt = self.pack_cache_ts_by_sn.get(device_sn)
        if (
            not force_refresh
            and cached is not None
            and receipt is not None
            and 0 <= time.time() - receipt < PACK_CACHE_TTL_S
        ):
            return cached
        device = next((d for d in self.devices if d.device_sn == device_sn), None)
        if device is None:
            raise CloudAuthError("Unknown Home device identifier")
        revision = self.pack_cache_revision_by_sn.get(device_sn, 0)
        await self.fetch_properties(device.device_id)
        if self.pack_cache_revision_by_sn.get(device_sn, 0) == revision:
            raise CloudAuthError("Home monitor did not report its battery inventory")
        return self.pack_cache_by_sn[device_sn]

    async def aclose(self) -> None:
        client, self._http = self._http, None
        self.token = None
        self.user_id = None
        self.devices.clear()
        self._systems_by_id.clear()
        self._monitor_locks.clear()
        self.monitor_receipt_ts_by_sn.clear()
        self.pack_cache_by_sn.clear()
        self.pack_cache_ts_by_sn.clear()
        self.pack_cache_source_by_sn.clear()
        self.pack_cache_revision_by_sn.clear()
        self.pack_inventory_by_sn.clear()
        self.pack_inventory_meta_by_sn.clear()
        if client is not None:
            await client.aclose()
