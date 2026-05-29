"""Cloudflare Access (Zero Trust) JWT verification.

When the dashboard sits behind Cloudflare Access with an identity
provider (e.g. Google / Gmail), Cloudflare authenticates the user at the
edge and forwards the request with a signed assertion in the
`Cf-Access-Jwt-Assertion` header. This module verifies that assertion
cryptographically so the app can trust the caller's email WITHOUT its own
password prompt — but only for requests that carry a valid, correctly-
audienced token. Direct LAN hits (no header) fall through to the app's
password login, so the origin stays protected even though it's reachable
on the LAN at :8123, and removing the app password is not required.

Verification is a full JWS RS256 check:
  • signature against Cloudflare's published JWKS (matched by `kid`,
    cached, refetched once on an unknown kid for key rotation),
  • `alg` pinned to RS256 (rejects the "none" / HS* alg-confusion class),
  • `iss` == the team domain,
  • `aud` contains the configured Application Audience tag — without this
    a token minted for a DIFFERENT Access app (or another Cloudflare
    tenant) could be replayed here,
  • `exp` not passed (small leeway).

Config (env, read at call time so it can change without a code edit):
  JACKERY_CF_ACCESS_TEAM    team name, e.g. "myteam" → certs at
                            https://myteam.cloudflareaccess.com/cdn-cgi/access/certs
  JACKERY_CF_ACCESS_AUD     the Application Audience (AUD) tag shown in
                            the Access application's settings
  JACKERY_CF_ACCESS_EMAILS  optional comma-separated allowlist; empty =
                            trust any email the Access policy admitted
                            (the policy is the primary gate; this is a
                            belt-and-suspenders second check)

Disabled — `is_configured()` False, `verify()` returns None for
everything — until both TEAM and AUD are set. Shipping this module with
no env set is a no-op.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time

import httpx
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

log = logging.getLogger("cf_access")

# Header name is matched case-insensitively (Starlette lowercases headers).
HEADER_NAME = "cf-access-jwt-assertion"
_JWKS_TTL_S = 3600
_LEEWAY_S = 30

_jwks_lock = threading.Lock()
# team -> (expires_at, {kid: RSA public key})
_jwks_cache: dict[str, tuple[float, dict]] = {}


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _team() -> str:
    return (os.environ.get("JACKERY_CF_ACCESS_TEAM") or "").strip()


def _aud() -> str:
    return (os.environ.get("JACKERY_CF_ACCESS_AUD") or "").strip()


def _allowlist() -> set[str]:
    raw = (os.environ.get("JACKERY_CF_ACCESS_EMAILS") or "").strip()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _team_domain(team: str) -> str:
    return f"https://{team}.cloudflareaccess.com"


def is_configured() -> bool:
    """True only when both team and audience are set — until then this
    whole layer is inert and the app behaves exactly as before."""
    return bool(_team() and _aud())


async def _get_jwks(team: str, force: bool = False) -> dict:
    """Return {kid: RSA pubkey} for the team, cached for _JWKS_TTL_S.
    On fetch failure, serve a stale cache if present rather than locking
    everyone out over a transient network blip."""
    now = time.time()
    if not force:
        with _jwks_lock:
            cached = _jwks_cache.get(team)
            if cached and cached[0] > now:
                return cached[1]
    url = f"{_team_domain(team)}/cdn-cgi/access/certs"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("cf_access: JWKS fetch failed (%s); serving stale", e)
        with _jwks_lock:
            cached = _jwks_cache.get(team)
        return cached[1] if cached else {}
    keys: dict[str, object] = {}
    for jwk in data.get("keys", []):
        try:
            if jwk.get("kty") != "RSA":
                continue
            n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
            e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
            keys[jwk["kid"]] = RSA.construct((n, e))
        except Exception:
            continue
    with _jwks_lock:
        _jwks_cache[team] = (now + _JWKS_TTL_S, keys)
    return keys


async def verify(token: str | None) -> str | None:
    """Verify a Cf-Access-Jwt-Assertion. Returns the authenticated email
    (lowercased) on success, else None. Safe to call when unconfigured."""
    team, aud = _team(), _aud()
    if not team or not aud or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception:
        return None
    # Pin the algorithm: never let the token's header talk us into "none"
    # or an HMAC alg we'd verify with public-key bytes.
    if header.get("alg") != "RS256":
        return None
    kid = header.get("kid")
    if not kid:
        return None
    try:
        sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    signing_input = f"{header_b64}.{payload_b64}".encode()

    keys = await _get_jwks(team)
    pub = keys.get(kid)
    if pub is None:
        keys = await _get_jwks(team, force=True)  # rotation: refetch once
        pub = keys.get(kid)
    if pub is None:
        return None
    try:
        pkcs1_15.new(pub).verify(SHA256.new(signing_input), sig)
    except (ValueError, TypeError):
        return None  # signature mismatch

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or (exp + _LEEWAY_S) < time.time():
        return None
    if payload.get("iss") != _team_domain(team):
        return None
    token_aud = payload.get("aud")
    auds = token_aud if isinstance(token_aud, list) else [token_aud]
    if aud not in auds:
        return None
    email = (payload.get("email") or "").strip().lower()
    if not email:
        return None
    allow = _allowlist()
    if allow and email not in allow:
        return None
    return email
