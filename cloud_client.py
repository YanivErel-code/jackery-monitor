"""
Jackery cloud API client (US region).

Reverse-engineered from:
  - https://qiita.com/Hsky16/items/c163137265a87186ac39
  - https://note.com/kotobuki157/n/n4b977c03f88b
  - https://github.com/theak/jackery-homeassistant

Auth flow:
  1. POST /v1/auth/login   query string carries:
       aesEncryptData = AES-ECB-PKCS7(plaintext=login_json, key=fixed 16-byte key)
       rsaForAesKey   = RSA-PKCS1v15(aes_key, public_key)
     body is a multipart/form-data with an empty 'file' field (Alamofire quirk).
     response: { code: 0, msg: "SUCCESS", token: "<jwt>" }
  2. GET  /v1/device/list                                   -> list of devices
  3. GET  /v1/device/property?deviceId=<id>                 -> properties dict

Token expires periodically (~24h); we re-login transparently on 'token expired' codes.

The properties dict shape matches BLE (rb, bt, ip, op, acip, acov, acohz, oac, odc, odcu, odcc, ec, ot, it, ...),
so we reuse the same _portable_status_to_dict adapter from device_client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import pad

log = logging.getLogger("cloud_client")

BASE_URL = "https://iot.jackeryapp.com"
LOGIN_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCVmzgJy/4XolxPnkfu32YtJqYG"
    "FLYqf9/rnVgURJED+8J9J3Pccd6+9L97/+7COZE5OkejsgOkqeLNC9C3r5mhpE4z"
    "k/HStss7Q8/5DqkGD1annQ+eoICo3oi0dITZ0Qll56Dowb8lXi6WHViVDdih/oeU"
    "wVJY89uJNtTWrz7t7QIDAQAB"
)
AES_KEY = b"1234567890123456"  # fixed key per reverse-engineered protocol

DEFAULT_HEADERS = {
    "accept": "*/*",
    "app_version": "1.0.5",
    "sys_version": "17.2",
    "platform": "1",  # 1 = iOS
    "accept-language": "en-US",
    "accept-encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
    "user-agent": "DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
    "model": "iPad Pro (12.9-inch) (3rd generation)",
}

CLOUD_POLL_INTERVAL_S = 60


@dataclass
class CloudDevice:
    device_id: str
    name: str
    model_code: int
    model_name: str
    device_sn: str


class CloudAuthError(RuntimeError):
    pass


class SessionContestedError(CloudAuthError):
    """Raised when the cloud rejects our token (401/1001/1002).

    This usually means another device (e.g. the official Jackery iOS app)
    just signed in on the same account and invalidated our session. We
    deliberately don't auto-relogin here — that creates a token war that
    keeps booting the user out of the phone app. The caller decides whether
    to back off or reclaim immediately.
    """
    pass


class JackeryCloudClient:
    """Async, single-account Jackery cloud client. Auto-relogins on token expiry."""

    def __init__(self, email: str, password: str, region: str = "US",
                 android_id: str = "abcd1234567890ef") -> None:
        self.email = email
        self.password = password
        self.region = region
        self.android_id = android_id
        self.token: Optional[str] = None
        self.devices: list[CloudDevice] = []
        self._http: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._mac_id = self._generate_mac_id()

    # ---- internals ----
    def _generate_mac_id(self) -> str:
        # Match the reference Android UDID derivation (Hsky16 / theak/jackery-homeassistant):
        #   prefix "2" + md5-uuidv3(android_id) when android_id is valid,
        #   prefix "9" + random uuid otherwise.
        # Using the documented default ("abcd1234567890ef") matches the working HA flow.
        if self.android_id and self.android_id != "9774d56d682e549c":
            md5 = hashlib.md5(self.android_id.encode("utf-8")).digest()
            u = uuid.UUID(bytes=md5, version=3)
            return "2" + str(u).replace("-", "")
        random_uuid_str = str(uuid.uuid4()).replace("-", "")
        return "9" + random_uuid_str

    @staticmethod
    def _aes_encrypt(plaintext: str) -> str:
        cipher = AES.new(AES_KEY, AES.MODE_ECB)
        ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
        return base64.b64encode(ct).decode()

    @staticmethod
    def _rsa_encrypt(data: bytes) -> str:
        pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            + LOGIN_PUBLIC_KEY_B64
            + "\n-----END PUBLIC KEY-----"
        )
        pub = RSA.importKey(pem)
        cipher = PKCS1_v1_5.new(pub)
        return base64.b64encode(cipher.encrypt(data)).decode()

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0, base_url=BASE_URL,
                                           headers=DEFAULT_HEADERS)
        return self._http

    # ---- public API ----
    async def login(self) -> str:
        login_bean = {
            "account": self.email,
            "loginType": 2,             # password
            "macId": self._mac_id,
            "password": self.password,
            "phone": "",
            "registerAppId": "com.hbxn.jackery",
            "verificationCode": "",
        }
        aes_payload = self._aes_encrypt(json.dumps(login_bean, ensure_ascii=False))
        rsa_key = self._rsa_encrypt(AES_KEY)

        client = await self._client()
        resp = await client.post(
            "/v1/auth/login",
            params={"aesEncryptData": aes_payload, "rsaForAesKey": rsa_key},
            # Empty multipart file matches the reference packet capture exactly
            # (Alamofire quirk in the iOS app).
            files={"file": ("", b"", "")},
        )
        if resp.status_code != 200:
            raise CloudAuthError(f"login HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("code") != 0:
            raise CloudAuthError(f"login failed: {data.get('msg') or data}")
        token = data.get("token") or ""
        if not token:
            raise CloudAuthError("login succeeded but no token in response")
        self.token = token
        log.info("Cloud login OK (token len=%d)", len(token))
        return token

    @staticmethod
    def _is_token_expired(data: dict) -> bool:
        # observed expired-token codes: 401, 1001, "token" in msg
        if not isinstance(data, dict):
            return False
        code = data.get("code")
        if code in (401, 1001, 1002):
            return True
        msg = (data.get("msg") or "").lower()
        return "token" in msg and ("expir" in msg or "invalid" in msg or "auth" in msg)

    async def _authed_get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.token:
            await self.login()
        client = await self._client()
        resp = await client.get(
            path, params=params or {},
            headers={"token": self.token or "", "content-type": "application/json"},
        )
        if resp.status_code != 200:
            raise CloudAuthError(f"{path} HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if self._is_token_expired(data):
            # Drop the cached token so the next intentional login() is fresh,
            # but don't re-login automatically — that would steal the session
            # back from whoever just claimed it (typically the iOS app).
            self.token = None
            raise SessionContestedError(
                f"{path}: cloud rejected token ({data.get('code')}: {data.get('msg')})"
            )
        return data

    async def fetch_devices(self) -> list[CloudDevice]:
        # The legacy Jackery app uses /v1/device/bind/list. Response shape:
        #   {code:0, data:[{devId, devSn, devModel, devName, devNickname,
        #                   modelCode, region, devState, ...}, ...]}
        data = await self._authed_get("/v1/device/bind/list")
        if data.get("code") != 0:
            raise CloudAuthError(f"device list failed: {data.get('msg') or data}")
        raw = data.get("data")
        if isinstance(raw, dict):
            items = raw.get("list") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        out: list[CloudDevice] = []
        for d in items:
            if not isinstance(d, dict):
                continue
            out.append(CloudDevice(
                device_id=str(d.get("devId") or d.get("id") or ""),
                name=str(d.get("devNickname") or d.get("devName")
                         or d.get("deviceName") or d.get("modelName") or "?"),
                model_code=int(d.get("modelCode") or 0),
                model_name=str(d.get("devModel") or d.get("modelName") or ""),
                device_sn=str(d.get("devSn") or d.get("deviceCode") or d.get("sn") or ""),
            ))
        self.devices = [d for d in out if d.device_id]
        log.info("Cloud devices: %s", [(d.name, d.model_code) for d in self.devices])
        return self.devices

    async def fetch_properties(self, device_id: str) -> dict[str, Any]:
        # Response shape: {code:0, data:{device:{...}, properties:{rb,bt,ip,op,...}}}
        data = await self._authed_get("/v1/device/property", {"deviceId": device_id})
        if data.get("code") != 0:
            raise CloudAuthError(f"property fetch failed: {data.get('msg') or data}")
        d = data.get("data") or {}
        props = d.get("properties") if isinstance(d, dict) else None
        return props or {}

    async def aclose(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None


# ---- adapt cloud properties dict -> our common telemetry shape -----------
def cloud_props_to_telemetry(p: dict[str, Any]) -> dict[str, Any]:
    """Map raw cloud-properties dict into the same shape device_client emits."""
    def f(key: str, default: float = 0) -> float:
        v = p.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def i(key: str, default: int = 0) -> int:
        v = p.get(key)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    acov = f("acov")
    acohz = f("acohz")
    # Cloud sends acov in deci-volts (e.g. 2401 -> 240.1V) but acohz already
    # in whole Hz (e.g. 60 -> 60Hz). BLE protocol uses deci-Hz; cloud differs.
    return {
        "battery_percent": i("rb"),
        "battery_temp_c": round(f("bt") / 10.0, 1),
        "input_power_w": i("ip"),
        "output_power_w": i("op"),
        "ac_input_w": i("acip"),
        "ac_output_v": round(acov / 10.0, 1) if acov else 0.0,
        "ac_output_hz": round(acohz, 1) if acohz else 0.0,
        "ac_on": bool(i("oac")),
        "dc_on": bool(i("odc")),
        "usb_on": bool(i("odcu")),
        "car_on": bool(i("odcc")),
        "ups_on": bool(i("ups", 1)),
        "super_charge_on": bool(i("sfc")),
        "error_code": i("ec"),
        "time_to_full_h": round(f("it") / 100.0, 2) if f("it") else 0.0,
        "time_remaining_h": round(f("ot") / 10.0, 2) if f("ot") else 0.0,
    }
