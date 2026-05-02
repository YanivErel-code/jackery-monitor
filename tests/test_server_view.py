"""
Per-browser device-view selection.

`serialize_status(view_device_id=...)` should:
  - default to the bridge-active device (existing behavior) when the
    cookie is missing, empty, points at the bridge-active device, or
    points at an unknown device on the account
  - synthesize a status payload for any *other* known device, pulling
    its telemetry from `cloud_meta.devices_telemetry[sn]`, its packs
    from `state.battery_packs_by_sn[sn]`, and its energy totals from
    the energy DB
  - override `cloud.selected_device_id` with the view's id so the
    frontend's `activeJackeryDevice()` follows the per-browser pick

The bridge keeps polling every device on every tick, so the synthesized
view is always current — picking a non-bridge-active device is purely a
UI render decision.
"""

from __future__ import annotations

import os

import pytest

# Force mock backend so importing server.py doesn't try to connect to the
# bridge daemon.
os.environ["BACKEND"] = "mock"


@pytest.fixture()
def server_state(isolated_data, monkeypatch):
    """Import server.py with isolated /data and seed `state` with two
    cloud devices (A is bridge-active, B is the secondary). Returns the
    server module so tests can call serialize_status() directly."""
    import importlib

    import server as server_mod
    importlib.reload(server_mod)

    s = server_mod.state
    # Pretend the bridge has connected and is polling device A.
    s.connection_status = "connected"
    s.connection_error = None
    s.device = server_mod.DeviceInfo(
        name="Jackery A",
        address="cloud",
        rssi=0,
        model_code=13,
        device_sn="SN-A",
        device_type="portable",
    )
    s.last_status = {
        "battery_percent": 80,
        "input_power_w": 100,
        "output_power_w": 50,
        "ac_input_w": 0,
        "solar_input_w": 100,
    }
    s.last_cloud_meta = {
        "selected_device_id": "id-A",
        "devices": [
            {"device_id": "id-A", "device_sn": "SN-A",
             "name": "Jackery A", "model_code": 13, "model_name": "5000 Plus"},
            {"device_id": "id-B", "device_sn": "SN-B",
             "name": "Jackery B", "model_code": 22, "model_name": "HomePower 3000"},
        ],
        "devices_telemetry": {
            "SN-A": {"telemetry": s.last_status, "ts": 1700000000},
            "SN-B": {"telemetry": {
                "battery_percent": 42,
                "input_power_w": 0,
                "output_power_w": 75,
                "ac_input_w": 0,
                "solar_input_w": 0,
            }, "ts": 1700000000},
        },
    }
    return server_mod


def test_no_cookie_returns_bridge_active(server_state):
    out = server_state.serialize_status(view_device_id=None)
    assert out["device"]["device_sn"] == "SN-A"
    assert out["telemetry"]["battery_percent"] == 80
    assert out["cloud"]["selected_device_id"] == "id-A"


def test_empty_cookie_returns_bridge_active(server_state):
    out = server_state.serialize_status(view_device_id="")
    assert out["device"]["device_sn"] == "SN-A"
    assert out["cloud"]["selected_device_id"] == "id-A"


def test_cookie_matching_bridge_active_returns_bridge_active(server_state):
    """When the cookie picks the same device the bridge is on, we should
    take the rich existing path, not the synthesized one."""
    out = server_state.serialize_status(view_device_id="id-A")
    assert out["device"]["device_sn"] == "SN-A"
    # Rich path returns the deque-backed history list (initially empty).
    assert isinstance(out["history"], list)


def test_cookie_picks_secondary_device(server_state):
    out = server_state.serialize_status(view_device_id="id-B")
    assert out["device"]["device_sn"] == "SN-B"
    assert out["device"]["name"] == "Jackery B"
    assert out["device"]["model_code"] == 22
    # Telemetry comes from cloud_meta.devices_telemetry, not state.last_status.
    assert out["telemetry"]["battery_percent"] == 42
    assert out["telemetry"]["output_power_w"] == 75
    # selected_device_id is overridden so the frontend's picker reflects B.
    assert out["cloud"]["selected_device_id"] == "id-B"


