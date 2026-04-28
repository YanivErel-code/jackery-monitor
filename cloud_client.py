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
        # Captured from the login response — needed for MQTT control commands.
        # mqtt_password is the base64-encoded 32-byte AES-256 key the cloud
        # gives us to derive the MQTT broker password from.
        self.user_id: Optional[str] = None
        self.mqtt_password: Optional[str] = None
        self.devices: list[CloudDevice] = []
        self._http: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._mac_id = self._generate_mac_id()
        # Lazy-initialised MQTT publisher (paho-mqtt). Connects on first
        # publish_command and stays alive until the cloud client closes.
        self._mqtt = None  # type: ignore[var-annotated]

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
        # Walk both the top level and any nested `data` dict to find token,
        # userId, mqttPassWord. The protocol doc says they're top-level but
        # the actual server has been seen putting them under a nested `data`
        # — be tolerant. Also tolerant of casing variations (mqttPassWord vs
        # mqttPassword vs mqtt_password) noted across reverse-engineered
        # write-ups.
        def _pick(d: dict, *keys: str):
            for k in keys:
                if k in d and d[k] is not None:
                    return d[k]
            return None

        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = _pick(data, "token", "Token") or _pick(nested, "token", "Token") or ""
        if not token:
            raise CloudAuthError(f"login succeeded but no token in response (keys: {sorted(data.keys())})")
        self.token = str(token)
        user_id_raw = (
            _pick(data, "userId", "userid", "user_id")
            or _pick(nested, "userId", "userid", "user_id")
        )
        self.user_id = str(user_id_raw) if user_id_raw is not None else None
        self.mqtt_password = (
            _pick(data, "mqttPassWord", "mqttPassword", "mqtt_password")
            or _pick(nested, "mqttPassWord", "mqttPassword", "mqtt_password")
        )
        # Diagnostic so we can see at a glance whether MQTT control will work
        # without dumping the actual password to the log.
        log.info(
            "Cloud login OK (token len=%d, userId=%s, mqtt_password=%s, top_keys=%s, data_keys=%s)",
            len(self.token), self.user_id,
            "set" if self.mqtt_password else "MISSING",
            sorted(data.keys()),
            sorted(nested.keys()) if nested else [],
        )
        return self.token

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

    # ---- MQTT control ----
    # Output toggles (AC/DC/USB/Car/etc.) go over MQTT, NOT the HTTP API.
    # Broker:   emqx.jackeryapp.com:8883 (TLS 1.2)
    # Topic:    hb/app/{userId}/command  (QoS 1)
    # Auth:     username = "{userId}@{macId}"
    #           password = base64(AES-256-CBC(username, key=b64decode(mqttPassWord), iv=key[:16]))
    # Action IDs: AC=4 DC=1 USB=2 Car=3 (body: {<property>: 0|1})
    # Reverse-engineered protocol doc: github.com/jlopez/socketry/docs/protocol.md
    BROKER_HOST = "emqx.jackeryapp.com"
    BROKER_PORT = 8883
    PORT_TO_ACTION: dict[str, tuple[int, str]] = {
        "ac":  (4, "oac"),
        "dc":  (1, "odc"),
        "usb": (2, "odcu"),
        "car": (3, "odcc"),
    }

    def _mqtt_password(self) -> tuple[str, str]:
        """Return (username, password) for the MQTT broker."""
        if not (self.user_id and self.mqtt_password):
            raise CloudAuthError("MQTT credentials missing — login first")
        username = f"{self.user_id}@{self._mac_id}"
        key = base64.b64decode(self.mqtt_password)
        if len(key) != 32:
            raise CloudAuthError(f"unexpected mqttPassWord length: {len(key)}")
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        ct = cipher.encrypt(pad(username.encode("utf-8"), AES.block_size))
        return username, base64.b64encode(ct).decode()

    async def _ensure_mqtt(self):
        """Lazy-connect on first command. Reconnects automatically thereafter."""
        if self._mqtt is not None and self._mqtt.is_connected():
            return self._mqtt
        # Lazy import so users without paho-mqtt installed aren't blocked at boot.
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ImportError as e:
            raise CloudAuthError(f"paho-mqtt not installed: {e}")

        username, password = self._mqtt_password()
        client_id = f"{self.user_id}@APP"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(username, password)
        # The Jackery broker uses a self-signed CA (`ca.jackery.com`) bundled
        # in the iOS app. We don't have the cert, so we keep TLS encryption
        # on but skip cert verification. Acceptable since the host is fixed
        # and the auth is per-user. To pin properly later, add the cert to
        # the repo and pass it via `client.tls_set(ca_certs=...)`.
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)

        # paho's connect is synchronous; run it in the default executor so we
        # don't block the asyncio loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, client.connect,
                                   self.BROKER_HOST, self.BROKER_PORT, 30)
        client.loop_start()  # background network thread
        self._mqtt = client
        log.info("MQTT connected to %s:%d as %s", self.BROKER_HOST, self.BROKER_PORT, client_id)
        return client

    async def publish_command(self, device_sn: str, port: str, on: bool,
                              timeout_s: float = 5.0) -> dict:
        """Send an output toggle command. Returns broker ack info."""
        port = (port or "").lower()
        if port not in self.PORT_TO_ACTION:
            raise CloudAuthError(f"unknown output port: {port!r}")
        if not device_sn:
            raise CloudAuthError("device_sn is required")

        action_id, prop_key = self.PORT_TO_ACTION[port]
        client = await self._ensure_mqtt()
        ts_ms = int(time.time() * 1000)
        payload = {
            "deviceSn": device_sn,
            "id": ts_ms,
            "version": 0,
            "messageType": "DevicePropertyChange",
            "actionId": action_id,
            "timestamp": ts_ms,
            "body": {prop_key: 1 if on else 0},
        }
        topic = f"hb/app/{self.user_id}/command"

        loop = asyncio.get_running_loop()
        msg_info = await loop.run_in_executor(
            None, lambda: client.publish(topic, json.dumps(payload), qos=1)
        )
        # Wait for the broker PUBACK so we know it accepted the command. The
        # device's actual property change comes back on a different topic and
        # will be picked up by the next /device/property poll — we don't wait
        # on it here.
        await loop.run_in_executor(None, lambda: msg_info.wait_for_publish(timeout_s))
        if not msg_info.is_published():
            raise CloudAuthError(f"MQTT publish timeout after {timeout_s}s")
        log.info("MQTT publish %s -> %s=%d (action %d)", port, prop_key,
                 1 if on else 0, action_id)
        return {"port": port, "on": bool(on), "action_id": action_id, "topic": topic}

    async def aclose(self) -> None:
        if self._mqtt is not None:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
            finally:
                self._mqtt = None
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
    # Per the reverse-engineered protocol (jlopez/socketry/docs/protocol.md):
    #   it / ot  are both decihours (raw 22 → 2.2h, 999 → 99.9h sentinel)
    #   acip     is the AC (grid) input power in watts
    #   cip      is the car/12V input power in watts
    #   ip       is the *total* input — solar = ip - acip - cip
    grid_w = i("acip")
    car_in_w = i("cip")
    total_in_w = i("ip")
    solar_w = max(0, total_in_w - grid_w - car_in_w)
    # 99.9h (raw 999) is the protocol's "not applicable" sentinel; treat as 0.
    raw_it = i("it")
    raw_ot = i("ot")
    return {
        "battery_percent": i("rb"),
        "battery_temp_c": round(f("bt") / 10.0, 1),
        "input_power_w": total_in_w,
        "output_power_w": i("op"),
        "ac_input_w": grid_w,           # grid
        "car_input_w": car_in_w,        # 12V cigarette
        "solar_input_w": solar_w,       # everything else on DC bus
        "ac_output_v": round(acov / 10.0, 1) if acov else 0.0,
        "ac_output_hz": round(acohz, 1) if acohz else 0.0,
        "ac_on": bool(i("oac")),
        "dc_on": bool(i("odc")),
        "usb_on": bool(i("odcu")),
        "car_on": bool(i("odcc")),
        "ups_on": bool(i("ups", 1)),
        "super_charge_on": bool(i("sfc")),
        "error_code": i("ec"),
        "time_to_full_h":   0.0 if raw_it in (0, 999) else round(raw_it / 10.0, 2),
        "time_remaining_h": 0.0 if raw_ot in (0, 999) else round(raw_ot / 10.0, 2),
    }
