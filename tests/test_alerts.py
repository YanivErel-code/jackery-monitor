"""Alert rules, the send-once state machine, config validation and ntfy delivery."""

from __future__ import annotations

import json

import httpx
import pytest

import alerts


@pytest.fixture()
def config(isolated_data):
    return alerts.default_config()


def _facts(**over):
    facts = {"soc_pct": 60, "connection": "connected",
             "headline": {"state": "solar_enough", "tone": "ok", "title": "No grid charging needed.", "detail": ""},
             "laundry": {"blocked": False}, "watchdog": None, "backup_last_run": None}
    facts.update(over)
    return facts


def test_nothing_to_report_when_all_is_well(config):
    assert alerts.conditions(_facts(), config) == {}


def test_low_battery_fires_at_threshold_and_names_the_level(config):
    found = alerts.conditions(_facts(soc_pct=20), config)
    assert list(found) == ["low_battery"]
    assert "20%" in found["low_battery"].message and found["low_battery"].priority == "high"
    assert alerts.conditions(_facts(soc_pct=21), config) == {}


def test_low_battery_stays_on_until_recovered_by_a_margin(config):
    assert "low_battery" in alerts.conditions(_facts(soc_pct=23), config, {"low_battery"})
    assert alerts.conditions(_facts(soc_pct=25), config, {"low_battery"}) == {}


def test_missing_battery_level_is_not_low(config):
    assert alerts.conditions(_facts(soc_pct=None), config) == {}


@pytest.mark.parametrize("headline, rule", [
    ({"state": "not_charging", "tone": "alert", "title": "The plug is on but the battery isn't charging.",
      "detail": "Check the charger cable."}, "charge_failed"),
    ({"state": "hold", "tone": "alert", "title": "Charging is on hold while the dryer runs.",
      "detail": "On course to fall below the 10% reserve."}, "reserve_risk"),
    ({"state": "scheduled", "tone": "alert", "title": "Not charging.", "detail": "The plan cannot keep the reserve."},
     "reserve_risk"),
])
def test_headline_alerts_reuse_the_dashboard_wording(config, headline, rule):
    found = alerts.conditions(_facts(headline=headline, laundry={"blocked": False}), config)
    assert list(found) == [rule]
    assert headline["title"] in found[rule].message and headline["detail"] in found[rule].message


def test_a_warning_tone_is_not_an_alert(config):
    headline = {"state": "charging_peak", "tone": "warn", "title": "Charging at peak.", "detail": ""}
    assert alerts.conditions(_facts(headline=headline), config) == {}


def test_inverter_trip_is_urgent(config):
    found = alerts.conditions(_facts(watchdog={"hw_trip_active": True, "error_message": None}), config)
    assert found["inverter_trip"].priority == "urgent"
    found = alerts.conditions(_facts(watchdog={"hw_trip_active": False, "error_message": "Gave up after 3 tries"}), config)
    assert found["inverter_trip"].message == "Gave up after 3 tries"
    assert alerts.conditions(_facts(watchdog={"hw_trip_active": False, "error_message": None}), config) == {}


def test_laundry_hold_and_offline_wait_for_their_delay(config):
    found = alerts.conditions(_facts(laundry={"blocked": True}, connection="error"), config)
    assert found["laundry_hold"].delay_s == 120 * 60 and "2 hours" in found["laundry_hold"].title
    assert found["offline"].delay_s == 15 * 60 and "15 minutes" in found["offline"].message


def test_backup_failure_carries_the_run_as_its_key(config):
    found = alerts.conditions(_facts(backup_last_run={"ok": False, "ts": 111, "error": "smb_failed: timeout"}), config)
    assert found["backup_failed"].key == "111" and "smb_failed" in found["backup_failed"].message
    assert alerts.conditions(_facts(backup_last_run={"ok": True, "ts": 112}), config) == {}


def test_a_disabled_rule_never_fires(config):
    config["rules"]["low_battery"]["enabled"] = False
    assert alerts.conditions(_facts(soc_pct=5), config) == {}


# ---- step(): send once, re-arm when cleared ----

def _alert(rule="low_battery", delay_s=0.0, key=""):
    return alerts.Alert(rule, "t", "m", delay_s=delay_s, key=key)


