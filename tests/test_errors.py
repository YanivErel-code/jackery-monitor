"""Verify the shared exception hierarchy and that re-based domain
errors still satisfy their old `except RuntimeError`/`except ValueError`
catchers (the multi-inheritance compatibility layer)."""

from __future__ import annotations

import pytest

from automation import AutomationError
from cloud_client import (
    CloudAuthError,
    CloudCredentialsError,
    JackeryCloudClient,
    SessionContestedError,
)
from device_client import DeviceClientError
from errors import ConfigError, IntegrationError, JackeryError, TransientError
from kasa_client import KasaConfigError, KasaError, KasaTransientError


def test_hierarchy_basics():
    # JackeryError is the root; the three axes are direct children.
    assert issubclass(TransientError, JackeryError)
    assert issubclass(ConfigError, JackeryError)
    assert issubclass(IntegrationError, JackeryError)
    # The axes are siblings, not parents of each other.
    assert not issubclass(TransientError, ConfigError)
    assert not issubclass(ConfigError, TransientError)


def test_kasa_classification():
    # KasaTransientError must satisfy `except TransientError`,
    # KasaConfigError must satisfy `except ConfigError`.
    assert issubclass(KasaTransientError, TransientError)
    assert issubclass(KasaTransientError, KasaError)
    assert issubclass(KasaConfigError, ConfigError)
    assert issubclass(KasaConfigError, KasaError)
    # Both still satisfy `except KasaError`.
    with pytest.raises(KasaError):
        raise KasaTransientError("blip")
    with pytest.raises(KasaError):
        raise KasaConfigError("bad creds")


def test_legacy_runtimeerror_compat():
    # Existing code that catches RuntimeError must still see these.
    with pytest.raises(RuntimeError):
        raise KasaError("x")
    with pytest.raises(RuntimeError):
        raise DeviceClientError("y")
    with pytest.raises(RuntimeError):
        raise CloudAuthError("z")


def test_legacy_valueerror_compat():
    # AutomationError used to be a ValueError subclass and callers may
    # still rely on that.
    with pytest.raises(ValueError):
        raise AutomationError("bad rule")
    # And it's a ConfigError now.
    with pytest.raises(ConfigError):
        raise AutomationError("bad rule")


def test_session_contested_inherits_cloud_auth():
    with pytest.raises(CloudAuthError):
        raise SessionContestedError("kicked")
    # SessionContestedError isn't a ConfigError — re-login can fix it.
    assert not issubclass(SessionContestedError, ConfigError)


def test_cloud_credentials_is_config_error():
    # CloudCredentialsError IS a ConfigError — bad creds are user-fixable.
    assert issubclass(CloudCredentialsError, ConfigError)
    assert issubclass(CloudCredentialsError, CloudAuthError)


def test_auth_error_code_10402_is_contested():
    """Empirical 2026-05-13: Jackery returns code=10402 with
    msg='Token expires' when another client has signed in on the same
    account. The 'TTL' framing earlier in the project's history was
    wrong — a leaked credential was constantly invalidating us.
    All auth-error codes route to the same contested-cooldown path."""
    assert JackeryCloudClient._is_auth_error(
        {"code": 10402, "msg": "Token expires", "data": {}}
    ) is True


def test_auth_error_legacy_codes_are_contested():
    is_err = JackeryCloudClient._is_auth_error
    assert is_err({"code": 401, "msg": "Unauthorized"}) is True
    assert is_err({"code": 1001, "msg": ""}) is True
    assert is_err({"code": 1002, "msg": ""}) is True


def test_auth_error_msg_fallback():
    """Fuzzy fallback for protocol drift — if the code is unknown but
    the message mentions token/expired/invalid/auth, treat it as
    contention rather than miss the signal."""
    is_err = JackeryCloudClient._is_auth_error
    assert is_err({"code": 12345, "msg": "Token has expired"}) is True
    assert is_err({"code": 12345, "msg": "Invalid token"}) is True


def test_auth_error_returns_false_for_success():
    is_err = JackeryCloudClient._is_auth_error
    assert is_err({"code": 0, "msg": "ok", "data": {}}) is False
    assert is_err(None) is False
    assert is_err({}) is False


def test_is_token_expired_back_compat_alias():
    """`_is_token_expired` is kept as an alias of `_is_auth_error` so
    any external caller importing the old name keeps working."""
    assert (
        JackeryCloudClient._is_token_expired
        is JackeryCloudClient._is_auth_error
    )


def test_dispatch_lets_caller_separate_axes():
    # The whole point: a caller can split transient retry from config
    # surface-to-user without substring-matching messages.
    def classify(exc: Exception) -> str:
        if isinstance(exc, TransientError):
            return "retry"
        if isinstance(exc, ConfigError):
            return "surface"
        return "unknown"

    assert classify(KasaTransientError("blip")) == "retry"
    assert classify(KasaConfigError("bad creds")) == "surface"
    assert classify(KasaError("plain")) == "unknown"