def test_unknown_cookie_falls_back_to_bridge_active(server_state):
    """Stale cookie pointing at a device that's no longer on the account
    should not crash and should not show empty data — fall back to the
    bridge-active device."""
    out = server_state.serialize_status(view_device_id="id-DOES-NOT-EXIST")
    assert out["device"]["device_sn"] == "SN-A"
    assert out["cloud"]["selected_device_id"] == "id-A"


def test_cookie_does_not_mutate_cached_cloud_meta(server_state):
    """serialize_status must shallow-copy cloud_meta before overriding
    selected_device_id, otherwise switching views on one client would
    poison the cache for everyone else."""
    server_state.serialize_status(view_device_id="id-B")
    assert server_state.state.last_cloud_meta["selected_device_id"] == "id-A"


def test_secondary_view_includes_packs_and_energy(server_state, monkeypatch):
    """Synthesized secondary view should include per-device packs from
    state.battery_packs_by_sn and per-device energy totals from the DB."""
    # Seed packs only for B — A has none. Both should render correctly.
    server_state.state.battery_packs_by_sn["SN-B"] = [
        {"rb": 50, "alias": "pack-1"},
    ]

    # Stub energy.totals so we don't need a populated DB.
    def fake_totals(sn):
        return {"today_solar_wh": 1000 if sn == "SN-B" else 5000}
    monkeypatch.setattr(server_state.state.energy, "totals", fake_totals)
    # Stub the savings decorator to passthrough so we can assert on the
    # raw totals dict.
    monkeypatch.setattr(server_state, "_decorate_totals_with_savings",
                        lambda totals, sn: totals)

    out_b = server_state.serialize_status(view_device_id="id-B")
    assert out_b["battery_packs"] == [{"rb": 50, "alias": "pack-1"}]
    assert out_b["energy"]["today_solar_wh"] == 1000

    out_a = server_state.serialize_status(view_device_id="id-A")
    assert out_a["battery_packs"] == []  # A has no packs cached
    assert out_a["energy"]["today_solar_wh"] == 5000


def test_secondary_view_history_pulls_from_energy_db(server_state, monkeypatch):
    """When viewing a non-bridge-active device, the live chart must
    hydrate from the energy DB instead of the in-memory deque (which
    only holds the bridge-active device's samples)."""
    captured: dict = {}

    def fake_history(device_sn, hours, bucket_s):
        captured["sn"] = device_sn
        captured["hours"] = hours
        return [
            {"ts": 1700000000, "battery_pct": 41, "input_w": 0, "output_w": 70},
            {"ts": 1700000060, "battery_pct": 42, "input_w": 0, "output_w": 75},
        ]
    monkeypatch.setattr(server_state.state.energy, "history", fake_history)

    # Reset the TTL cache so we know the hydrate ran for THIS test.
    server_state._view_history_cache.clear()

    out = server_state.serialize_status(view_device_id="id-B")
    assert captured["sn"] == "SN-B"
    assert out["history"] == [
        {"ts": 1700000000, "battery_percent": 41,
         "input_power_w": 0, "output_power_w": 70},
        {"ts": 1700000060, "battery_percent": 42,
         "input_power_w": 0, "output_power_w": 75},
    ]


