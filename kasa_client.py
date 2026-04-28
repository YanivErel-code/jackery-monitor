"""
Thin wrapper around python-kasa for Jackery Monitor automations.

Two operations matter for our use case:
  - discover()         : find Kasa devices on the LAN (best-effort; depends on
                         Docker networking; bridge mode often blocks UDP
                         broadcasts).
  - set_state(host, on): toggle a specific device by IP. Always works as long
                         as the container can reach the device's IP directly.
  - status(host)       : read the current on/off state.

We keep the API minimal so the rest of the codebase doesn't need to know
about python-kasa internals (model classes, async iterator quirks, etc.).

Newer "KASA SMART" devices (KP125M, EP25, KP405, etc.) require the user's
Kasa cloud account credentials even for local control. We load them lazily
from kasa_creds (encrypted JSON in /data) and pass to python-kasa as a
Credentials object. Older devices (KP115, HS103) ignore the credentials
gracefully, so it's safe to always pass them when available.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import kasa_creds

log = logging.getLogger("kasa_client")


class KasaError(RuntimeError):
    pass


def _credentials():
    """Build a python-kasa Credentials() if Kasa cloud creds are saved.
       Lazy-imports so module load doesn't fail if python-kasa is missing."""
    saved = kasa_creds.load()
    if not saved:
        return None
    try:
        from kasa import Credentials  # type: ignore
    except ImportError:
        return None
    # Kasa cloud expects emails lowercased and trimmed — case mismatches are
    # the #1 reason for "challenge did not match" errors with valid creds.
    email = (saved.get("email") or "").strip().lower()
    password = saved.get("password") or ""
    return Credentials(username=email, password=password)


async def discover(timeout: float = 3.0) -> list[dict]:
    """Best-effort LAN discovery. Returns a list of dicts:
       {host, alias, model, type, is_on}.

       Returns [] (no error) if discovery finds nothing — that's the common
       case in Docker bridge networks where UDP broadcasts don't propagate.
    """
    try:
        from kasa import Discover  # type: ignore
    except ImportError as e:
        raise KasaError(f"python-kasa not installed: {e}")

    creds = _credentials()
    try:
        devices = await Discover.discover(timeout=timeout, credentials=creds)
    except Exception as e:
        log.warning("Kasa discover failed: %s", e)
        return []

    out: list[dict] = []
    for host, dev in (devices or {}).items():
        try:
            await dev.update()
            out.append(_describe(host, dev))
        except Exception as e:
            log.warning("Kasa describe %s failed: %s", host, e)
    log.info("Kasa discovery found %d devices", len(out))
    return out


async def status(host: str) -> dict:
    """Read the current state of a single device by IP."""
    dev = await _connect(host)
    return _describe(host, dev)


async def set_state(host: str, on: bool) -> dict:
    """Turn a device on/off by IP. Returns the new state."""
    dev = await _connect(host)
    if on:
        await dev.turn_on()
    else:
        await dev.turn_off()
    await dev.update()
    return _describe(host, dev)


async def _connect(host: str):
    try:
        from kasa import Discover  # type: ignore
    except ImportError as e:
        raise KasaError(f"python-kasa not installed: {e}")
    creds = _credentials()
    try:
        dev = await Discover.discover_single(host, credentials=creds)
        await dev.update()
        return dev
    except Exception as e:
        # Surface the auth-mode hint plainly — newer Kasa firmware needs
        # the cloud-account credentials, which are saved separately via
        # /api/kasa/credentials.
        msg = str(e)
        if "challenge" in msg.lower() or "credentials" in msg.lower() or "auth" in msg.lower():
            if creds is None:
                msg += " — this device needs Kasa cloud credentials. Add them in the Automation tab."
            else:
                msg += " — saved Kasa cloud credentials were rejected; re-enter them."
        raise KasaError(f"could not reach Kasa device at {host}: {msg}")


def _describe(host: str, dev: Any) -> dict:
    """Pull a small subset of the python-kasa device into a JSON-safe dict."""
    type_name = "unknown"
    try:
        if hasattr(dev, "device_type"):
            type_name = dev.device_type.name if hasattr(dev.device_type, "name") else str(dev.device_type)
    except Exception:
        pass
    return {
        "host": host,
        "alias": getattr(dev, "alias", None) or host,
        "model": getattr(dev, "model", None),
        "type": type_name,
        "is_on": bool(getattr(dev, "is_on", False)),
    }
