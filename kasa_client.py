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

import logging
from typing import Any

import kasa_creds
from errors import ConfigError, IntegrationError, TransientError

log = logging.getLogger("kasa_client")


# Multiple inheritance preserves `except RuntimeError` callers while
# adopting the shared hierarchy for new dispatch.
class KasaError(IntegrationError, RuntimeError):
    """Generic kasa upstream failure. Prefer KasaTransientError or
    KasaAuthError when the raise site can tell which kind it is."""


class KasaTransientError(KasaError, TransientError):
    """Retryable: timeout, connection reset, "device busy". Callers
    should back off and try again."""


class KasaConfigError(KasaError, ConfigError):
    """A configuration problem the user has to fix: missing python-kasa
    dependency, or saved Kasa cloud credentials are missing/rejected.
    Don't retry — surface to the user."""


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
        raise KasaConfigError(f"python-kasa not installed: {e}") from e

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


# Per-call retry for transient kasa failures. Network blips, the
# device being briefly busy after a recent toggle, and TCP RSTs all
# fail the first attempt and succeed the second. Auth errors are
# permanent and skip the retry — no point hammering bad creds.
_KASA_RETRY_ATTEMPTS = 3
_KASA_RETRY_BACKOFF_S = (0.4, 0.8)  # delays between attempts


async def _with_retry(operation_name: str, fn):
    """Run an async kasa operation with bounded retries on transient
    failures. `fn` is a no-arg async callable that does one attempt
    and returns its result (or raises). KasaConfigError (bad creds,
    missing dep) breaks out immediately — retrying won't help and
    just multiplies the user-facing error spam by 3x."""
    import asyncio
    last_err: Exception | None = None
    for attempt in range(_KASA_RETRY_ATTEMPTS):
        try:
            return await fn()
        except KasaConfigError:
            raise
        except Exception as e:
            last_err = e
        if attempt < _KASA_RETRY_ATTEMPTS - 1:
            delay = _KASA_RETRY_BACKOFF_S[min(attempt, len(_KASA_RETRY_BACKOFF_S) - 1)]
            log.info("Kasa %s attempt %d/%d failed: %s; retrying in %.1fs",
                     operation_name, attempt + 1, _KASA_RETRY_ATTEMPTS,
                     last_err, delay)
            await asyncio.sleep(delay)
    # All attempts failed — re-raise the last error so callers see
    # the actual underlying message rather than a generic "retry
    # exhausted" string.
    raise last_err if last_err is not None else KasaError(f"{operation_name} failed")


async def status(host: str) -> dict:
    """Read the current state of a single device by IP. Retries
    transient failures (network blip, device busy)."""
    async def _attempt():
        dev = await _connect(host)
        return _describe(host, dev)
    return await _with_retry(f"status({host})", _attempt)


async def set_state(host: str, on: bool) -> dict:
    """Turn a device on/off by IP. Retries transient failures so a
    single network blip during a UI toggle doesn't bubble up as
    'err' to the user — most kasa hiccups recover on attempt 2."""
    async def _attempt():
        dev = await _connect(host)
        if on:
            await dev.turn_on()
        else:
            await dev.turn_off()
        await dev.update()
        return _describe(host, dev)
    return await _with_retry(f"set_state({host}, on={on})", _attempt)


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


def _is_auth_failure(err: Exception) -> bool:
    lower = str(err).lower()
    return ("challenge" in lower or "credentials" in lower
            or "klap" in lower or "authenticationerror" in lower)


async def _connect(host: str):
    try:
        from kasa import Discover  # type: ignore
    except ImportError as e:
        raise KasaConfigError(f"python-kasa not installed: {e}") from e
    _log_kasa_version_once()
    creds = _credentials()
    first_err: Exception | None = None
    # Pass 1: with saved cloud creds if present. Required for SMART-line
    # plugs (KP125M / EP25 / KP405); older plugs ignore them.
    if creds is not None:
        try:
            dev = await Discover.discover_single(host, credentials=creds)
            await dev.update()
            return dev
        except Exception as e:
            if not _is_auth_failure(e):
                raise KasaTransientError(
                    f"could not reach Kasa device at {host}: "
                    f"{type(e).__name__}: {e}"
                ) from e
            first_err = e
            log.info("Kasa %s: credentialed connect rejected, retrying "
                     "without creds (older plug fallback)", host)
    # Pass 2: no creds. Works for the older HS / KP non-M line; will
    # fail-with-auth-error for SMART-line if the saved creds were wrong.
    try:
        dev = await Discover.discover_single(host, credentials=None)
        await dev.update()
        return dev
    except Exception as e:
        # Prefer the original credentialed error message if both passes
        # failed — the user almost certainly has a SMART-line plug with
        # bad creds, and that's the actionable diagnosis.
        err = first_err or e
        msg = f"{type(err).__name__}: {err}"
        lower = str(err).lower()
        if _is_auth_failure(err):
            if first_err is None:
                # No creds saved at all and the device wants them.
                msg += " — this device needs Kasa cloud credentials. Add them in the Automation tab."
            else:
                # Tried with creds, fell through, still failed.
                msg += (" — saved Kasa cloud credentials were rejected AND "
                        "credential-less fallback also failed. Verify the "
                        "email matches your Kasa account exactly (case as "
                        "registered) or re-add this device.")
            raise KasaConfigError(
                f"could not reach Kasa device at {host}: {msg}"
            ) from err
        if "zoneinfo" in lower or "no time zone" in lower:
            msg += " — server is missing tzdata; if you're seeing this, the latest image hasn't deployed yet."
        raise KasaTransientError(
            f"could not reach Kasa device at {host}: {msg}"
        ) from err


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
