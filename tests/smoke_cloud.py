"""
Smoke-test for cloud_client.py against an in-process fake Jackery server.

Verifies:
  - AES/RSA encryption round-trips correctly using the fixed key (server side
    decrypts and checks email/password)
  - login -> token -> device list -> property fetch end-to-end
  - cloud_props_to_telemetry adapter produces the expected shape
  - token expiry triggers transparent re-login
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import unpad

import cloud_client as cc

# RSA keypair: we override the *public* key in cloud_client with one we know
# the private half of, so the fake server can decrypt the AES key.
TEST_RSA = RSA.generate(1024)
TEST_PUB_PEM = TEST_RSA.publickey().export_key("PEM").decode()
TEST_PUB_B64 = "".join(TEST_PUB_PEM.splitlines()[1:-1])

GOOD_EMAIL = "test@example.com"
GOOD_PASS = "hunter2"
GOOD_TOKEN = "tok-fresh"
EXPIRED_TOKEN_TRIGGER = {"_force_expire": False}

PROPS = {
    "rb": 87, "bt": 256, "ip": 0, "op": 412,
    # acov is deci-volts (1199 -> 119.9V), acohz is whole Hz (60 -> 60.0Hz)
    "acip": 0, "acov": 1199, "acohz": 60,
    "oac": 1, "odc": 0, "odcu": 1, "odcc": 0,
    "ups": 1, "sfc": 0, "ec": 0,
    "it": 0, "ot": 64,
}


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_kw):
        return

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/v1/auth/login":
            self._json(404, {"code": 404, "msg": "not found"})
            return
        # consume the body so the connection doesn't hang
        try:
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
        except Exception:
            pass
        q = parse_qs(u.query)
        aes_b64 = q.get("aesEncryptData", [""])[0]
        rsa_b64 = q.get("rsaForAesKey", [""])[0]
        try:
            rsa_cipher = PKCS1_v1_5.new(TEST_RSA)
            aes_key = rsa_cipher.decrypt(base64.b64decode(rsa_b64), b"x")
            assert aes_key and aes_key != b"x", "rsa decrypt failed"
            aes_cipher = AES.new(aes_key, AES.MODE_ECB)
            plain = unpad(aes_cipher.decrypt(base64.b64decode(aes_b64)),
                          AES.block_size).decode("utf-8")
            payload = json.loads(plain)
        except Exception as e:
            self._json(200, {"code": 500, "msg": f"decrypt error: {e}"})
            return
        if (payload.get("account") != GOOD_EMAIL or
                payload.get("password") != GOOD_PASS):
            self._json(200, {"code": 1, "msg": "bad credentials"})
            return
        EXPIRED_TOKEN_TRIGGER["_force_expire"] = False
        self._json(200, {"code": 0, "msg": "SUCCESS", "token": GOOD_TOKEN})

    def do_GET(self):
        u = urlparse(self.path)
        token = self.headers.get("token") or ""
        if EXPIRED_TOKEN_TRIGGER["_force_expire"] or token != GOOD_TOKEN:
            self._json(200, {"code": 1001, "msg": "token expired"})
            return
        if u.path == "/v1/device/bind/list":
            # Real shape: data is a bare list of devices, NOT {list: [...]}
            self._json(200, {"code": 0, "msg": "ok", "data": [
                {"devId": "dev-13-001", "devSn": "SN-XYZ",
                 "devModel": "HTE1195000A",
                 "devName": "Explorer 5000 Plus",
                 "devNickname": "Explorer 5000 Plus",
                 "modelCode": 13, "region": "US", "devState": 1},
                {"devId": "dev-19-002", "devSn": "SN-ABC",
                 "devModel": "HTE1163000B",
                 "devName": "HomePower 3000",
                 "devNickname": "HomePower 3000",
                 "modelCode": 19, "region": "US", "devState": 1},
            ]})
            return
        if u.path == "/v1/device/property":
            q = parse_qs(u.query)
            dev_id = q.get("deviceId", [""])[0]
            if dev_id not in ("dev-13-001", "dev-19-002"):
                self._json(200, {"code": 2, "msg": "no such device"})
                return
            self._json(200, {"code": 0, "msg": "ok",
                             "data": {"properties": PROPS}})
            return
        self._json(404, {"code": 404, "msg": "not found"})


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return srv, f"http://{host}:{port}"


async def run() -> None:
    # Patch BASE_URL + fake public key into cloud_client
    srv, base = start_server()
    try:
        cc.BASE_URL = base
        cc.LOGIN_PUBLIC_KEY_B64 = TEST_PUB_B64

        client = cc.JackeryCloudClient(GOOD_EMAIL, GOOD_PASS, region="US")

        tok = await client.login()
        assert tok == GOOD_TOKEN, f"expected {GOOD_TOKEN}, got {tok}"
        print("[ok] login -> token =", tok)

        devs = await client.fetch_devices()
        assert len(devs) == 2, devs
        names = sorted(d.name for d in devs)
        assert names == ["Explorer 5000 Plus", "HomePower 3000"], names
        models = sorted(d.model_code for d in devs)
        assert models == [13, 19], models
        print("[ok] device list (%d devices): %s" % (
            len(devs), [(d.name, d.model_code) for d in devs]))

        props = await client.fetch_properties(devs[0].device_id)
        assert props["rb"] == 87, props
        print("[ok] raw properties: rb=%d op=%d ec=%d" %
              (props["rb"], props["op"], props["ec"]))

        tele = cc.cloud_props_to_telemetry(props)
        expected = {
            "battery_percent": 87, "battery_temp_c": 25.6,
            "output_power_w": 412, "ac_output_v": 119.9,
            "ac_output_hz": 60.0, "ac_on": True, "dc_on": False,
            "usb_on": True, "ups_on": True, "error_code": 0,
        }
        for k, v in expected.items():
            assert tele[k] == v, f"telemetry[{k}]={tele[k]!r}, expected {v!r}"
        print("[ok] cloud_props_to_telemetry: battery=%d%% out=%dW ac=%.1fV/%.1fHz" % (
            tele["battery_percent"], tele["output_power_w"],
            tele["ac_output_v"], tele["ac_output_hz"]))

        # Force expiry -> should re-login transparently
        EXPIRED_TOKEN_TRIGGER["_force_expire"] = True
        client.token = "stale"
        props2 = await client.fetch_properties(devs[0].device_id)
        assert props2["rb"] == 87
        print("[ok] auto re-login on expired token")

        await client.aclose()
        print("\nALL SMOKE TESTS PASSED")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
