import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture()
def app(isolated_data, monkeypatch):
    monkeypatch.setenv("JACKERY_LG_THINQ_CREDS_FILE", str(isolated_data / "lg.json"))
    import crypto_util

    importlib.reload(crypto_util)
    import lg_thinq

    importlib.reload(lg_thinq)
    import lg_thinq_routes

    importlib.reload(lg_thinq_routes)
    application = FastAPI()
    application.include_router(lg_thinq_routes.router)
    return application, lg_thinq


async def test_connection_status_never_returns_token_and_invalid_replacement_preserves_old(
    app, monkeypatch
):
    application, lg = app
    token = "thinqpat_" + "mock-test-value" * 4
    api = AsyncMock()
    api.async_get_device_list.return_value = []
    monkeypatch.setattr(lg, "make_api", lambda *args: api)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        saved = await client.post(
            "/api/lg_thinq/credentials", json={"token": token, "country_code": "US"}
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert token not in saved.text
        api.async_get_device_list.side_effect = RuntimeError(token)
        rejected = await client.post(
            "/api/lg_thinq/credentials", json={"token": token + "replacement", "country_code": "US"}
        )
        assert rejected.status_code == 400
        assert token not in rejected.text
        assert lg.load_credentials()["token"] == token
        status = await client.get("/api/lg_thinq/status")
        assert token not in status.text
        deleted = await client.delete("/api/lg_thinq/credentials")
        assert deleted.json()["configured"] is False


async def test_status_exposes_configured_protection_and_warnings_without_token(app, monkeypatch, tmp_path):
    application, lg = app
    import smart_charge

    monkeypatch.setenv("JACKERY_SMART_CHARGE_FILE", str(tmp_path / "smart.json"))
    importlib.reload(smart_charge)
    smart_charge.set_config({"lg_interlock_enabled": True, "lg_device_ids": ["dryer"]}, "MAIN")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/api/lg_thinq/status")
    assert response.status_code == 200
    status = response.json()
    assert status["protection_enabled"] and not status["configured"]
    assert status["warnings"]
    assert "token" not in status
    assert lg.block_reason(smart_charge.get_config("MAIN")) is None


@pytest.mark.parametrize(
    "body",
    [
        {"token": "private-invalid-value"},
        {"token": ["private-invalid-value"]},
        {"token": "thinqpat_" + "a" * 40, "country_code": "../secret"},
    ],
)
async def test_validation_never_echoes_secret_input(app, body):
    application, _ = app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post("/api/lg_thinq/credentials", json=body)
    assert response.status_code == 400
    assert "private-invalid-value" not in response.text
    assert "thinqpat_" not in response.text


async def test_all_lg_routes_require_existing_app_authentication(app, isolated_data, monkeypatch):
    application, _ = app
    import auth

    importlib.reload(auth)
    import api_auth

    importlib.reload(api_auth)
    import cf_access

    monkeypatch.setattr(cf_access, "is_configured", lambda: False)
    api_auth.install(application, Path("web"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for method, path in (
            ("GET", "status"),
            ("POST", "refresh"),
            ("POST", "credentials"),
            ("DELETE", "credentials"),
        ):
            response = await client.request(method, "/api/lg_thinq/" + path)
            assert response.status_code in (401, 403, 503)
