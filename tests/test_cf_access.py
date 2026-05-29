"""Unit tests for cf_access JWT verification — the security-critical
path that lets a Cloudflare-Access-authenticated request skip the app
password. We generate a local RSA key, stub the JWKS fetch with its
public half, and craft signed tokens to exercise every accept/reject
branch."""
from __future__ import annotations

import base64
import json
import time

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

import cf_access

TEAM = "testteam"
AUD = "test-aud-tag-123"
ISS = f"https://{TEAM}.cloudflareaccess.com"
KID = "test-kid-1"

_KEY = RSA.generate(2048)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _make_token(*, kid=KID, alg="RS256", aud=AUD, iss=ISS,
                email="user@example.com", exp_offset=3600,
                sign_key=_KEY):
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    payload = {"aud": aud, "iss": iss, "email": email,
               "exp": int(time.time()) + exp_offset,
               "iat": int(time.time())}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    signing_input = f"{h}.{p}".encode()
    sig = pkcs1_15.new(sign_key).sign(SHA256.new(signing_input))
    return f"{h}.{p}.{_b64url(sig)}"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Configure env + stub the JWKS fetch to return our public key."""
    monkeypatch.setenv("JACKERY_CF_ACCESS_TEAM", TEAM)
    monkeypatch.setenv("JACKERY_CF_ACCESS_AUD", AUD)
    monkeypatch.delenv("JACKERY_CF_ACCESS_EMAILS", raising=False)
    pub = _KEY.publickey()

    async def _fake_jwks(team, force=False):
        return {KID: pub}

    monkeypatch.setattr(cf_access, "_get_jwks", _fake_jwks)
    cf_access._jwks_cache.clear()


async def _verify(token):
    return await cf_access.verify(token)


@pytest.mark.asyncio
async def test_valid_token_returns_email():
    assert await _verify(_make_token()) == "user@example.com"


@pytest.mark.asyncio
async def test_email_lowercased():
    assert await _verify(_make_token(email="User@Example.COM")) \
        == "user@example.com"


@pytest.mark.asyncio
async def test_wrong_aud_rejected():
    # A token minted for a different Access app / tenant must not pass.
    assert await _verify(_make_token(aud="some-other-app")) is None


@pytest.mark.asyncio
async def test_wrong_iss_rejected():
    assert await _verify(_make_token(iss="https://evil.cloudflareaccess.com")) is None


@pytest.mark.asyncio
async def test_expired_rejected():
    assert await _verify(_make_token(exp_offset=-3600)) is None


@pytest.mark.asyncio
async def test_alg_none_rejected():
    # alg-confusion: header says "none" → reject without even checking sig.
    assert await _verify(_make_token(alg="none")) is None


@pytest.mark.asyncio
async def test_bad_signature_rejected():
    # Signed with a DIFFERENT key than the JWKS advertises.
    other = RSA.generate(2048)
    assert await _verify(_make_token(sign_key=other)) is None


@pytest.mark.asyncio
async def test_unknown_kid_rejected():
    assert await _verify(_make_token(kid="nope")) is None


@pytest.mark.asyncio
async def test_malformed_token_rejected():
    assert await _verify("not.a.jwt") is None
    assert await _verify("only-one-part") is None
    assert await _verify("") is None
    assert await _verify(None) is None


@pytest.mark.asyncio
async def test_allowlist_enforced(monkeypatch):
    monkeypatch.setenv("JACKERY_CF_ACCESS_EMAILS",
                       "alice@example.com, user@example.com")
    assert await _verify(_make_token()) == "user@example.com"
    assert await _verify(_make_token(email="intruder@example.com")) is None


@pytest.mark.asyncio
async def test_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("JACKERY_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("JACKERY_CF_ACCESS_AUD", raising=False)
    assert not cf_access.is_configured()
    assert await _verify(_make_token()) is None


def test_is_configured_requires_both(monkeypatch):
    monkeypatch.setenv("JACKERY_CF_ACCESS_TEAM", TEAM)
    monkeypatch.delenv("JACKERY_CF_ACCESS_AUD", raising=False)
    assert not cf_access.is_configured()
    monkeypatch.setenv("JACKERY_CF_ACCESS_AUD", AUD)
    assert cf_access.is_configured()
