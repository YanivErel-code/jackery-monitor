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
    # Trim whitespace from copy-paste, but DON'T lowercase the email — the
    # Kasa cloud's KLAP challenge-hash uses the email exactly as registered,
    # and we don't know what case the user signed up with. Password is case-
    # sensitive too, untouched.
    email = (saved.get("email") or "").strip()
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


_logged_kasa_version = False

def _log_kasa_version_once():
    global _logged_kasa_version
    if _logged_kasa_version:
        return
    _logged_kasa_version = True
    try:
        import kasa  # type: ignore
        log.info("python-kasa version: %s", getattr(kasa, "__version__", "unknown"))
    except Exception:
        pass


async def _connect(host: str):
    try:
        from kasa import Discover, Device  # type: ignore
    except ImportError as e:
        raise KasaError(f"python-kasa not installed: {e}")
    _log_kasa_version_once()
    creds = _credentials()

    # Two connect strategies — python-kasa's protocol auto-detection has
    # been flaky for SMART devices. Try the modern Device.connect (which
    # picks SMART/IOT/KLAP based on the device's discovery handshake)
    # first; fall back to Discover.discover_single (older API path).
    errors: list[str] = []
    for strategy in ("Device.connect", "Discover.discover_single"):
        try:
            if strategy == "Device.connect" and hasattr(Device, "connect"):
                dev = await Device.connect(host=host, credentials=creds)
            else:
                dev = await Discover.discover_single(host, credentials=creds)
            await dev.update()
            return dev
        except Exception as e:
            errors.append(f"{strategy}: {type(e).__name__}: {e}")
            log.info("Kasa connect via %s failed: %s", strategy, e)

    msg = " | ".join(errors)
    if any("challenge" in e.lower() or "credentials" in e.lower() or "auth" in e.lower() for e in errors):
        if creds is None:
            msg += " — this device needs Kasa cloud credentials. Add them in the Automation tab."
        else:
            msg += " — saved Kasa cloud credentials were rejected. Try lowercasing the email; if you registered the device under a different Kasa account, use that account's credentials."
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
