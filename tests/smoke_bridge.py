"""
End-to-end smoke test: bridge.py + cloud_client.py + BridgeDeviceClient.

Mocks:
  * Jackery cloud HTTP server (in-process, like smoke_cloud.py)
  * BLE backend (we monkey-patch NativeBleClient with a no-op stub so the
    bridge runs cloud-only without needing a real adapter)
  * Keychain credentials (monkey-patch keychain_get)

Verifies:
  * Bridge boots without BLE
  * Cloud poller logs in, fetches device list + properties
  * BridgeDeviceClient.connect() handshake works
  * BridgeDeviceClient.poll() returns telemetry with source='cloud',
    plus _ble / _cloud meta blocks
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pick a free port for the bridge before importing it
import socket
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
BRIDGE_PORT = _s.getsockname()[1]
_s.close()
os.environ["BRIDGE_HOST"] = "127.0.0.1"
os.environ["BRIDGE_PORT"] = str(BRIDGE_PORT)
os.environ["BLE_POLL_INTERVAL_S"] = "5"
os.environ["CLOUD_POLL_INTERVAL_S"] = "5"

import smoke_cloud  # spins fake jackery server, patches cloud_client
import cloud_client as cc

# Stub the entire NativeBleClient before bridge imports it (no BLE in CI env)
import device_client as _dc
from typing import Optional

class StubBleClient:
    def __init__(self, *a, **kw):
        self._device = None
    async def connect(self):
        return False, None, "stub: no BLE adapter in test env"
    async def disconnect(self):
        return None
    async def poll(self):
        return None
    async def set_output(self, port, on):
        raise _dc.DeviceClientError("stub: BLE not available")
    @property
    def device(self):
        return None

_dc.NativeBleClient = StubBleClient
import device_client
import bridge


# Force bridge to think it has cloud creds + no BLE permission
bridge.keychain_get = lambda service, account: {
    "cloud-email": smoke_cloud.GOOD_EMAIL,
    "cloud-password": smoke_cloud.GOOD_PASS,
    "cloud-region": "US",
}.get(account)


# Stub BLE client: never finds an adapter, never returns telemetry
class StubBle:
    async def connect(self):
        return False, None, "stub: no BLE adapter in test env"

    async def disconnect(self):
        return None

    async def poll(self):
        return None

    async def set_output(self, port, on):
        return None


# Replace the BLE client on the singleton state object
bridge.state.ble = StubBle()

# Speed up the BLE-failure backoff so the test finishes fast
bridge.BLE_POLL = 1


async def run_bridge():
    await bridge.main()


async def main():
    # Spin the fake Jackery cloud
    srv, base = smoke_cloud.start_server()
    cc.BASE_URL = base
    cc.LOGIN_PUBLIC_KEY_B64 = smoke_cloud.TEST_PUB_B64

    # Boot the bridge in background
    task = asyncio.create_task(run_bridge())
    try:
        # Wait until bridge is listening
        for _ in range(40):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)
                w.close()
                await w.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("bridge never came up")
        print("[ok] bridge listening on 127.0.0.1:%d" % BRIDGE_PORT)

        # BridgeDeviceClient handshake
        cli = device_client.BridgeDeviceClient(host="127.0.0.1", port=BRIDGE_PORT)
        ok, info, msg = await cli.connect()
        assert ok, f"connect failed: {msg}"
        print("[ok] BridgeDeviceClient.connect():", msg)

        # Wait for cloud poller to populate telemetry (poll interval = 5s)
        telemetry = None
        for i in range(40):
            t = await cli.poll()
            if t and t.get("battery_percent") == 87:
                telemetry = t
                break
            await asyncio.sleep(0.5)
        assert telemetry, "cloud telemetry never arrived"
        print("[ok] telemetry via bridge:", {
            k: telemetry[k] for k in ("battery_percent", "output_power_w",
                                       "ac_output_v", "ac_on")
        })

        # Verify source meta surfaced through last_status
        st = cli.last_status or {}
        print("[ok] last_status.source =", st.get("source"))
        print("[ok] last_status.ble    =", st.get("ble", {}).get("state"))
        print("[ok] last_status.cloud  =", st.get("cloud", {}).get("state"))
        assert st.get("source") == "cloud", st
        assert st.get("cloud", {}).get("state") == "connected", st
        # BLE was removed from the architecture — the bridge is cloud-only
        # now, and `last_status.ble` is no longer populated. The stub
        # NativeBleClient at the top of the file is kept just so importing
        # bridge.py doesn't crash; we no longer assert anything about it.

        await cli.disconnect()
        print("\nALL BRIDGE SMOKE TESTS PASSED")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        srv.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
