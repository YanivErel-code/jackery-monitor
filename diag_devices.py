"""Probe device-list + property endpoints to see actual response shapes."""
from __future__ import annotations
import asyncio, base64, hashlib, json, subprocess, uuid
import httpx
from Cryptodome.Cipher import AES, PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from Cryptodome.Util.Padding import pad

BASE = "https://iot.jackeryapp.com"
PUB = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCVmzgJy/4XolxPnkfu32YtJqYG"
       "FLYqf9/rnVgURJED+8J9J3Pccd6+9L97/+7COZE5OkejsgOkqeLNC9C3r5mhpE4z"
       "k/HStss7Q8/5DqkGD1annQ+eoICo3oi0dITZ0Qll56Dowb8lXi6WHViVDdih/oeU"
       "wVJY89uJNtTWrz7t7QIDAQAB")
AES_KEY = b"1234567890123456"

def kc(account):
    out = subprocess.check_output(["security","find-generic-password","-s","jackery-monitor","-a",account,"-w"])
    return out[:-1] if out.endswith(b"\n") else out

def aes_enc(s): return base64.b64encode(AES.new(AES_KEY,AES.MODE_ECB).encrypt(pad(s.encode(),16))).decode()
def rsa_enc(b):
    pem = "-----BEGIN PUBLIC KEY-----\n"+PUB+"\n-----END PUBLIC KEY-----"
    return base64.b64encode(PKCS1_v1_5.new(RSA.importKey(pem)).encrypt(b)).decode()

def gen_macid():
    md5 = hashlib.md5(b"abcd1234567890ef").digest()
    return "2"+str(uuid.UUID(bytes=md5,version=3)).replace("-","")

HDR = {
  "accept":"*/*","app_version":"1.0.5","sys_version":"17.2","platform":"1",
  "accept-language":"en-US","accept-encoding":"br;q=1.0, gzip;q=0.9, deflate;q=0.8",
  "user-agent":"DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
  "model":"iPad Pro (12.9-inch) (3rd generation)",
}

async def main():
    email = kc("cloud-email").decode()
    pw    = kc("cloud-password").decode()
    body = {"account":email,"loginType":2,"macId":gen_macid(),"password":pw,"phone":"","registerAppId":"com.hbxn.jackery","verificationCode":""}

    async with httpx.AsyncClient(timeout=15.0, base_url=BASE, headers=HDR) as c:
        # 1) login
        r = await c.post("/v1/auth/login",
            params={"aesEncryptData":aes_enc(json.dumps(body,ensure_ascii=False)),
                    "rsaForAesKey":rsa_enc(AES_KEY)},
            files={"file":("",b"","")})
        d = r.json()
        token = d["token"]
        print("=== LOGIN OK ===  token len:", len(token))

        # 2) device list -- print raw
        r = await c.get("/v1/device/bind/list", headers={"token":token,"content-type":"application/json"})
        print("\n=== /v1/device/bind/list ===")
        print(json.dumps(r.json(), indent=2)[:4000])

        # 3) try /v1/device/list as a fallback
        r2 = await c.get("/v1/device/list", headers={"token":token,"content-type":"application/json"})
        print("\n=== /v1/device/list ===")
        print(json.dumps(r2.json(), indent=2)[:2000])

        # 4) attempt /v1/device/property for any device id we find
        data = r.json().get("data")
        candidates = []
        def walk(o):
            if isinstance(o, dict):
                for k,v in o.items():
                    if k.lower() in ("devid","deviceid","id","sn","devicecode") and isinstance(v,(str,int)):
                        candidates.append((k,str(v)))
                    walk(v)
            elif isinstance(o,list):
                for x in o: walk(x)
        walk(data)
        print("\n=== id candidates ===", candidates[:10])

        for k,dev in candidates[:3]:
            r3 = await c.get("/v1/device/property", params={"deviceId":dev}, headers={"token":token,"content-type":"application/json"})
            print(f"\n=== /v1/device/property?deviceId={dev}  (key was {k}) ===")
            print(json.dumps(r3.json(), indent=2)[:2000])

asyncio.run(main())
