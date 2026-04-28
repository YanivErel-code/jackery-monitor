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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger("kasa_client")


class KasaError(RuntimeError):
    pass


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

    try:
        devices = await Discover.discover(timeout=timeout)
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
    try:
        dev = await Discover.discover_single(host)
        await dev.update()
        return dev
    except Exception as e:
        raise KasaError(f"could not reach Kasa device at {host}: {e}")


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
