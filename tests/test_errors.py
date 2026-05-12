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
    TokenExpiredError,
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


def test_token_expired_is_distinct_from_contested():
    """Both are auth errors, but the bridge handles them differently:
    contested → 60s cooldown + alert; expired → silent re-login. They
    must inherit CloudAuthError for back-compat catchers, but neither
    is a subclass of the other."""
    with pytest.raises(CloudAuthError):
        raise TokenExpiredError("expired")
    assert not issubclass(TokenExpiredError, SessionContestedError)
    assert not issubclass(SessionContestedError, TokenExpiredError)


def test_classify_auth_error_code_10402_is_expired():
    """Empirical 2026-05-12: Jackery returns code=10402 with
    msg='Token expires' on every poll after the token's ~5s TTL runs
    out. Must be classified 'expired' (routine), not 'contested'."""
    cls = JackeryCloudClient._classify_auth_error
    assert cls({"code": 10402, "msg": "Token expires", "data": {}}) == "expired"


def test_classify_auth_error_legacy_codes_are_contested():
    cls = JackeryCloudClient._classify_auth_error
    assert cls({"code": 401, "msg": "Unauthorized"}) == "contested"
    assert cls({"code": 1001, "msg": ""}) == "contested"
    assert cls({"code": 1002, "msg": ""}) == "contested"


def test_classify_auth_error_msg_fallback_is_expired():
    """Anything we can't pin to a specific code but matches the fuzzy
    'token expired/invalid/auth' pattern defaults to 'expired' — the
    safer choice (silent re-login) given we don't have evidence it's
    a real contention."""
    cls = JackeryCloudClient._classify_auth_error
    assert cls({"code": 12345, "msg": "Token has expired"}) == "expired"
    assert cls({"code": 12345, "msg": "Invalid token"}) == "expired"


def test_classify_auth_error_returns_none_for_success():
    cls = JackeryCloudClient._classify_auth_error
    assert cls({"code": 0, "msg": "ok", "data": {}}) is None
    assert cls(None) is None
    assert cls({}) is None


def test_is_token_expired_back_compat():
    """The back-compat shim returns True for either flavor so callers
    that don't care about the distinction keep working."""
    is_exp = JackeryCloudClient._is_token_expired
    assert is_exp({"code": 10402, "msg": "Token expires"}) is True
    assert is_exp({"code": 401, "msg": "unauth"}) is True
    assert is_exp({"code": 0, "msg": "ok"}) is False


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