def test_step_sends_once_then_stays_quiet_then_rearms():
    memory: dict = {}
    assert [a.rule for a in alerts.step(memory, {"low_battery": _alert()}, 100)] == ["low_battery"]
    assert alerts.step(memory, {"low_battery": _alert()}, 160) == []
    assert alerts.active_rules(memory) == {"low_battery"}
    assert alerts.step(memory, {}, 220) == [] and memory == {}
    assert len(alerts.step(memory, {"low_battery": _alert()}, 280)) == 1


def test_step_waits_out_the_delay_and_forgets_a_condition_that_cleared_early():
    memory: dict = {}
    held = {"offline": _alert("offline", delay_s=900)}
    assert alerts.step(memory, held, 0) == []
    assert alerts.active_rules(memory) == set()
    assert alerts.step(memory, held, 899) == []
    assert alerts.step(memory, {}, 900) == []          # came back before the delay ran out
    assert alerts.step(memory, held, 1000) == []       # the clock starts again
    assert len(alerts.step(memory, held, 1900)) == 1


def test_step_alerts_again_when_the_key_changes():
    memory: dict = {}
    assert len(alerts.step(memory, {"backup_failed": _alert("backup_failed", key="1")}, 0)) == 1
    assert alerts.step(memory, {"backup_failed": _alert("backup_failed", key="1")}, 60) == []
    assert len(alerts.step(memory, {"backup_failed": _alert("backup_failed", key="2")}, 120)) == 1


def test_memory_survives_a_json_round_trip(isolated_data):
    state = alerts.load_state()
    alerts.step(state["memory"], {"low_battery": _alert()}, 100)
    alerts.record(state, _alert(), 100, None)
    alerts.save_state(state)
    again = alerts.load_state()
    assert alerts.step(again["memory"], {"low_battery": _alert()}, 160) == []
    assert again["recent"][0]["rule"] == "low_battery" and again["recent"][0]["delivered"] is True


# ---- config ----

def test_config_defaults_then_merges_and_persists(isolated_data):
    assert alerts.get_config()["enabled"] is False
    saved = alerts.set_config({"ntfy_topic": "jackery-abc123", "enabled": True,
                               "rules": {"low_battery": {"threshold_pct": "30"}, "offline": {"enabled": False}}})
    assert saved["rules"]["low_battery"] == {"enabled": True, "threshold_pct": 30}
    again = alerts.get_config()
    assert again["enabled"] and again["ntfy_topic"] == "jackery-abc123"
    assert again["rules"]["offline"]["enabled"] is False and again["rules"]["laundry_hold"]["after_min"] == 120


@pytest.mark.parametrize("patch", [
    {"enabled": True},                                        # no topic yet
    {"ntfy_topic": "has spaces"},
    {"ntfy_topic": "x/../y"},
    {"ntfy_server": "ntfy.sh"},
    {"ntfy_server": "https://ntfy.sh/path"},
    {"rules": {"low_battery": {"threshold_pct": 0}}},
    {"rules": {"low_battery": {"threshold_pct": "lots"}}},
    {"rules": {"nonsense": {"enabled": True}}},
])
def test_config_rejects_what_would_not_work(isolated_data, patch):
    with pytest.raises(ValueError):
        alerts.set_config(patch)
    assert alerts.get_config() == alerts.default_config()


# ---- delivery ----

async def test_send_ntfy_posts_the_alert(config):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    config["ntfy_topic"] = "jackery-abc123"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        error = await alerts.send_ntfy(
            config, alerts.Alert("low_battery", "Battery low", "At 19%.", priority="high", tag="low_battery"),
            client=client)
    assert error is None
    assert seen["url"] == "https://ntfy.sh"
    assert seen["body"] == {"topic": "jackery-abc123", "title": "Battery low", "message": "At 19%.",
                            "priority": 4, "tags": ["low_battery"]}


async def test_send_ntfy_reports_failure_without_raising(config):
    assert await alerts.send_ntfy(config, _alert()) == "No ntfy topic is set."
    config["ntfy_topic"] = "jackery-abc123"

    def refuse(request):
        return httpx.Response(429)

    def drop(request):
        raise httpx.ConnectError("no route")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        assert "429" in await alerts.send_ntfy(config, _alert(), client=client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(drop)) as client:
        assert "Could not reach ntfy" in await alerts.send_ntfy(config, _alert(), client=client)