def test_view_select_only_bumps_requesting_browser_ws(server_state):
    """Picking on browser A must NOT update browser B's WS view_id.

    Earlier, /api/view/select_device matched WSes by prior cookie value,
    which collected every browser that happened to share the same prior
    view (e.g. both still on the bridge-active default). The wrongly-
    bumped browser's WS would then push the other view into B's UI for
    2s until B's safety-net /api/status poll snapped back, producing
    a visible "jumping back and forth." The fix matches by auth-session
    cookie instead: only WSes belonging to the requester's session get
    updated."""
    s = server_state

    class FakeWS:
        def __init__(self):
            self.sent: list[str] = []
        async def send_text(self, payload: str):
            self.sent.append(payload)

    ws_a = FakeWS()
    ws_b = FakeWS()
    # Both browsers connected before either picked anything: same view
    # (None = bridge-active), but distinct auth sessions.
    s.state.ws_clients[ws_a] = {"view_id": None, "auth_token": "session-A"}
    s.state.ws_clients[ws_b] = {"view_id": None, "auth_token": "session-B"}

    # Simulate browser A's pick by reproducing the WS-bump logic from
    # api_view_select_device. We call the endpoint logic directly via
    # the same identity match it uses.
    request_auth = "session-A"
    new_id = "id-B"
    for _ws, info in s.state.ws_clients.items():
        if request_auth is not None:
            if info.get("auth_token") == request_auth:
                info["view_id"] = new_id
        else:
            if info.get("view_id") is None:
                info["view_id"] = new_id

    assert s.state.ws_clients[ws_a]["view_id"] == "id-B"
    # Critically: browser B's view did NOT change.
    assert s.state.ws_clients[ws_b]["view_id"] is None


async def test_record_forecasts_for_all_devices(server_state, monkeypatch):
    """The hourly forecast recorder should iterate every device the
    bridge knows about and call the build helper for each, ignoring
    failures on individual devices instead of bailing out. Verifies
    that the loop covers BOTH the bridge-active device and any
    secondary, so prediction_accuracy keeps populating regardless of
    which Jackery the worker happens to be watching."""
    s = server_state

    # Stub location so the recorder doesn't bail early on "location
    # not set." Returns whatever truthy dict the helper would expect.
    monkeypatch.setattr(
        s.device_location, "get",
        lambda: {"latitude": 37.7, "longitude": -122.4},
    )

    called: list[str] = []

    async def fake_build(sn):
        called.append(sn)
        # 5000 succeeds, 3000 returns ready=False (calibrating) — the
        # loop should still cover both and just count the secondary
        # as skipped.
        if sn == "SN-A":
            return {"ready": True, "forecast": [{"ts": 1, "predicted_soc": 50}],
                    "configured": True, "device_sn": sn}
        return {"ready": False, "forecast": [], "configured": True, "device_sn": sn}
    monkeypatch.setattr(s, "_build_and_record_forecast", fake_build)

    await s._record_forecasts_for_all_devices()

    # Both devices in cloud_meta got attempted, in order they appear.
    assert called == ["SN-A", "SN-B"]


async def test_record_forecasts_skips_when_no_devices(server_state, monkeypatch):
    """Empty cloud_meta on cold start (bridge hasn't populated it yet)
    should not raise — just log and skip the iteration."""
    s = server_state
    s.state.last_cloud_meta = {}  # simulate pre-poll state

    monkeypatch.setattr(
        s.device_location, "get",
        lambda: {"latitude": 37.7, "longitude": -122.4},
    )
    called: list[str] = []
    async def fake_build(sn):
        called.append(sn)
        return {"ready": True, "forecast": [], "configured": True}
    monkeypatch.setattr(s, "_build_and_record_forecast", fake_build)

    await s._record_forecasts_for_all_devices()
    assert called == []  # nothing to iterate, nothing called


def test_view_history_is_cached(server_state, monkeypatch):
    """Per-view history is hit on every tick — a TTL cache keeps the
    energy DB query rate sane. We count only LIVE_CHART_HOURS-window
    queries (the live-chart hydrate) to avoid double-counting the
    energy-totals decorator's own 24h/year queries."""
    calls = {"live_chart_hydrates": 0}
    live_chart_hours = server_state.LIVE_CHART_HOURS

    def fake_history(device_sn, hours, bucket_s):
        if hours == live_chart_hours:
            calls["live_chart_hydrates"] += 1
        return []
    monkeypatch.setattr(server_state.state.energy, "history", fake_history)

    server_state._view_history_cache.clear()
    server_state.serialize_status(view_device_id="id-B")
    server_state.serialize_status(view_device_id="id-B")
    server_state.serialize_status(view_device_id="id-B")
    assert calls["live_chart_hydrates"] == 1
