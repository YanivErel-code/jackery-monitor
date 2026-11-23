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
import json
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

@pytest.mark.parametrize("path", ["/", "/v2", "/classic", "/sw.js", "/static/app.js", "/static/v2.js",
                                  "/static/v2_history.js",
                                  "/static/v2_rules.js", "/static/v2_settings.js"])
def test_dashboard_shell_revalidates_after_deployment(client, path):
    """HTML and scripts must not age independently across a deployment."""
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("etag")
    if path == "/sw.js":
        assert response.headers.get("service-worker-allowed") == "/"


def test_v2_dashboard_is_behind_the_same_auth_gate(unauth_client):
    r = unauth_client.get("/v2", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"
    assert unauth_client.get("/api/v2/overview").status_code == 401


def test_root_opens_v2_by_default_and_classic_once_saved_as_default(client):
    """"/" follows the saved default; /v2 and /classic always open theirs."""
    v2, classic = client.get("/v2").text, client.get("/classic").text
    assert 'id="page-live"' in v2 and 'id="tab-live"' in classic
    assert client.get("/").text == v2
    r = client.post("/api/settings", json={"default_ui_version": 1})
    assert r.status_code == 200 and r.json()["settings"]["default_ui_version"] == 1
    assert client.get("/").text == classic
    assert client.get("/v2").text == v2
    client.post("/api/settings", json={"default_ui_version": 2})
    assert client.get("/").text == v2


def test_each_view_links_to_the_other_and_settings_offers_the_default(client):
    classic = client.get("/classic").text
    assert 'href="/v2"' in classic and 'id="default-ui"' in classic
    v2 = client.get("/v2").text
    assert 'href="/classic"' in v2
    # History, Rules & plugs and Settings stay inside V2's menu (framed
    # classic screens) instead of navigating away from it.
    for page in ("history", "rules", "settings"):
        assert f'href="#{page}" data-page="{page}"' in v2
    # Every script the V2 shell loads must be one the server actually has.
    import re
    for src in re.findall(r'<script src="([^"]+)"', v2):
        assert client.get(src).status_code == 200, src
    assert all(f'id="page-{name}"' in v2 for name in ("forecast", "history", "rules", "settings"))
    # A colour token used but not defined renders as nothing (an invisible
    # button, in one case), so every var() must exist in both themes.
    css = client.get("/static/v2.css").text
    used = set(re.findall(r"var\(--([a-z0-9-]+)", css))
    for theme in ("light", "dark"):
        block = re.search(r':root\[data-theme="' + theme + r'"\] \{(.*?)\n\}', css, re.S).group(1)
        assert used <= set(re.findall(r"--([a-z0-9-]+):", block)), theme
    assert 'id="classic-frame"' in v2 and "classList.add('embed')" in classic
    spec = next(s for s in client.get("/api/settings").json()["settings"] if s["key"] == "default_ui_version")
    assert (spec["default"], spec["min"], spec["max"], spec["control"]) == (2, 1, 2, "custom")


def test_shells_stamp_asset_urls_with_the_build_and_report_it(client, app):
    """A new shell must never run against cached old scripts: asset URLs
    change with the build, and the overview reports the build so an open
    page can reload itself after a deploy."""
    import re
    build = app.ui_build()
    assert re.fullmatch(r"[0-9a-f]{12}", build)
    for path in ("/v2", "/classic"):
        html = client.get(path).text
        assets = re.findall(r'"(/static/[^"]+\.(?:js|css)[^"]*)"', html)
        assert assets and all(a.endswith(f"?v={build}") for a in assets), assets
        assert client.get(assets[0]).status_code == 200
    assert f'name="ui-build" content="{build}"' in client.get("/v2").text
    assert "__UI_BUILD__" not in client.get("/v2").text
    sn = app.state.device.device_sn if app.state.device else "MOCK"
    assert client.get("/api/v2/overview", params={"device_sn": sn}).json()["ui_build"] == build


def test_first_release_classic_links_still_open_the_classic_view(client):
    assert client.get("/?ui=v1").text == client.get("/classic").text
    assert client.get("/").text == client.get("/v2").text


def test_v2_overview_answers_before_any_decision_exists(client, app):
    sn = app.state.device.device_sn if app.state.device else None
    r = client.get("/api/v2/overview", params={"device_sn": sn or "MOCK"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["headline"]["state"] == "manual"
    assert body["plan"]["blocks"] == [] and body["activity"] == []
    assert body["rates"] and body["grid_today"]["tiers"]


def test_app_summary_is_compact_and_carries_the_headline(client, app):
    r = client.get("/api/app/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "now", "device_sn", "device_name", "connection", "last_update_ts", "battery",
        "power", "headline", "charging_now", "next_charge_ts", "until_ts", "reserve_pct"}
    assert set(body["battery"]) == {"soc_pct", "time_remaining_h", "time_to_full_h"}
    assert set(body["power"]) == {"solar_w", "grid_w", "input_w", "output_w"}
    if body["device_sn"]:
        assert body["headline"]["state"] == "manual" and body["headline"]["title"]
    assert body["charging_now"] is False and body["next_charge_ts"] is None
    assert len(r.content) < 2000


def test_app_summary_requires_sign_in(app):
    with TestClient(app.app, follow_redirects=False) as c:
        assert c.get("/api/app/summary").status_code == 401


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


def test_session_cookie_secure_flag_follows_env(unauth_client, monkeypatch):
    """JACKERY_COOKIE_SECURE=1 must promote the Set-Cookie to Secure.
    Default (unset/0) leaves it off so Cloudflare Tunnel HTTP-to-origin
    deployments still work."""
    monkeypatch.setenv("JACKERY_COOKIE_SECURE", "1")
    r = unauth_client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "verysecret"},
    )
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "jackery_session=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()


def test_session_cookie_default_is_not_secure(unauth_client, monkeypatch):
    """Default: no Secure flag — matches the canonical Cloudflare Tunnel
    deployment where the origin sees plain HTTP."""
    monkeypatch.delenv("JACKERY_COOKIE_SECURE", raising=False)
    r = unauth_client.post(
        "/api/auth/setup",
        json={"username": "alice", "password": "verysecret"},
    )
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "jackery_session=" in set_cookie
    # No "Secure" attribute on the cookie.
    assert "secure" not in set_cookie.lower()


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


# ---------- energy daily rollup ----------

def test_energy_daily_route_shape_and_clamp(app, client):
    """/api/energy/daily: contract shape, days clamp, and per-device
    resolution. DB-level rollup math is covered in test_energy_db; this
    is the route-layer regression the integration review flagged."""
    sn = "TEST-DAILY-RT"
    app.state.energy.upsert_device(sn, "Rig", 13, "Explorer 5000 Plus")
    bucket = (int(__import__("time").time()) - 3600) // 60 * 60
    with app.state.energy._conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO samples
                   (device_sn, bucket, solar_wh, output_wh, input_wh,
                    ac_input_wh, solar_charge_diverted_wh,
                    last_solar_w, last_output_w, last_battery_pct,
                    sample_count)
               VALUES (?, ?, 1500, 800, 900, 200, 100, 3200, 1900, 42, 1)""",
            (sn, bucket))

    r = client.get(f"/api/energy/daily?device_sn={sn}&days=9999")
    assert r.status_code == 200
    j = r.json()
    assert j["device_sn"] == sn
    assert j["days"] == 365                      # clamped
    assert isinstance(j["tz_offset_s"], int)
    assert len(j["daily"]) == 1
    row = j["daily"][0]
    assert set(row) == {"date", "solar_kwh", "consumed_kwh", "charged_kwh",
                        "grid_kwh", "diverted_kwh", "peak_solar_w",
                        "peak_output_w", "min_soc", "max_soc"}
    assert row["solar_kwh"] == 1.5
    assert row["peak_output_w"] == 1900
    assert row["min_soc"] == 42 and row["max_soc"] == 42

    # Unknown device -> empty, not an error.
    r2 = client.get("/api/energy/daily?device_sn=NOPE")
    assert r2.status_code == 200 and r2.json()["daily"] == []


def test_energy_daily_requires_auth(unauth_client):
    unauth_client.post("/api/auth/setup",
                       json={"username": "u", "password": "p" * 12})
    unauth_client.post("/api/auth/logout")
    r = unauth_client.get("/api/energy/daily")
    assert r.status_code == 401


# ---------------------------------------------------------------------
# /app/handoff: returns the Cloudflare Access assertion to the iOS app,
# and only for a request that Access itself authenticated.
# ---------------------------------------------------------------------

_HANDOFF_TOKEN = "aaa.bbb.ccc-_"


@pytest.fixture()
def access(monkeypatch):
    import cf_access

    async def _verify(token):
        return "user@example.com" if token == _HANDOFF_TOKEN else None

    monkeypatch.setattr(cf_access, "verify", _verify)
    monkeypatch.setattr(cf_access, "is_configured", lambda: True)
    return cf_access


def test_handoff_returns_the_assertion_to_the_app(app, access):
    from urllib.parse import parse_qs, urlsplit
    with TestClient(app.app, follow_redirects=False) as c:
        r = c.get("/app/handoff", headers={access.HEADER_NAME: _HANDOFF_TOKEN})
    assert r.status_code == 303
    target = urlsplit(r.headers["location"])
    assert (target.scheme, target.netloc) == ("jackery", "auth")
    assert parse_qs(target.query) == {"cf": [_HANDOFF_TOKEN]}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [{}, {"cf-access-jwt-assertion": "forged"}])
def test_handoff_refuses_without_a_valid_assertion(app, access, headers):
    with TestClient(app.app, follow_redirects=False) as c:
        r = c.get("/app/handoff", headers=headers)
    assert "jackery://" not in r.headers.get("location", "")
    assert r.status_code != 200


def test_handoff_gives_a_password_session_nothing(client, access):
    """A LAN password session has no Access assertion to hand over."""
    r = client.get("/app/handoff", follow_redirects=False)
    assert r.status_code == 404


def test_handoff_is_off_when_access_is_not_configured(client, access, monkeypatch):
    monkeypatch.setattr(access, "is_configured", lambda: False)
    r = client.get("/app/handoff", follow_redirects=False,
                   headers={access.HEADER_NAME: _HANDOFF_TOKEN})
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Alerts and Apple push: settings API, phone registration, one pass of the loop.
# ---------------------------------------------------------------------

_PUSH_TOKEN, _START_TOKEN, _ACTIVITY_TOKEN = "a1" * 32, "b2" * 32, "c3" * 40


def _push_key_pem() -> str:
    from Crypto.PublicKey import ECC
    return ECC.generate(curve="P-256").export_key(format="PEM", use_pkcs8=True)


def test_alerts_api_lists_rules_and_saves_settings(client):
    body = client.get("/api/alerts").json()
    assert body["config"]["enabled"] is False and body["recent"] == []
    assert body["push"] == {"configured": False, "team_id": None, "key_id": None, "bundle_id": None, "devices": []}
    assert {r["name"] for r in body["rules"]} == set(body["config"]["rules"])
    assert all(r["label"] for r in body["rules"])

    r = client.post("/api/alerts/config", json={"rules": {"low_battery": {"threshold_pct": 500}}})
    assert r.status_code == 400 and "between 5 and 95" in r.json()["detail"]
    r = client.post("/api/alerts/config", json={"enabled": True, "rules": {"low_battery": {"threshold_pct": 35}}})
    assert r.status_code == 200, r.text
    assert client.get("/api/alerts").json()["config"]["rules"]["low_battery"]["threshold_pct"] == 35


def test_push_key_and_phone_registration(client, app):
    r = client.post("/api/alerts/push_key", json={"team_id": "TEAM123456", "key_id": "KEY1234567",
                                                  "bundle_id": "com.example.jackery", "key_pem": "junk"})
    assert r.status_code == 400 and ".p8" in r.json()["detail"]
    r = client.post("/api/alerts/push_key", json={"team_id": "TEAM123456", "key_id": "KEY1234567",
                                                  "bundle_id": "com.example.jackery", "key_pem": _push_key_pem()})
    assert r.status_code == 200 and r.json()["push"]["configured"] is True
    assert "PRIVATE" not in r.text

    assert client.post("/api/app/devices", json={"install_id": "x"}).status_code == 400
    r = client.post("/api/app/devices", json={"install_id": "install-0001", "name": "Yaniv's iPhone",
                                              "environment": "development", "push_token": _PUSH_TOKEN})
    assert r.json() == {"ok": True}
    client.post("/api/app/devices", json={"install_id": "install-0001", "start_token": _START_TOKEN})
    push = client.get("/api/alerts").json()["push"]
    assert [(d["name"], d["alerts"], d["live_activity"]) for d in push["devices"]] == [("Yaniv's iPhone", True, True)]
    assert _PUSH_TOKEN not in json.dumps(push)

    assert client.delete("/api/app/devices/install-0001").json()["push"]["devices"] == []
    assert client.delete("/api/app/devices/install-0001").status_code == 404
    assert client.delete("/api/alerts/push_key").json()["push"]["configured"] is False


def test_phone_registration_requires_sign_in(app):
    with TestClient(app.app, follow_redirects=False) as c:
        assert c.post("/api/app/devices", json={"install_id": "install-0001"}).status_code == 401


def test_alerts_test_button_reports_what_apple_said(client, app, monkeypatch):
    assert "key is missing" in client.post("/api/alerts/test").json()["error"]

    async def fake_send(title, message, **kwargs):
        return None

    monkeypatch.setattr(app.apns, "send_alert", fake_send)
    assert client.post("/api/alerts/test").json() == {"ok": True, "error": None}


def _quiet_summary(state="scheduled", soc=12):
    return {"now": 0, "device_name": "Explorer", "connection": "connected", "until_ts": 9000.0,
            "battery": {"soc_pct": soc}, "power": {"grid_w": 900, "solar_w": 0, "output_w": 300},
            "headline": {"state": state, "tone": "ok", "title": "A headline.", "detail": ""}}


async def test_alerts_tick_sends_once_retries_a_failure_and_logs_it(client, app, monkeypatch):
    sent, fail = [], [True]

    async def fake_send(title, message, **kwargs):
        sent.append(kwargs["rule"])
        return "Apple refused it: InternalServerError" if fail[0] else None

    monkeypatch.setattr(app.apns, "send_alert", fake_send)
    monkeypatch.setattr(app, "_alerts_state", None)
    monkeypatch.setattr(app, "api_app_summary", _quiet_summary)

    assert await app._alerts_tick(now=1000) == []          # alerts are off by default
    app.alerts.set_config({"enabled": True})
    assert [a.rule for a in await app._alerts_tick(now=1060)] == ["low_battery"]
    fail[0] = False
    assert [a.rule for a in await app._alerts_tick(now=1120)] == ["low_battery"]   # tried again
    assert await app._alerts_tick(now=1180) == []
    assert sent == ["low_battery", "low_battery"]
    recent = client.get("/api/alerts").json()["recent"]
    assert [(r["title"], r["delivered"]) for r in recent] == [("Battery low", True), ("Battery low", False)]


async def test_live_activity_follows_charging(client, app, monkeypatch):
    calls = []

    finals = []

    async def live_start(content, **kwargs):
        calls.append(("start", content["status"], kwargs["alert"], *([True] if kwargs.get("only_missing") else [])))

    async def live_update(content, **kwargs):
        calls.append(("update", content["socPct"]))
        if content["status"] == "Charging finished":
            finals.append(content["status"])

    async def live_end(content, **kwargs):
        calls.append(("end", content["status"]))

    for name, fn in (("live_start", live_start), ("live_update", live_update), ("live_end", live_end)):
        monkeypatch.setattr(app.apns, name, fn)
    monkeypatch.setattr(app.apns, "is_configured", lambda: True)
    monkeypatch.setattr(app, "_alerts_state", None)
    now_state = {"state": "scheduled", "soc": 50}
    monkeypatch.setattr(app, "api_app_summary", lambda: _quiet_summary(now_state["state"], now_state["soc"]))

    app.alerts.set_config({"enabled": True})
    await app._alerts_tick(now=0)
    assert calls == []                                      # nothing going on
    now_state["state"] = "charging"
    await app._alerts_tick(now=60)
    await app._alerts_tick(now=120)                         # no change: no push
    now_state["soc"] = 51
    await app._alerts_tick(now=180)
    now_state["state"] = "scheduled"
    await app._alerts_tick(now=240)
    await app._alerts_tick(now=300)
    # the same card shows the final state for a while, then is ended
    assert calls == [("start", "Charging", ("Charging", "A headline.")), ("update", 51), ("update", 51)]
    assert finals == ["Charging finished"]
    await app._alerts_tick(now=240 + app.alerts.LIVE_LINGER_S)
    assert calls[3:] == [("end", "Charging finished")]

    # a hold begins while the finished card is showing: that card carries on
    del calls[:]
    now_state["state"] = "charging"
    await app._alerts_tick(now=2000)
    now_state["state"] = "scheduled"
    await app._alerts_tick(now=2060)
    now_state["state"] = "hold"
    await app._alerts_tick(now=2120)
    assert [c[0] for c in calls] == ["start", "update", "update", "start"]
    assert calls[-1][1:] == ("Charging on hold", ("Charging on hold", "A headline."), True)   # only phones without a card
    assert "end" not in [c[0] for c in calls]

    # switched off in settings: the card is ended at once, none is started
    del calls[:]
    app.alerts.set_config({"live_activity": False})
    await app._alerts_tick(now=2180)
    now_state["soc"] = 52
    await app._alerts_tick(now=2240)
    assert [c[0] for c in calls] == ["end"]


async def test_live_activity_shows_solar_and_is_replaced_when_old(client, app, monkeypatch):
    calls = []

    async def record(name, content, **kwargs):
        calls.append((name, content["status"]))

    for name in ("live_start", "live_update", "live_end"):
        monkeypatch.setattr(app.apns, name, lambda content, _n=name, **kw: record(_n, content, **kw))
    monkeypatch.setattr(app.apns, "is_configured", lambda: True)
    monkeypatch.setattr(app, "_alerts_state", None)
    summary = _quiet_summary("solar_enough", 60)
    summary["power"].update(grid_w=0, solar_w=2400)
    monkeypatch.setattr(app, "api_app_summary", lambda: summary)

    app.alerts.set_config({"enabled": True, "live_solar": False})
    await app._alerts_tick(now=0)
    assert calls == []                                      # the option is off
    app.alerts.set_config({"live_solar": True})
    await app._alerts_tick(now=60)
    assert calls == [("live_start", "Charging from solar")]
    await app._alerts_tick(now=60 + app.alerts.LIVE_MAX_AGE_S)
    assert calls[1:] == [("live_end", "Charging from solar"), ("live_start", "Charging from solar")]


async def test_a_live_activity_that_could_not_start_is_tried_again(client, app, monkeypatch):
    starts = []

    async def live_start(content, **kwargs):
        starts.append(1)
        return "No phone is registered." if len(starts) == 1 else None

    monkeypatch.setattr(app.apns, "live_start", live_start)
    monkeypatch.setattr(app.apns, "is_configured", lambda: True)
    monkeypatch.setattr(app, "_alerts_state", None)
    monkeypatch.setattr(app, "api_app_summary", lambda: _quiet_summary("charging", 50))
    app.alerts.set_config({"enabled": True})
    await app._alerts_tick(now=0)
    await app._alerts_tick(now=60)
    await app._alerts_tick(now=120)
    assert len(starts) == 2


def test_alert_facts_come_from_the_live_status(client, app):
    facts = app._alert_facts(app.api_app_summary())
    assert set(facts) == {"soc_pct", "headline", "connection", "laundry", "watchdog", "backup_last_run"}
    assert facts["laundry"] == {"blocked": False}


async def test_widgets_are_refreshed_when_the_status_changes(client, app, monkeypatch):
    reloads = []

    async def reload_widgets(**kwargs):
        reloads.append(1)

    monkeypatch.setattr(app.apns, "reload_widgets", reload_widgets)
    monkeypatch.setattr(app.apns, "is_configured", lambda: True)
    monkeypatch.setattr(app, "_alerts_state", None)
    now_state = {"state": "scheduled"}
    monkeypatch.setattr(app, "api_app_summary", lambda: _quiet_summary(now_state["state"], 50))
    await app._alerts_tick(now=0)
    await app._alerts_tick(now=60)
    assert reloads == []                                    # notifications are off; nothing changed
    now_state["state"] = "solar_enough"
    await app._alerts_tick(now=120)
    assert reloads == [1]                                   # sent even with notifications off


# ---------------------------------------------------------------------
# The iOS app's own token: opens /api/app/* and nothing else.
# ---------------------------------------------------------------------

def test_app_token_opens_only_the_app_api(client, app):
    token = client.post("/api/app/token", json={"install_id": "install-0001"}).json()["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    with TestClient(app.app, follow_redirects=False) as fresh:               # no session cookie
        assert fresh.get("/api/app/summary").status_code == 401
        assert fresh.get("/api/app/summary", headers=bearer).status_code == 200
        assert fresh.post("/api/app/devices", headers=bearer,
                          json={"install_id": "install-0001", "push_token": _PUSH_TOKEN}).status_code == 200
        # nothing outside /api/app/
        for path in ("/api/status", "/api/alerts", "/api/settings", "/"):
            assert fresh.get(path, headers=bearer).status_code in (401, 303), path
        assert fresh.post("/api/alerts/config", headers=bearer, json={"enabled": True}).status_code == 401
        # the token cannot mint another, and a wrong one opens nothing
        assert fresh.post("/api/app/token", headers=bearer, json={"install_id": "install-0001"}).status_code == 403
        assert fresh.get("/api/app/summary", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert fresh.get("/api/app/summary", headers={"Authorization": f"Basic {token}"}).status_code == 401
    # removing the phone revokes it
    client.delete("/api/app/devices/install-0001")
    with TestClient(app.app, follow_redirects=False) as fresh:
        assert fresh.get("/api/app/summary", headers=bearer).status_code == 401


async def test_a_phone_that_lost_its_card_gets_it_back_unless_it_was_swiped_away(client, app, monkeypatch):
    starts = []

    async def live_start(content, **kwargs):
        starts.append(bool(kwargs.get("only_missing")))

    async def live_update(content, **kwargs):
        return None

    monkeypatch.setattr(app.apns, "live_start", live_start)
    monkeypatch.setattr(app.apns, "live_update", live_update)
    monkeypatch.setattr(app.apns, "is_configured", lambda: True)
    monkeypatch.setattr(app, "_alerts_state", None)
    monkeypatch.setattr(app, "api_app_summary", lambda: _quiet_summary("charging", 50))
    app.alerts.set_config({"enabled": True})
    await app._alerts_tick(now=1000)
    assert starts == [False]

    # the app starts up after an update and finds no card
    client.post("/api/app/devices", json={"install_id": "install-0001", "activity_token": "", "activity_gone": "launch"})
    await app._alerts_tick(now=1060)
    assert starts == [False, True]
    await app._alerts_tick(now=1120)
    assert starts == [False, True]                                      # asked once, restarted once

    # the owner swipes it away: a later launch does not bring it back this session
    client.post("/api/app/devices", json={"install_id": "install-0001", "activity_token": "", "activity_gone": "dismissed"})
    client.post("/api/app/devices", json={"install_id": "install-0001", "activity_token": "", "activity_gone": "launch"})
    await app._alerts_tick(now=1180)
    assert starts == [False, True]


async def test_morning_summary_is_sent_once_with_its_parts(client, app, monkeypatch):
    sent = []

    async def fake_send(title, message, **kwargs):
        sent.append((title, message, kwargs.get("page")))

    async def fake_forecast(sn):
        return {"forecast": [{"ts": 100 * 86400 + 12 * 3600, "duration_h": 1, "solar_w": 2500},
                             {"ts": 101 * 86400 + 12 * 3600, "duration_h": 1, "solar_w": 9000}]}

    monkeypatch.setattr(app.apns, "send_alert", fake_send)
    monkeypatch.setattr(app, "_build_and_record_forecast", fake_forecast)
    monkeypatch.setattr(app.device_location, "get_tz_offset", lambda: 0)
    monkeypatch.setattr(app, "_alerts_state", None)
    monkeypatch.setattr(app, "api_app_summary", lambda: {**_quiet_summary("scheduled", 61), "device_sn": "MOCK"})
    app.alerts.set_config({"enabled": True, "daily_summary": {"enabled": True, "hour": 7},
                           "rules": {"low_battery": {"enabled": False}}})
    await app._alerts_tick(now=100 * 86400 + 6 * 3600)
    assert sent == []
    await app._alerts_tick(now=100 * 86400 + 7 * 3600 + 5)
    await app._alerts_tick(now=100 * 86400 + 7 * 3600 + 65)
    assert len(sent) == 1
    title, message, page = sent[0]
    assert title == "Jackery this morning" and page == "live"
    assert message.startswith("Battery at 61%. Solar expected today: 2.5 kWh.")
    assert client.get("/api/alerts").json()["recent"][0]["rule"] == "daily_summary"

