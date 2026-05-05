"""Shared exception hierarchy for the Jackery Monitor.

The original codebase had per-module RuntimeError subclasses (KasaError,
DeviceClientError, CloudAuthError, ...) and ~190 bare `except Exception`
sites that couldn't tell a transient network blip from a permanent
config bug. This module gives every module a common base and two
dispatch axes that *do* matter to callers:

  - TransientError  : retry-it-might-work-next-time. Network blip,
                      timeout, RST, "device busy", upstream 5xx, lock
                      contention. Callers should back off and try again.
  - ConfigError     : don't retry. Bad credentials, malformed config,
                      missing dependency. Surface to the user and stop
                      hammering the upstream.

Anything that doesn't clearly fit either axis stays an `IntegrationError`
(generic upstream). That's the safe default — narrowing happens
incrementally as call sites learn the real failure modes.

Existing module-level errors (KasaError, DeviceClientError, ...) are
re-based onto this hierarchy so `except IntegrationError` catches all
upstream failures, while `except TransientError` only catches the
ones worth retrying.
"""

from __future__ import annotations


class JackeryError(Exception):
    """Top of the hierarchy. Every domain error in this app should
    inherit from this so a top-level handler can distinguish ours
    from genuinely-unexpected exceptions (KeyError, AttributeError,
    etc.) that indicate bugs."""


class TransientError(JackeryError):
    """The operation might succeed if retried. Callers should back
    off (see backoff.LoopBackoff) and try again. Network blips,
    timeouts, "device busy", upstream 5xx fall here."""


class ConfigError(JackeryError):
    """The operation will keep failing until configuration changes.
    Bad credentials, malformed input, missing dependency. Don't
    retry; surface to the user. Distinguishing this from
    TransientError is the main reason this hierarchy exists."""


class IntegrationError(JackeryError):
    """Upstream call failed but we can't (yet) say whether it's
    transient or config. Default base for KasaError, DeviceClientError,
    CloudAuthError. Narrow to TransientError or ConfigError when the
    raise site knows."""
