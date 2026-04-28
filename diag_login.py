"""
Standalone cloud login diagnostic.
- Reads email + password directly from keychain (raw bytes) and prints lengths/hex
- Sends the login request manually with verbose logging
- Prints the FULL server response (no truncation)
Run on the Mac:  ./venv/bin/python diag_login.py
"""
from __future__ import annotations
import asyncio, base64, hashlib, json, subprocess, sys, uuid
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

def kc(account: str) -> bytes:
    """Read raw bytes from keychain (no shell quoting issues)."""
    out = subprocess.check_output([
        "security", "find-generic-password",
        "-s", "jackery-monitor",
        "-a", account,
        "-w",
    ])
    # security adds a trailing newline
    if out.endswith(b"\n"):
        out = out[:-1]
    return out

def aes_encrypt(plain: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plain.encode(), AES.block_size))).decode()

def rsa_encrypt(data: bytes) -> str:
    pem = "-----BEGIN PUBLIC KEY-----\n" + PUB + "\n-----END PUBLIC KEY-----"
    return base64.b64encode(PKCS1_v1_5.new(RSA.importKey(pem)).encrypt(data)).decode()

def gen_macid() -> str:
    aid = "abcd1234567890ef"
    md5 = hashlib.md5(aid.encode()).digest()
    return "2" + str(uuid.UUID(bytes=md5, version=3)).replace("-", "")

async def main():
    email = kc("cloud-email").decode()
    pw_bytes = kc("cloud-password")
    print(f"email   ({len(email)} chars): {email!r}")
    print(f"pw_bytes ({len(pw_bytes)} bytes): {pw_bytes!r}")
    print(f"pw hex  : {pw_bytes.hex()}")
    # Look for sneaky chars
    for i, b in enumerate(pw_bytes):
        if b < 0x20 or b > 0x7e:
            print(f"  WARN: non-printable byte at index {i}: 0x{b:02x}")
    pw = pw_bytes.decode("utf-8")

    body = {
        "account": email,
        "loginType": 2,
        "macId": gen_macid(),
        "password": pw,
        "phone": "",
        "registerAppId": "com.hbxn.jackery",
        "verificationCode": "",
    }
    plain = json.dumps(body, ensure_ascii=False)
    print(f"json    ({len(plain)} chars): {plain[:120]}...")
    aes_pl = aes_encrypt(plain)
    rsa_pl = rsa_encrypt(AES_KEY)

    headers = {
        "accept": "*/*",
        "app_version": "1.0.5",
        "sys_version": "17.2",
        "platform": "1",
        "accept-language": "en-US",
        "accept-encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
        "user-agent": "DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
        "model": "iPad Pro (12.9-inch) (3rd generation)",
    }

    async with httpx.AsyncClient(timeout=15.0, base_url=BASE, headers=headers) as c:
        r = await c.post(
            "/v1/auth/login",
            params={"aesEncryptData": aes_pl, "rsaForAesKey": rsa_pl},
            files={"file": ("", b"", "")},
        )
        print(f"HTTP    : {r.status_code}")
        print(f"HEADERS : {dict(r.headers)}")
        print(f"BODY    : {r.text}")

if __name__ == "__main__":
    asyncio.run(main())
