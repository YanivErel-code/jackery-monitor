"""
Pluggable device-client layer.

Two modes, picked at runtime via environment variables:

  BACKEND=mock       -> generated fake telemetry, no hardware
  BACKEND=bridge     -> talks to a JSON-RPC bridge process over TCP (the bridge proxies the Jackery cloud)

The interface (`DeviceClient`) is async and tiny on purpose: connect, poll, set output, disconnect.
The FastAPI server only knows about this interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("device_client")

# ----------- common types -----------
@dataclass
class DeviceInfo:
    name: str
    address: str
    rssi: int
    model_code: Optional[int]
    device_sn: Optional[str]
    device_type: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "rssi": self.rssi,
            "model_code": self.model_code,
            "device_sn": self.device_sn,
            "device_type": self.device_type,
        }


class DeviceClientError(RuntimeError):
    pass


# ----------- abstract base -----------
class DeviceClient:
    backend_name = "abstract"

    async def connect(self) -> tuple[bool, Optional[DeviceInfo], Optional[str]]:
        """Return (ok, device_info, error)."""
        raise NotImplementedError

    async def poll(self) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def set_output(self, port: str, on: bool) -> None:
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        raise NotImplementedError


# ----------- mock -----------
class MockDeviceClient(DeviceClient):
    backend_name = "mock"

    def __init__(self) -> None:
        self._connected = False
        self._t0 = time.time()
        self._battery = 78.0
        self._switches = {"ac": True, "dc": False, "usb": True, "car": False}

    async def connect(self):
        await asyncio.sleep(0.4)
        self._connected = True
        info = DeviceInfo(
            name="EXPLORER-5000P-MOCK",
            address="AA:BB:CC:DD:EE:FF",
            rssi=-55,
            model_code=13,
            device_sn="MOCK0000000000A",
            device_type="portable",
        )
        return True, info, None

    async def poll(self):
        if not self._connected:
            return None
        self._battery = max(5, min(100, self._battery + random.uniform(-0.4, 0.3)))
        out = max(0, int(380 + 90 * random.random())) if self._switches["ac"] else 0
        in_w = max(0, int(120 + 60 * random.random())) if random.random() > 0.4 else 0
        return {
            "battery_percent": int(self._battery),
            "battery_temp_c": round(24.0 + random.uniform(-0.5, 0.5), 1),
            "input_power_w": in_w,
            "output_power_w": out,
            "ac_input_w": in_w,
            "ac_output_v": 120.0,
            "ac_output_hz": 60.0,
            "ac_on": self._switches["ac"],
            "dc_on": self._switches["dc"],
            "usb_on": self._switches["usb"],
            "car_on": self._switches["car"],
            "ups_on": True,
            "super_charge_on": False,
            "error_code": 0,
            "time_to_full_h": 0.0 if in_w == 0 else 2.5,
            "time_remaining_h": round((self._battery / 100) * 12, 2),
        }

    async def set_output(self, port, on):
        if port not in self._switches:
            raise DeviceClientError(f"unknown port {port}")
        self._switches[port] = bool(on)

    async def disconnect(self):
        self._connected = False

    @property
    def is_connected(self):
        return self._connected


# ----------- bridge (talks to bridge.py over TCP JSON-RPC) -----------
class BridgeDeviceClient(DeviceClient):
    """Talks to bridge.py running on the host. Used inside Docker on macOS."""
    backend_name = "bridge"

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._connected = False
        self._device: Optional[DeviceInfo] = None
        self._lock = asyncio.Lock()
        self._last_status: Optional[dict] = None

    async def _rpc(self, method: str, **params) -> dict:
        """One-shot JSON-RPC call: open socket, send line, read line, close."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5.0,
                )
            except (OSError, asyncio.TimeoutError) as e:
                raise DeviceClientError(
                    f"Cannot reach BLE bridge at {self.host}:{self.port}. "
                    f"Is bridge.py running on the host? ({e})"
                )
            try:
                request = json.dumps({"method": method, "params": params}) + "\n"
                writer.write(request.encode())
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=20.0)
                if not line:
                    raise DeviceClientError("Bridge closed connection")
                resp = json.loads(line.decode())
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        if "error" in resp and resp["error"]:
            raise DeviceClientError(resp["error"])
        return resp.get("result", {})

    async def connect(self):
        # Tell the bridge to (re)kick its BLE scan; cloud autoconnects on its own.
        try:
            await self._rpc("connect")
        except DeviceClientError as e:
            log.warning("Bridge connect rpc failed: %s", e)
        # Also fetch initial status so we report a sensible device/state.
        try:
            r = await self._rpc("status")
        except DeviceClientError as e:
            return False, None, str(e)
        d = r.get("device") or {}
        if not d:
            ble_state = (r.get("ble") or {}).get("state", "?")
            cloud_state = (r.get("cloud") or {}).get("state", "?")
            cloud_err = (r.get("cloud") or {}).get("error")
            msg = f"Bridge has no device yet (BLE: {ble_state}"
            if cloud_state != "disabled":
                msg += f", Cloud: {cloud_state}"
                if cloud_err:
                    msg += f" ({cloud_err})"
            msg += ")"
            self._connected = True   # bridge itself is reachable
            return True, None, msg
        info = DeviceInfo(
            name=d.get("name", "?"),
            address=d.get("address", "?"),
            rssi=d.get("rssi", -100),
            model_code=d.get("model_code"),
            device_sn=d.get("device_sn"),
            device_type=d.get("device_type", "portable"),
        )
        self._device = info
        self._connected = True
        self._last_status = r
        return True, info, None

    async def poll(self):
        try:
            r = await self._rpc("poll")
        except DeviceClientError as e:
            log.warning("Bridge poll failed: %s", e)
            return None
        self._last_status = r
        # Also pull the device dict (cloud may have arrived after BLE failed)
        d = r.get("device")
        if d and (not self._device or self._device.address != d.get("address")):
            self._device = DeviceInfo(
                name=d.get("name", "?"),
                address=d.get("address", "?"),
                rssi=d.get("rssi", -100),
                model_code=d.get("model_code"),
                device_sn=d.get("device_sn"),
                device_type=d.get("device_type", "portable"),
            )
        t = r.get("telemetry")
        if t is not None:
            t = dict(t)
            t["_source"] = r.get("source")
            t["_ble"] = r.get("ble")
            t["_cloud"] = r.get("cloud")
        return t

    @property
    def device_info(self):
        return self._device

    @property
    def last_status(self):
        return getattr(self, "_last_status", None)

    async def set_output(self, port, on):
        r = await self._rpc("set_output", port=port, on=bool(on))
        if not r.get("ok"):
            raise DeviceClientError(r.get("error", "set_output failed"))

    async def select_device(self, device_id: str) -> dict:
        """Switch which cloud device the bridge polls. Returns {ok, device_id, name}."""
        r = await self._rpc("select_device", device_id=device_id)
        if not r.get("ok"):
            raise DeviceClientError(r.get("error", "select_device failed"))
        # Reset our cached DeviceInfo so the next poll picks up the new device
        self._device = None
        return r

    async def auth_status(self) -> dict:
        """Whether the bridge has cloud credentials, and current cloud state."""
        return await self._rpc("auth_status")

    async def set_credentials(self, email: str, password: str, region: str = "US") -> dict:
        """Validate + persist Jackery cloud creds in the host keychain, restart cloud poller."""
        r = await self._rpc("set_credentials", email=email, password=password, region=region)
        if not r.get("ok"):
            raise DeviceClientError(r.get("error", "set_credentials failed"))
        # Reset our cached DeviceInfo so the next poll picks up the new device
        self._device = None
        return r

    async def disconnect(self):
        try:
            await self._rpc("disconnect")
        finally:
            self._connected = False

    @property
    def is_connected(self):
        return self._connected


# ----------- factory -----------
def make_client() -> DeviceClient:
    backend = os.environ.get("BACKEND", "").lower().strip()
    bridge_url = os.environ.get("BRIDGE_URL", "").strip()

    if not backend:
        if os.environ.get("JACKERY_MOCK") == "1":
            backend = "mock"
        else:
            backend = "bridge"

    if backend == "mock":
        log.info("Backend: MOCK (synthetic telemetry)")
        return MockDeviceClient()

    if backend == "bridge":
        host, port = _parse_bridge_url(bridge_url or "host.docker.internal:8766")
        log.info("Backend: BRIDGE -> %s:%s", host, port)
        return BridgeDeviceClient(host, port)

    raise SystemExit(f"Unknown BACKEND={backend!r} (use mock|bridge)")


def _parse_bridge_url(url: str) -> tuple[str, int]:
    if "://" in url:
        url = url.split("://", 1)[1]
    if ":" not in url:
        return url, 8766
    host, port = url.rsplit(":", 1)
    return host, int(port)
