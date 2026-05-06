"""End-to-end smoke tests for FastAPI routes registered in server.py.

Uses TestClient with a mock backend + isolated /data dir. Each test
reloads `server` so its module-level `state` picks up the patched env
vars (same pattern as test_server_view.py).

Coverage focus: high-traffic happy paths + the auth gate. Detailed
behavioral tests for individual subsystems live in their own files
(test_automation, test_smart_charge, test_energy_db, etc.); these are
the route-layer tests the audit flagged as missing.
"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

# Force mock backend at module-import time so server's `state.client`
# lands on the synthetic generator instead of trying to connect to the
# bridge daemon during the lifespan startup.
os.environ["BACKEND"] = "mock"


@pytest.fixture()
def app(isolated_data, monkeypatch, tmp_path):
    """Reload server with isolated data and a mock backend; return the
    module so tests can also reach `state` directly when needed.

    Every module that reads a /data path at import time has its
    module-level constants frozen on first import. We force-reload the
    relevant modules in dependency order so they pick up the patched
    env vars from the isolated_data fixture (which runs first)."""
    monkeypatch.setenv("JACKERY_MOCK", "1")
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("JACKERY_DB", str(tmp_path / "energy.db"))
    monkeypatch.setenv("JACKERY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JACKERY_BACKUP_CREDS_FILE",
                       str(tmp_path / "backup-creds.json"))

    # Reload in dependency order so each module sees the patched env
    # AND its dependencies' updated constants. crypto_util has to be
    # first since auth, *_creds modules all depend on it.
    import crypto_util
    importlib.reload(crypto_util)
    for name in (
        "auth", "settings", "automation", "location", "smart_charge",
        "cost", "anthropic_creds", "anthropic_prefs",
        "kasa_creds", "kasa_devices", "backup_creds", "energy_db",
    ):
        mod = importlib.import_module(name)
        importlib.reload(mod)

    import server as server_mod
    importlib.reload(server_mod)
    return server_mod


@pytest.fixture()
def unauth_client(app):
    """TestClient with NO auth user set up — used to exercise the
    /setup redirect path."""
    with TestClient(app.app) as c:
        yield c


@pytest.fixture()
def client(app):
    """TestClient with an admin user pre-created. Cookies are set on
    the client jar so subsequent requests are authenticated."""
    with TestClient(app.app) as c:
        r = c.post(
            "/api/auth/setup",
            json={"username": "smoke", "password": "smokesmokesmoke"},
        )
        assert r.status_code == 200, r.text
        yield c


# ---------- auth flow + middleware ----------

def test_unauth_root_redirects_to_setup_when_no_user(unauth_client):
    """Fresh install (no user yet) → / redirects to /setup."""
    r = unauth_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_unauth_api_returns_401_when_no_user(unauth_client):
    """Same gate, but for an /api/* path → 401 setup_required."""
    r = unauth_client.get("/api/status")
    assert r.status_code == 401
    assert r.json() == {"detail": "setup_required"}


def test_auth_setup_creates_user_and_sets_cookie(unauth_client):
    r = unauth_client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "verysecret"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "username": "alice"}
    # Subsequent setup attempts are 403.
    r2 = unauth_client.post(
        "/api/auth/setup",
        json={"username": "bob", "password": "anothersecret"},
    )
    assert r2.status_code == 403


def test_auth_setup_rejects_short_password(unauth_client):
    r = unauth_client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "short"},
    )
    assert r.status_code == 400


def test_auth_login_logout_round_trip(client):
    # client fixture already created user "smoke"; logout, then back in.
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    # /api/auth/me should now 401.
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    r = client.post(
        "/api/auth/login",
        json={"username": "smoke", "password": "smokesmokesmoke"},
    )
    assert r.status_code == 200
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "smoke"


def test_auth_login_rejects_wrong_password(client):
    r = client.post(
        "/api/auth/login",
        json={"username": "smoke", "password": "WRONG"},
    )
    assert r.status_code == 401


def test_change_password_round_trip(client):
    r = client.post(
        "/api/auth/change_password",
        json={"current": "smokesmokesmoke", "new": "newsecretpw"},
    )
    assert r.status_code == 200
    # Old password should now fail.
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login",
        json={"username": "smoke", "password": "smokesmokesmoke"},
    )
    assert r.status_code == 401
    # New password works.
    r = client.post(
        "/api/auth/login",
        json={"username": "smoke", "password": "newsecretpw"},
    )
    assert r.status_code == 200


# ---------- core read endpoints ----------

def test_status_returns_telemetry_after_auth(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    # connect_device runs async in the lifespan, so a fast test can
    # land before it completes — accept any non-error state.
    assert body["connection_status"] in (
        "scanning", "connecting", "connected", "disconnected",
    )
    # Top-level keys the UI relies on.
    assert "device" in body


def test_devices_endpoint_returns_list(client):
    r = client.get("/api/devices")
    assert r.status_code == 200
    body = r.json()
    assert "devices" in body
    assert isinstance(body["devices"], list)


def test_devices_capacity_endpoint(client):
    r = client.get("/api/devices/capacity")
    assert r.status_code == 200
    assert "devices" in r.json()


def test_settings_get_and_save_round_trip(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    # /api/settings returns metadata (label/hint/default + current value)
    # under "settings", keyed by name with the schema as values.
    assert "settings" in body
    keys = {s["key"] for s in body["settings"]}
    assert "low_battery_threshold" in keys
    # Round-trip a benign setting.
    r = client.post(
        "/api/settings",
        json={"low_battery_threshold": 12},
    )
    assert r.status_code == 200
    r = client.get("/api/settings")
    by_key = {s["key"]: s for s in r.json()["settings"]}
    assert by_key["low_battery_threshold"].get("value") == 12


def test_anthropic_models_returns_fallback_without_key(client):
    """No key configured → endpoint must still return a populated list
    (the static fallback) so the UI dropdown isn't empty."""
    r = client.get("/api/anthropic/models")
    assert r.status_code == 200
    body = r.json()
    assert body["source"].startswith("fallback")
    assert len(body["models"]) > 0


def test_automation_rules_initially_empty(client):
    r = client.get("/api/automation/rules")
    assert r.status_code == 200
    body = r.json()
    assert body["rules"] == []


def test_algorithm_suggestions_initially_empty(client):
    r = client.get("/api/algorithm/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["suggestions"] == []


def test_algorithm_changes_initially_empty(client):
    r = client.get("/api/algorithm/changes")
    assert r.status_code == 200
    body = r.json()
    assert body["changes"] == []


def test_kasa_saved_initially_empty(client):
    """The /api/kasa/devices route runs a live LAN discovery (which 500s
    when python-kasa isn't installed in CI). /api/kasa/saved reads the
    persisted registry — that's the route the UI hits on tab open."""
    r = client.get("/api/kasa/saved", params={"refresh": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["devices"] == []


def test_backup_status_endpoint(client):
    r = client.get("/api/backup/status")
    assert r.status_code == 200
    # Should have at least the configured/last_run scaffolding even when
    # nothing's been backed up yet.
    body = r.json()
    assert isinstance(body, dict)


def test_smart_charge_config_returns_default(client):
    """No active device picked + no configs saved → endpoint should
    return a usable default config rather than 500."""
    r = client.get("/api/smart_charge/config")
    assert r.status_code == 200
    body = r.json()
    assert "config" in body
    assert "mode" in body["config"]


def test_cost_plan_get_endpoint(client):
    r = client.get("/api/cost/plan")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_location_get_endpoint(client):
    r = client.get("/api/location")
    assert r.status_code == 200


# ---------- error paths ----------

def test_review_now_requires_device(client):
    """No active device + no device_sn arg → 400, not 500."""
    r = client.post("/api/algorithm/review_now", params={})
    # Mock backend DOES advertise a device, so this might actually 202.
    # Either is acceptable; we just want to know we don't 500.
    assert r.status_code in (202, 400)


def test_unknown_route_returns_404(client):
    r = client.get("/api/totally/made/up")
    assert r.status_code == 404
