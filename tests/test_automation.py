"""Unit tests for the automation rule engine.

Edge-trigger semantics, retry-on-failure, per-Jackery-device routing.
kasa_client.set_state is monkey-patched so no real network calls happen.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def engine_with_fake_kasa(isolated_data, monkeypatch):
    """Reload the automation module against the isolated /data and
       patch kasa_client.set_state so we can introspect what would be called."""
    import crypto_util, kasa_client, automation
    importlib.reload(crypto_util)
    importlib.reload(kasa_client)
    importlib.reload(automation)

    calls: list[tuple[str, bool]] = []
    fail_next: list[Exception] = []

    async def fake_set_state(host: str, on: bool):
        if fail_next:
            raise fail_next.pop(0)
        calls.append((host, on))
        return {"host": host, "on": on}

    monkeypatch.setattr(kasa_client, "set_state", fake_set_state)
    return automation.AutomationEngine(), calls, fail_next


# ---------- _matches operator semantics ----------
def test_matches_lt():
    from automation import _matches
    assert _matches({"operator": "<", "value": 20}, 19.9) is True
    assert _matches({"operator": "<", "value": 20}, 20.0) is False
    assert _matches({"operator": "<", "value": 20}, 50) is False


def test_matches_lte():
    from automation import _matches
    assert _matches({"operator": "<=", "value": 20}, 20) is True
    assert _matches({"operator": "<=", "value": 20}, 21) is False


def test_matches_eq_with_tolerance():
    from automation import _matches, EQUALS_TOLERANCE
    assert _matches({"operator": "=", "value": 50}, 50.0) is True
    assert _matches({"operator": "=", "value": 50}, 50.0 + EQUALS_TOLERANCE) is True
    assert _matches({"operator": "=", "value": 50}, 50.0 + EQUALS_TOLERANCE + 0.01) is False


def test_matches_gte_and_gt():
    from automation import _matches
    assert _matches({"operator": ">=", "value": 80}, 80) is True
    assert _matches({"operator": ">=", "value": 80}, 79.99) is False
    assert _matches({"operator": ">",  "value": 80}, 80.01) is True
    assert _matches({"operator": ">",  "value": 80}, 80) is False


def test_matches_unknown_operator_returns_false():
    from automation import _matches
    assert _matches({"operator": "BAD", "value": 50}, 50) is False


# ---------- _validate ----------
def test_validate_rejects_bad_operator(isolated_data):
    import automation
    importlib.reload(automation)
    with pytest.raises(automation.AutomationError):
        automation._validate({
            "operator": "approximately",
            "value": 20,
            "action": "off",
            "kasa_host": "1.2.3.4",
        })


def test_validate_rejects_bad_action(isolated_data):
    import automation
    importlib.reload(automation)
    with pytest.raises(automation.AutomationError):
        automation._validate({
            "operator": "<",
            "value": 20,
            "action": "explode",
            "kasa_host": "1.2.3.4",
        })


def test_validate_rejects_missing_host(isolated_data):
    import automation
    importlib.reload(automation)
    with pytest.raises(automation.AutomationError):
        automation._validate({
            "operator": "<",
            "value": 20,
            "action": "off",
            "kasa_host": "",
        })


def test_validate_assigns_id_when_missing(isolated_data):
    import automation
    importlib.reload(automation)
    rule = automation._validate({
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
    })
    assert "id" in rule and len(rule["id"]) == 8


# ---------- evaluate ----------
@pytest.mark.asyncio
async def test_evaluate_fires_on_edge_transition(engine_with_fake_kasa):
    eng, calls, _fail = engine_with_fake_kasa
    eng.upsert({
        "name": "low-batt off",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
        "jackery_device_sn": "A",
    })

    # SOC well above threshold → no edge, no fire.
    fired = await eng.evaluate({"A": 50}, active_sn="A")
    assert fired == [] and calls == []

    # Drop below threshold → edge transition → fires once.
    fired = await eng.evaluate({"A": 15}, active_sn="A")
    assert len(fired) == 1
    assert calls == [("1.2.3.4", False)]

    # Stay below → no re-fire.
    fired = await eng.evaluate({"A": 10}, active_sn="A")
    assert fired == []
    assert calls == [("1.2.3.4", False)]

    # Climb back above → state resets.
    await eng.evaluate({"A": 50}, active_sn="A")
    # Cross again → fires again.
    fired = await eng.evaluate({"A": 15}, active_sn="A")
    assert len(fired) == 1
    assert calls == [("1.2.3.4", False), ("1.2.3.4", False)]


@pytest.mark.asyncio
async def test_evaluate_retries_on_action_failure(engine_with_fake_kasa):
    """Failed action must NOT consume the edge — retry on next eval."""
    eng, calls, fail_next = engine_with_fake_kasa
    eng.upsert({
        "name": "test",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
        "jackery_device_sn": "A",
    })
    # Prime: prior reading high so SOC=15 is a transition.
    await eng.evaluate({"A": 50}, active_sn="A")

    # First eval at low SOC: action fails.
    fail_next.append(RuntimeError("device offline"))
    fired = await eng.evaluate({"A": 15}, active_sn="A")
    assert fired == []  # no successful fires
    assert calls == []  # action threw, nothing recorded

    # Second eval at low SOC: action succeeds → fires now.
    fired = await eng.evaluate({"A": 15}, active_sn="A")
    assert len(fired) == 1
    assert calls == [("1.2.3.4", False)]


@pytest.mark.asyncio
async def test_evaluate_routes_to_correct_device(engine_with_fake_kasa):
    eng, calls, _ = engine_with_fake_kasa
    eng.upsert({
        "name": "5K low",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
        "jackery_device_sn": "FIVE_K",
    })
    eng.upsert({
        "name": "HP3 high",
        "operator": ">=", "value": 80, "action": "on",
        "kasa_host": "5.6.7.8",
        "jackery_device_sn": "HP_3K",
    })

    # Prime both rules' edge state at non-matching values.
    await eng.evaluate({"FIVE_K": 50, "HP_3K": 50}, active_sn="FIVE_K")
    assert calls == []

    # 5K crosses low; HP3 still mid → only first rule fires.
    await eng.evaluate({"FIVE_K": 15, "HP_3K": 50}, active_sn="FIVE_K")
    assert calls == [("1.2.3.4", False)]

    # HP3 crosses high; 5K still low (no edge, already fired) → only HP3 fires.
    await eng.evaluate({"FIVE_K": 15, "HP_3K": 85}, active_sn="FIVE_K")
    assert calls == [("1.2.3.4", False), ("5.6.7.8", True)]


@pytest.mark.asyncio
async def test_evaluate_skips_disabled_rules(engine_with_fake_kasa):
    eng, calls, _ = engine_with_fake_kasa
    rule = eng.upsert({
        "name": "test",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
        "jackery_device_sn": "A",
        "enabled": False,
    })
    await eng.evaluate({"A": 50}, active_sn="A")
    fired = await eng.evaluate({"A": 15}, active_sn="A")
    assert fired == [] and calls == []


@pytest.mark.asyncio
async def test_evaluate_handles_missing_device_data(engine_with_fake_kasa):
    """Rule for device 'A' shouldn't fire (or crash) when only 'B' has data."""
    eng, calls, _ = engine_with_fake_kasa
    eng.upsert({
        "name": "test",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
        "jackery_device_sn": "A",
    })
    fired = await eng.evaluate({"B": 5}, active_sn="B")
    assert fired == [] and calls == []


# ---------- CRUD ----------
def test_crud(isolated_data):
    import automation
    importlib.reload(automation)
    eng = automation.AutomationEngine()
    assert eng.list_rules() == []
    rule = eng.upsert({
        "name": "test",
        "operator": "<", "value": 20, "action": "off",
        "kasa_host": "1.2.3.4",
    })
    assert len(eng.list_rules()) == 1
    # Update by id
    eng.upsert({**rule, "name": "renamed"})
    assert eng.list_rules()[0]["name"] == "renamed"
    # Delete
    assert eng.delete(rule["id"]) is True
    assert eng.list_rules() == []
    # Delete nonexistent
    assert eng.delete("nope") is False
