"""Monitoring-only capability guards without DB, credentials or API traffic."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

import advisor_routes as advisor
import claude_advisor


class FakeEnergy:
    def __init__(self):
        self.writes = []

    def get_suggestion(self, suggestion_id):
        return {
            "id": suggestion_id,
            "device_sn": "HOME",
            "status": "pending",
            "kind": "config",
            "target": "smart_charge.example",
            "proposed_value": 5,
            "reasoning": "fake",
        }

    def record_change(self, **kwargs):
        self.writes.append(kwargs)

    def update_suggestion_status(self, *args):
        self.writes.append(args)

    def expire_old_suggestions(self):
        self.writes.append("expire")

    def list_devices(self):
        return [{"device_sn": sn} for sn in ("HOME", "PORTABLE", "UNKNOWN")]


@pytest.fixture
def context():
    def forbidden(*args, **kwargs):
        raise AssertionError("portable capacity/model data must not be accessed")

    helpers = advisor.AdvisorHelpers(forbidden, forbidden, forbidden)
    helpers.is_read_only = lambda sn: sn != "PORTABLE"
    state = SimpleNamespace(
        energy=FakeEnergy(),
        device=SimpleNamespace(device_sn="HOME"),
        last_status={},
        advisor_jobs={},
        last_advisor_run_by_sn={},
    )
    app = FastAPI()
    advisor.install(app, state, helpers)
    return state, helpers, {r.path: r.endpoint for r in app.routes}


def assert_monitoring_only(error):
    assert error.value.status_code == 501
    assert "monitoring only" in error.value.detail


async def test_bundle_blocks_home_before_portable_data_access(context):
    state, helpers, _ = context
    with pytest.raises(HTTPException) as error:
        await advisor._build_advisor_bundle(state, helpers, "HOME")
    assert_monitoring_only(error)


def test_tool_builder_blocks_home_before_capacity_hints(context):
    state, helpers, _ = context
    with pytest.raises(HTTPException) as error:
        advisor._make_advisor_query_fn(state, helpers, "HOME")
    assert_monitoring_only(error)


async def test_direct_review_blocks_before_provider_key_or_api_access(context, monkeypatch):
    state, helpers, _ = context

    def forbidden():
        raise AssertionError("provider credentials must not be accessed")

    monkeypatch.setattr(claude_advisor, "has_usable_key", forbidden)
    with pytest.raises(HTTPException) as error:
        await advisor._run_advisor_review(state, helpers, "HOME")
    assert_monitoring_only(error)
    assert state.energy.writes == []


@pytest.mark.parametrize("path", ["/api/algorithm/review_now", "/api/algorithm/preview"])
async def test_home_routes_block_before_jobs_or_bundle(context, monkeypatch, path):
    state, helpers, routes = context
    spawned = []

    def spawn(coro):
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(advisor.asyncio, "create_task", spawn)

    async def forbidden(*args):
        raise AssertionError("no bundle should be built")

    monkeypatch.setattr(advisor, "_build_advisor_bundle", forbidden)
    with pytest.raises(HTTPException) as error:
        await routes[path]()
    assert_monitoring_only(error)
    assert spawned == []
    assert state.advisor_jobs == {}


async def test_apply_blocks_home_before_config_or_audit_writes(context, monkeypatch):
    state, _, routes = context
    config_writes = []
    monkeypatch.setattr(
        claude_advisor,
        "ALLOWED_TARGETS",
        {"smart_charge.example": {"min": 0, "max": 10, "scope": "device"}},
    )
    monkeypatch.setattr(advisor.smart_charge, "get_config", lambda sn: {"example": 0})
    monkeypatch.setattr(advisor.smart_charge, "set_config", lambda *a, **k: config_writes.append(k))
    with pytest.raises(HTTPException) as error:
        await routes["/api/algorithm/suggestions/{suggestion_id}/apply"](1)
    assert_monitoring_only(error)
    assert config_writes == []
    assert state.energy.writes == []


async def test_portable_suggestion_can_still_be_applied(context, monkeypatch):
    state, helpers, routes = context
    helpers.is_read_only = lambda sn: False
    config_writes = []
    monkeypatch.setattr(
        claude_advisor,
        "ALLOWED_TARGETS",
        {"smart_charge.example": {"min": 0, "max": 10, "scope": "device"}},
    )
    monkeypatch.setattr(advisor.smart_charge, "get_config", lambda sn: {"example": 0})
    monkeypatch.setattr(
        advisor.smart_charge, "set_config", lambda cfg, **k: config_writes.append(cfg)
    )
    result = await routes["/api/algorithm/suggestions/{suggestion_id}/apply"](1)
    assert result["ok"] is True
    assert config_writes == [{"example": 5}]
    assert len(state.energy.writes) == 2


async def test_already_built_query_stops_after_capability_changes(context):
    state, helpers, _ = context
    helpers.is_read_only = lambda sn: False
    helpers.capacity_hints = lambda sn: (3024, 0)
    query = advisor._make_advisor_query_fn(state, helpers, "HOME")
    helpers.is_read_only = lambda sn: True
    result = await query("query_samples", {})
    assert "monitoring only" in result["error"]


async def test_loop_skips_home_and_unknown_without_stamping_last_run(context, monkeypatch):
    state, helpers, _ = context
    reviewed = []

    async def review(state, helpers, sn):
        reviewed.append(sn)
        return {"ok": True}

    async def sleep(seconds):
        if seconds != 60:
            raise asyncio.CancelledError

    monkeypatch.setattr(advisor, "_run_advisor_review", review)
    monkeypatch.setattr(advisor.asyncio, "sleep", sleep)
    monkeypatch.setattr(advisor.time, "time", lambda: 86400 + 8 * 3600)
    monkeypatch.setattr(advisor.device_location, "get_tz_offset", lambda: 0)
    monkeypatch.setattr(advisor.user_settings, "get", lambda key: 8)
    monkeypatch.setattr(claude_advisor, "has_usable_key", lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await advisor.advisor_loop(state, helpers)
    assert reviewed == ["PORTABLE"]
    assert set(state.last_advisor_run_by_sn) == {"PORTABLE"}


async def test_capability_change_during_review_prevents_persistence(context, monkeypatch):
    state, helpers, _ = context
    helpers.is_read_only = lambda sn: False

    async def bundle(*args):
        return {}

    async def review(*args, **kwargs):
        helpers.is_read_only = lambda sn: True
        return {"config_suggestions": [], "anomalies": []}

    monkeypatch.setattr(advisor, "_build_advisor_bundle", bundle)
    monkeypatch.setattr(advisor, "_make_advisor_query_fn", lambda *a: None)
    monkeypatch.setattr(claude_advisor, "has_usable_key", lambda: True)
    monkeypatch.setattr(claude_advisor, "review", review)
    with pytest.raises(HTTPException) as error:
        await advisor._run_advisor_review(state, helpers, "HOME")
    assert_monitoring_only(error)
    assert state.energy.writes == []


def test_portable_helper_default_is_backward_compatible():
    helpers = advisor.AdvisorHelpers(lambda *a: 3024, lambda sn: (3024, 0), lambda *a: 50)
    assert helpers.is_read_only("PORTABLE") is False
