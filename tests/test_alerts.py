"""Alert rules, the send-once state machine, config validation and the Live Activity plan."""

from __future__ import annotations

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
    assert alerts.get_config()["enabled"] is False and alerts.get_config()["live_activity"] is True
    saved = alerts.set_config({"enabled": True, "live_activity": False,
                               "rules": {"low_battery": {"threshold_pct": "30"}, "offline": {"enabled": False}}})
    assert saved["rules"]["low_battery"] == {"enabled": True, "threshold_pct": 30}
    again = alerts.get_config()
    assert again["enabled"] and again["live_activity"] is False
    assert again["rules"]["offline"]["enabled"] is False and again["rules"]["laundry_hold"]["after_min"] == 120


@pytest.mark.parametrize("patch", [
    {"rules": {"low_battery": {"threshold_pct": 0}}},
    {"rules": {"low_battery": {"threshold_pct": "lots"}}},
    {"rules": {"nonsense": {"enabled": True}}},
])
def test_config_rejects_what_would_not_work(isolated_data, patch):
    with pytest.raises(ValueError):
        alerts.set_config(patch)
    assert alerts.get_config() == alerts.default_config()


def test_an_undelivered_alert_is_tried_again_and_logged_once():
    state = {"memory": {}, "recent": []}
    current = {"low_battery": _alert()}
    for now in (100, 160, 220):
        (alert,) = alerts.step(state["memory"], current, now)
        alerts.record(state, alert, now, "Could not reach Apple: ConnectError")
        alerts.undelivered(state["memory"], alert)
    assert len(state["recent"]) == 1 and state["recent"][0]["ts"] == 220
    (alert,) = alerts.step(state["memory"], current, 280)
    alerts.record(state, alert, 280, None)
    assert [r["delivered"] for r in state["recent"]] == [True, False]
    assert alerts.step(state["memory"], current, 340) == []


# ---- Live Activity ----

def _summary(state="charging", soc=41.6, until=5000.0, grid=1200, title="Charging from the grid at $0.25 until 3 PM."):
    return {"battery": {"soc_pct": soc}, "power": {"grid_w": grid, "solar_w": 0, "output_w": 430.4},
            "headline": {"state": state, "tone": "ok", "title": title}, "until_ts": until}


def test_live_content_only_exists_while_something_is_going_on():
    content = alerts.live_content(_summary())
    assert content == {"socPct": 42, "state": "charging", "status": "Charging",
                       "title": "Charging from the grid at $0.25 until 3 PM.", "tone": "ok",
                       "gridW": 1200, "solarW": 0, "outputW": 430, "untilTs": 5000.0}
    assert alerts.live_content(_summary(state="hold"))["status"] == "Charging on hold"
    assert alerts.live_content(_summary(state="scheduled")) is None
    assert alerts.live_content({"battery": {}, "power": {}, "headline": None}) is None


def test_live_content_for_an_ending_activity_says_so():
    final = alerts.live_content(_summary(state="scheduled", title="Not charging. Next charge starts at 12 AM."), final=True)
    assert final["status"] == "Charging finished" and final["untilTs"] is None and final["socPct"] == 42


def test_live_step_starts_updates_on_change_refreshes_and_ends():
    memory: dict = {}
    first = alerts.live_content(_summary())
    assert alerts.live_step(memory, None, 0) is None
    assert alerts.live_step(memory, first, 0) == "start"
    assert alerts.live_step(memory, first, 60) is None
    # power alone is not worth a push...
    assert alerts.live_step(memory, alerts.live_content(_summary(grid=900)), 120) is None
    # ...but it rides along on the periodic refresh
    assert alerts.live_step(memory, alerts.live_content(_summary(grid=900)), 600) == "update"
    assert memory["content"]["gridW"] == 900
    # a new level or status goes out at once
    assert alerts.live_step(memory, alerts.live_content(_summary(soc=43.2)), 660) == "update"
    assert alerts.live_step(memory, alerts.live_content(_summary(soc=43.2, state="charging_peak")), 720) == "update"
    assert alerts.live_step(memory, None, 780) == "end" and memory == {}
    assert alerts.live_step(memory, None, 840) is None
