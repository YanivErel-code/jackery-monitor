"""
Jackery 5000 Plus Monitor — local web app.

FastAPI backend that:
  • talks to the device through a pluggable backend (mock / docker bridge)
  • polls battery / power / output status every 10 s
  • exposes a JSON API + WebSocket stream
  • keeps an in-memory ring buffer of the last N samples for the UI chart

Backend selection (env vars):
  BACKEND=mock              -> synthetic telemetry, no hardware
  BACKEND=bridge            -> talks to bridge.py over TCP (host bridge proxies the Jackery cloud)
  BRIDGE_URL=host:port      -> bridge endpoint (default host.docker.internal:8766)
  JACKERY_MOCK=1            -> shorthand for BACKEND=mock

Run:    python server.py
Mock:   JACKERY_MOCK=1 python server.py
Docker: see docker-compose.yml
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import api_auth
import auth
import backoff as _backoff
import backup
import backup_creds
import backup_discover
import cost as cost_module
import energy_db
import forecaster
import kasa_client
import kasa_creds
import location as device_location
import settings as user_settings
import smart_charge
import weather_client
from automation import AutomationEngine, AutomationError
from device_client import (
    DeviceClient,
    DeviceClientError,
    DeviceInfo,
    device_type_for,
    make_client,
)
from energy_db import EnergyDB
from kasa_devices import KasaRegistry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jackery-monitor")

# ---------- config ----------
WEB_DIR = Path(__file__).parent / "web"
# Live chart shows the last N hours by appending one in-memory sample per
# LIVE_CHART_INTERVAL_S (independent of how often we poll the bridge — the
# poll cadence is for the energy aggregator and live KPIs). The deque is
# sized for exactly that span; the chart label and storage agree.
LIVE_CHART_HOURS = 6
LIVE_CHART_INTERVAL_S = 60
HISTORY_LIMIT = (LIVE_CHART_HOURS * 3600) // LIVE_CHART_INTERVAL_S
# Per-expansion-battery refresh cadence. The bridge subscribes to MQTT
# SubDevicePropertyChange pushes and serves packs from memory, so the
# server can poll the bridge every iteration cheaply — there's no cloud
# HTTP cost. Setting this to 0 means "every poll iteration".
BATTERY_PACK_REFRESH_S = 0
# DB persistence cadence — writing every iteration would be 4 rows/min
# for no analytical gain; daily-learning queries only need ~minute
# resolution. Cache stays fresh; only the DB write is throttled.
BATTERY_PACK_DB_PERSIST_S = 300
# Local hour-of-day for the daily Claude advisor review. Lives in
# user_settings ("advisor_trigger_hour") so it's editable from the
# Settings page without a container restart. Env-var fallback
# (JACKERY_ADVISOR_HOUR) handled automatically by settings.py.

# When the forecaster's behavior changes in a way that invalidates older
# saved predictions, bump this timestamp to the deploy time of the fix.
# The /api/forecast/accuracy endpoint exposes a "post-fix" summary using
# this as a `made_at` floor so the dashboard headline reflects current
# model behavior instead of being dragged down by stale rows that age
# out over 14 days. Most recent bump: drain model is now hybrid
# (parasitic_w baseline + percentage of throughput) instead of
# pure-percentage, so the 200-500W constant draw on multi-pack rigs
# the advisor flagged as "unaccounted gap" is captured directly.
# Override via env var when shipping new fixes if updating in code is
# inconvenient.
FORECASTER_BREAKING_CHANGE_TS = int(
    os.environ.get("JACKERY_FORECASTER_CUTOFF_TS", "1778075673")
)

# Per-browser "viewing this Jackery" preference. Independent of the bridge's
# active-device (which the worker manages), so two browsers can look at
# different Jackerys at the same time without stomping each other. Plain
# device_id stored in the cookie — server validates against the current
# account's known devices on every read, so a stale cookie just falls back
# to the bridge-active view.
VIEW_DEVICE_COOKIE = "view_device_id"
VIEW_DEVICE_COOKIE_TTL_S = 365 * 24 * 3600


# ---------- app state ----------
class AppState:
    def __init__(self) -> None:
        self.client: DeviceClient = make_client()
        self.device: DeviceInfo | None = None
        self.energy = EnergyDB()
        self.last_status: dict[str, Any] | None = None
        self.last_update_ts: float | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        # Last append timestamp so we sample the live chart exactly every
        # LIVE_CHART_INTERVAL_S regardless of how fast we poll the bridge.
        self.last_history_ts: float = 0.0
        # Set the first time we hydrate the deque from the energy DB after
        # startup so a container restart doesn't blank the chart.
        self.history_hydrated: bool = False
        self.connection_status = "disconnected"   # disconnected | scanning | connecting | connected | error
        self.connection_error: str | None = None
        self.low_battery_alerted = False
        self.poll_task: asyncio.Task | None = None
        # WebSocket -> {view_id, auth_token} captured at connect time.
        # `view_id` may be None for clients that haven't picked a device
        # (they default to bridge-active). `auth_token` uniquely
        # identifies a browser session — used by /api/view/select_device
        # to bump ONLY the requester's WSes when their cookie changes,
        # without disturbing other browsers that happened to share the
        # same prior view selection. Each broadcast renders status once
        # per unique view_id and fans out to the matching clients.
        self.ws_clients: dict[WebSocket, dict[str, str | None]] = {}
        self.last_source: str | None = None
        self.last_cloud_meta: dict | None = None
        # Per-expansion-battery cache. Refreshed every BATTERY_PACK_REFRESH_S
        # by the poll loop so the UI gets near-realtime per-pack SOC without
        # hammering the cloud. Populated only when the active device has
        # at least one expansion battery.
        # Per-device pack cache. Keys are device SNs; values are the cloud's
        # raw pack-list shape. Devices without expansion packs (e.g. the
        # HomePower 3000) just stay missing from the dict so the UI knows
        # to hide the card for them.
        self.battery_packs_by_sn: dict[str, list[dict]] = {}
        self.last_packs_ts_by_sn: dict[str, float] = {}
        # Last DB-persist timestamp, separate from in-memory cache refresh.
        self.last_packs_db_ts_by_sn: dict[str, float] = {}
        # Last advisor-review timestamp per device, so the daily loop
        # doesn't re-fire on container restart within the same window.
        self.last_advisor_run_by_sn: dict[str, float] = {}
        self.advisor_task: asyncio.Task | None = None
        # Per-device fire-and-forget review job state. Reviews can run
        # 60-180s under adaptive thinking + multi-turn tool calls, well
        # past Cloudflare/proxy timeouts, so /api/algorithm/review_now
        # returns 202 immediately and the UI polls /review_status.
        # Shape: device_sn -> {status, started_at, finished_at, result, error}.
        self.advisor_jobs: dict[str, dict[str, Any]] = {}
        # Battery-SOC automation engine — rules persisted to /data/automation.json,
        # evaluated each poll cycle, edge-triggered so a rule fires once per
        # threshold crossing instead of every single poll. Each successful
        # firing writes an audit row via record_automation_fire so the
        # Automation tab's "View history" view + duration calculations have
        # a real persisted log to work from.
        self.automation: AutomationEngine = AutomationEngine(
            firing_recorder=self.energy.record_automation_fire,
        )
        # Saved Kasa device registry — devices the user has manually added &
        # tested. Rule editor picks from this list instead of asking for an
        # IP each time.
        self.kasa: KasaRegistry = KasaRegistry()

    @property
    def backend(self) -> str:
        return self.client.backend_name

    def reset_live_history(self) -> None:
        """Clear the in-memory live-chart deque + flags so the poll loop
           re-hydrates from the energy DB for whichever device is now active."""
        self.history.clear()
        self.last_history_ts = 0.0
        self.history_hydrated = False


state = AppState()


# ---------- connection flow ----------
async def connect_device() -> bool:
    state.connection_status = "scanning"
    state.connection_error = None
    await broadcast_status("status")

    try:
        ok, info, err = await state.client.connect()
    except Exception as e:
        log.exception("connect raised")
        state.connection_status = "error"
        state.connection_error = f"{type(e).__name__}: {e}"
        await broadcast_status("status")
        return False

    if not ok:
        state.connection_status = "error"
        state.connection_error = err or "connect failed"
        log.warning(state.connection_error)
        await broadcast_status("status")
        return False

    state.device = info
    state.connection_status = "connected"
    state.connection_error = None
    log.info("Connected via %s backend: %s", state.backend,
             info.name if info else "?")
    await broadcast_status("status")
    return True


async def poll_loop() -> None:
    # Cap at 5 min so a transient outage doesn't slow the recovery once
    # the bridge / network comes back. Matches the kasa reconciler base.
    bo = _backoff.LoopBackoff(max_s=5 * 60)
    while True:
        base_s = user_settings.get("poll_interval_s")
        try:
            # Auto-reconnect if we're not connected (e.g. bridge was down at startup,
            # or the container raced ahead of the host bridge). Without this we'd
            # sit forever with is_connected=False and never poll again.
            if not state.client.is_connected and state.backend != "mock":
                log.info("poll_loop: client not connected, attempting reconnect...")
                ok = await connect_device()
                if not ok:
                    bo.record_failure()
                    await asyncio.sleep(bo.next_sleep(base_s))
                    continue

            status_dict = await state.client.poll()

            # Always pull the latest DeviceInfo from the client even if telemetry
            # is briefly None (e.g. just after select_device clears the cache).
            new_dev = getattr(state.client, "device_info", None)
            if new_dev is not None and (
                state.device is None
                or getattr(state.device, "device_sn", None) != getattr(new_dev, "device_sn", None)
            ):
                state.reset_live_history()
                state.device = new_dev
                await broadcast_status("status")

            if status_dict:
                ts = time.time()
                # Strip and stash source metadata before storing telemetry.
                source = status_dict.pop("_source", None)
                cloud_meta = status_dict.pop("_cloud", None)
                state.last_source = source
                state.last_cloud_meta = cloud_meta
                state.last_status = status_dict
                state.last_update_ts = ts

                # Log unknown model_codes ONCE per process so server logs
                # become a contribution channel — paste the warning into
                # a PR adding the model to models.json.
                _flag_unknown_models(cloud_meta)

                # Persist the device-reported UTC offset (`uo` field) so
                # _start_of_day buckets "today" at the user's local
                # midnight even when no location/Open-Meteo is configured.
                # No-op if location already has the same offset.
                tz_off = status_dict.get("utc_offset_seconds")
                if tz_off is not None and tz_off != device_location.get_tz_offset():
                    try:
                        device_location.update_timezone(int(tz_off))
                    except Exception as e:
                        log.debug("uo persist failed: %s", e)

                # Energy aggregation: integrate W over time per device.
                # The bridge polls every Jackery device on the account, so
                # we get telemetry for non-active ones via cloud_meta. Without
                # this, switching to (say) the HomePower 3000 would show
                # "Today: 0 kWh" because samples were only ever written
                # while it was the active device.
                dev = state.device
                dev_sn = dev.device_sn if dev and dev.device_sn else None
                cloud = cloud_meta or {}
                devs_telemetry_all = (cloud.get("devices_telemetry") or {}) if isinstance(cloud, dict) else {}
                cloud_devices = (cloud.get("devices") or []) if isinstance(cloud, dict) else []

                # Write samples for every device the bridge has telemetry for.
                samples_to_write: list[tuple] = []
                for other_sn, entry in devs_telemetry_all.items():
                    other_t = (entry or {}).get("telemetry") or {}
                    if not other_t:
                        continue
                    meta = next(
                        (d for d in cloud_devices
                         if str(d.get("device_sn")) == str(other_sn)),
                        {},
                    )
                    samples_to_write.append(
                        (other_sn, other_t, meta.get("name"), meta.get("model_code"))
                    )
                # The active device's status_dict may carry richer fields
                # than its devices_telemetry mirror — write it explicitly
                # if the loop above didn't already.
                if dev_sn and dev_sn not in devs_telemetry_all:
                    samples_to_write.append(
                        (dev_sn, status_dict,
                         getattr(dev, "name", None),
                         getattr(dev, "model_code", None))
                    )
                for sn, t, name, model_code in samples_to_write:
                    state.energy.upsert_device(sn, name, model_code, None)
                    state.energy.record(
                        sn, ts,
                        float(t.get("input_power_w") or 0),
                        float(t.get("output_power_w") or 0),
                        int(t.get("battery_percent") or 0),
                        solar_w=float(t.get("solar_input_w") or 0),
                        ac_input_w=float(t.get("ac_input_w") or 0),
                    )

                # Hydrate the live chart from the energy DB on the first
                # successful poll after startup, so the chart shows the
                # last LIVE_CHART_HOURS even immediately after a restart.
                if dev_sn and not state.history_hydrated:
                    try:
                        past = state.energy.history(
                            dev_sn,
                            hours=LIVE_CHART_HOURS,
                            bucket_s=LIVE_CHART_INTERVAL_S,
                        )
                        for p in past:
                            state.history.append(_energy_db_row_to_chart_point(p))
                        log.info("Live chart hydrated with %d historical points (last %dh)",
                                 len(past), LIVE_CHART_HOURS)
                    except Exception as e:
                        log.warning("history hydrate failed: %s", e)
                    state.history_hydrated = True

                # Per-expansion-battery refresh. Refresh packs for every
                # device on the account, not just the bridge-active one,
                # so per-browser views of secondary devices show the same
                # pack rows the bridge-active view would. The bridge
                # serves these from its MQTT push cache so a no-op
                # refresh is essentially free.
                pack_sns = {sn for sn, *_ in samples_to_write}
                if dev_sn:
                    pack_sns.add(dev_sn)
                for pack_sn in pack_sns:
                    await _refresh_packs_for(pack_sn, ts)

                # Append a live sample once per LIVE_CHART_INTERVAL_S so the
                # chart's x-axis spacing is stable (the bridge poll cadence
                # is independent and faster).
                if ts - state.last_history_ts >= LIVE_CHART_INTERVAL_S:
                    state.history.append({
                        "ts": ts,
                        "battery_percent": status_dict["battery_percent"],
                        "input_power_w": status_dict["input_power_w"],
                        "output_power_w": status_dict["output_power_w"],
                    })
                    state.last_history_ts = ts
                await broadcast_status("telemetry")

                threshold = user_settings.get("low_battery_threshold")
                bp = status_dict["battery_percent"]
                # Compare the threshold against SYSTEM SOC (capacity-
                # weighted across main + packs), not the main unit's
                # reported `battery_percent`. On a 6-pack rig the main
                # can read 23% while the system is at 16% — alerting on
                # main means we'd miss the real "battery is critically
                # low" moment. _system_soc_pct returns main_pct
                # unchanged on single-unit devices.
                active_sn = state.device.device_sn if state.device else None
                active_model_code = getattr(state.device, "model_code", None) if state.device else None
                bp_system = _system_soc_pct(float(bp), active_sn, active_model_code)
                if bp_system <= threshold and not state.low_battery_alerted:
                    state.low_battery_alerted = True
                    await broadcast({
                        "type": "alert",
                        "data": {"level": "warning",
                                 "message": f"Battery low: {bp_system:.0f}%"},
                    })
                elif bp_system > threshold + 5:
                    state.low_battery_alerted = False

                # Run automation rules. The bridge polls every Jackery device
                # so rules can target any of them, not just the active one;
                # we build a {device_sn: soc} dict from cloud_meta and let
                # the engine pick each rule's target.
                #
                # Use SYSTEM SOC (capacity-weighted across main + every
                # expansion pack), not the main unit's `battery_percent`.
                # On a 6-pack rig the main pack runs ~6-7pp ahead of (or
                # behind) the system during BMS rebalancing, so a "<20%"
                # rule against main reads 23% while the system is actually
                # at 16% — rule never fires when it should. Same bug class
                # as the forecaster slope-fit issue we fixed 2026-05-06,
                # different consumer.
                cloud = cloud_meta or {}
                devs_telemetry = (cloud.get("devices_telemetry") or {}) if isinstance(cloud, dict) else {}
                model_code_by_sn = {
                    str(d.get("device_sn")): (
                        int(d.get("model_code")) if d.get("model_code") is not None else None
                    )
                    for d in (cloud.get("devices") or [])
                    if d.get("device_sn") is not None
                }
                soc_by_sn: dict[str, float] = {}
                for sn, entry in devs_telemetry.items():
                    t = (entry or {}).get("telemetry") or {}
                    bp_dev = t.get("battery_percent")
                    if bp_dev is not None:
                        soc_by_sn[sn] = _system_soc_pct(
                            float(bp_dev), sn, model_code_by_sn.get(str(sn)),
                        )
                # Always include the active device too (for legacy rules
                # without an explicit jackery_device_sn). active_sn /
                # active_model_code already computed in the low-battery
                # alert block above; reuse.
                if active_sn and bp is not None and active_sn not in soc_by_sn:
                    soc_by_sn[active_sn] = _system_soc_pct(
                        float(bp), active_sn, active_model_code,
                    )
                if soc_by_sn:
                    try:
                        fired = await state.automation.evaluate(soc_by_sn, active_sn=active_sn)
                        for rule in fired:
                            await broadcast({
                                "type": "automation_fired",
                                "data": {
                                    "id": rule.get("id"),
                                    "name": rule.get("name"),
                                    "action": rule.get("action"),
                                    "kasa_alias": rule.get("kasa_alias"),
                                    "kasa_host": rule.get("kasa_host"),
                                    "jackery_device_sn": rule.get("jackery_device_sn"),
                                    "jackery_device_name": rule.get("jackery_device_name"),
                                    "last_fired": rule.get("last_fired"),
                                },
                            })
                    except Exception as e:
                        log.warning("automation evaluate failed: %s", e)
            bo.reset()
        except Exception as e:
            bo.record_failure()
            log.exception("Poll loop error: %s", e)

        # Re-read each iteration so a settings change applies on the next
        # cycle (instead of at restart).
        await asyncio.sleep(bo.next_sleep(base_s))


# ---------- WebSocket fan-out ----------
async def broadcast(message: dict[str, Any]) -> None:
    """Send the same payload to every connected client. Use for global
    events (alerts, automation_fired) where every browser sees the same
    thing. For status/telemetry use `broadcast_status` so each client
    gets their per-view render."""
    if not state.ws_clients:
        return
    payload = json.dumps(message)
    dead: list[WebSocket] = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.pop(ws, None)


async def broadcast_status(message_type: str = "status") -> None:
    """Render `serialize_status` per unique view_device_id and fan out
    to the matching clients. Memoizes by view so we render once even
    when several browsers share the same selection (the common case)."""
    if not state.ws_clients:
        return
    rendered: dict[str | None, str] = {}
    dead: list[WebSocket] = []
    for ws, info in state.ws_clients.items():
        view_id = info.get("view_id")
        if view_id not in rendered:
            rendered[view_id] = json.dumps({
                "type": message_type,
                "data": serialize_status(view_device_id=view_id),
            })
        try:
            await ws.send_text(rendered[view_id])
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.pop(ws, None)


def _capacity_hints(device_sn: str | None) -> tuple[int | None, int | None]:
    """Look up (main_wh, pack_wh) from the device's model_code so
    `prediction_accuracy()` and `smart_charge_analytics()` can capacity-
    weight the actual SOC to match the (system-weighted) predicted.
    Returns (None, None) for unknown devices — callers fall back to
    main-only behavior."""
    if not device_sn:
        return (None, None)
    dev_meta = next(
        (d for d in state.energy.list_devices()
         if d.get("device_sn") == device_sn),
        None,
    )
    if not dev_meta:
        return (None, None)
    model_code = dev_meta.get("model_code")
    return (forecaster.battery_capacity_wh(model_code),
            forecaster.expansion_pack_capacity_wh(model_code))


def _system_soc_pct(main_pct: float, device_sn: str | None,
                    model_code: int | None = None) -> float:
    """Combined SOC across the main unit + every cached expansion pack
    *for the given device*, weighted by capacity. Devices without packs
    return main_pct unchanged so single-unit setups (e.g. HomePower 3000)
    behave exactly as before.
    """
    packs = state.battery_packs_by_sn.get(device_sn or "", []) if device_sn else []
    if not packs:
        return main_pct
    main_wh = forecaster.battery_capacity_wh(model_code)
    pack_wh = forecaster.expansion_pack_capacity_wh(model_code)
    total_wh = main_wh + len(packs) * pack_wh
    if total_wh <= 0:
        return main_pct
    stored = main_pct * main_wh / 100.0
    for p in packs:
        rb = p.get("rb")
        if rb is not None:
            stored += float(rb) * pack_wh / 100.0
    return max(0.0, min(100.0, stored / total_wh * 100.0))


def _total_capacity_wh(device_sn: str | None,
                       model_code: int | None = None) -> int:
    """Total system capacity for a device, including expansion packs.

    Resolution order:
      1. Manual override on the devices row (set via the Device tab).
      2. Auto-derived from the cached battery_packs list when the device
         is the active one — main + N x pack capacity. This is the new
         hands-off path; users with packs no longer have to set the
         override explicitly.
      3. Spec capacity for the model (no expansion packs assumed).
    """
    if device_sn:
        override = state.energy.get_capacity_override(device_sn)
        if override:
            return int(override)
    main_wh = forecaster.battery_capacity_wh(model_code)
    packs = state.battery_packs_by_sn.get(device_sn or "", []) if device_sn else []
    if packs:
        pack_wh = forecaster.expansion_pack_capacity_wh(model_code)
        return main_wh + len(packs) * pack_wh
    return main_wh


def _in_progress_savings_row() -> dict | None:
    """A synthetic energy_db.history row covering [last_db_record, now]
    so the cost display reflects current grid/output use without waiting
    for the next 2s poll to land in the DB.

    Returns None if there's no recent telemetry or the gap is large
    enough that linear interpolation would be wrong (>60s)."""
    t = state.last_status
    if not t or not state.last_update_ts:
        return None
    now = time.time()
    dt = now - state.last_update_ts
    if dt <= 0 or dt > 60:
        return None
    h = dt / 3600.0
    return {
        "ts": int(now),  # tagged with "now" so rate_at picks the correct TOU slot
        "output_wh":   float(t.get("output_power_w") or 0) * h,
        "input_wh":    float(t.get("input_power_w") or 0) * h,
        "solar_wh":    float(t.get("solar_input_w") or 0) * h,
        "ac_input_wh": float(t.get("ac_input_w") or 0) * h,
    }


def _decorate_totals_with_savings(totals: dict, device_sn: str) -> dict:
    """Add today_savings + lifetime_savings + cost_plan to a totals dict.

    Cheap enough to do per-poll (hourly buckets over 1y is ~8760 rows
    iterated through Python). Idempotent — safe to call from any caller
    that already produced the kWh totals."""
    try:
        plan = cost_module.get_plan()
        loc = device_location.get() or {}
        tz_offset = int(loc.get("utc_offset_seconds") or 0)
        from energy_db import _start_of_day
        today_since = _start_of_day(int(time.time()))
        today_hist = [r for r in state.energy.history(device_sn, hours=24, bucket_s=3600)
                      if (r.get("ts") or 0) >= today_since]
        life_hist = state.energy.history(device_sn, hours=24 * 365, bucket_s=3600)
        # Tack on the in-progress sliver — covers grid/output activity
        # since the last DB record() call so the dashboard reflects
        # changes within ~2s instead of waiting for the next poll
        # cycle to land.
        in_progress = _in_progress_savings_row()
        if in_progress:
            today_hist.append(in_progress)
            life_hist.append(in_progress)
        totals["today_savings"] = cost_module.today_savings(today_hist, plan, tz_offset)
        totals["lifetime_savings"] = cost_module.lifetime_savings(life_hist, plan, tz_offset)
        totals["cost_plan"] = {"type": plan["type"],
                               "currency": plan.get("currency", "USD")}
    except Exception as e:
        log.debug("cost decoration failed: %s", e)
    return totals


async def _refresh_packs_for(device_sn: str, ts: float) -> None:
    """Pull the latest expansion-pack telemetry for `device_sn` and
    update the in-memory cache + (throttled) the energy DB. Throttled
    per-SN by BATTERY_PACK_REFRESH_S; on failure we deliberately do
    NOT advance the per-device timestamp so the next tick retries
    immediately rather than waiting a full refresh window."""
    last_ts = state.last_packs_ts_by_sn.get(device_sn, 0.0)
    if ts - last_ts < BATTERY_PACK_REFRESH_S:
        return
    rpc = getattr(state.client, "_rpc", None)
    if rpc is None:
        return
    try:
        result = await rpc("get_battery_packs", device_sn=device_sn)
    except Exception as e:
        log.warning("battery_packs refresh failed for %s: %s", device_sn, e)
        return
    err = (result or {}).get("error")
    packs = (result or {}).get("packs") or []
    if err:
        log.warning("battery_packs RPC error for %s: %s", device_sn, err)
        return
    if packs:
        # Strip BMS sensor garbage at ingestion so it doesn't flow into
        # the cache, the DB, or the advisor's analysis. Bad pack temps
        # in particular have shown up as 4C and 135C — impossibilities
        # the sensor would never produce if it were working. Drop them;
        # downstream code already handles missing values gracefully.
        packs = _sanitize_pack_telemetry(packs)
        state.battery_packs_by_sn[device_sn] = packs
        state.last_packs_ts_by_sn[device_sn] = ts
        # DB persist throttled separately — the cache is fresh on every
        # iteration but the daily-learning trace only needs minute
        # resolution.
        last_db = state.last_packs_db_ts_by_sn.get(device_sn, 0.0)
        if ts - last_db >= BATTERY_PACK_DB_PERSIST_S:
            state.energy.record_battery_packs(device_sn, packs, int(ts))
            state.last_packs_db_ts_by_sn[device_sn] = ts
    else:
        # Empty list with no error means the device has no expansion
        # packs (e.g. HomePower 3000). Record that explicitly so the UI
        # hides the pack card for this device.
        state.battery_packs_by_sn[device_sn] = []
        state.last_packs_ts_by_sn[device_sn] = ts


def _energy_db_row_to_chart_point(p: dict) -> dict:
    """Rename the energy_db.history columns into the live-chart shape
    the frontend expects. Used by both the startup hydrate path and
    the per-view history fetch."""
    return {
        "ts": p["ts"],
        "battery_percent": p["battery_pct"] or 0,
        "input_power_w": p["input_w"] or 0,
        "output_power_w": p["output_w"] or 0,
    }


_VIEW_HISTORY_TTL_S = 30
_view_history_cache: dict[str, tuple[float, list[dict]]] = {}


def _view_history(device_sn: str | None) -> list[dict]:
    """Live-chart points for a non-bridge-active view. Hydrated from the
    energy DB and cached for _VIEW_HISTORY_TTL_S so the broadcast loop
    doesn't requery on every tick."""
    if not device_sn:
        return []
    now = time.time()
    cached = _view_history_cache.get(device_sn)
    if cached and now - cached[0] < _VIEW_HISTORY_TTL_S:
        return cached[1]
    try:
        rows = state.energy.history(
            device_sn, hours=LIVE_CHART_HOURS, bucket_s=LIVE_CHART_INTERVAL_S,
        )
        out = [_energy_db_row_to_chart_point(p) for p in rows]
        _view_history_cache[device_sn] = (now, out)
        return out
    except Exception as e:
        log.debug("view history hydrate for %s failed: %s", device_sn, e)
        return []


def serialize_status(view_device_id: str | None = None) -> dict[str, Any]:
    """Build the WS/REST status payload.

    `view_device_id` is the per-browser cookie value indicating which
    Jackery this client wants to see. When it matches the bridge-active
    device (or is missing/unknown), we return the same rich response we
    always have. When it points at a different device on the account, we
    synthesize the response from cached per-device data: telemetry from
    `cloud_meta.devices_telemetry`, packs from the per-device cache, live
    history from the energy DB. The bridge polls every device on every
    tick, so all this data is fresh.

    `cloud.selected_device_id` in the response is overridden with the
    chosen view so the frontend's `activeJackeryDevice()` reflects the
    per-browser selection without other code changes.
    """
    cloud_src = state.last_cloud_meta or {}
    bridge_active_id = cloud_src.get("selected_device_id")
    bridge_active_sn = state.device.device_sn if state.device else None

    # Resolve the view override against currently-known devices. A stale
    # cookie pointing at a device that's no longer on the account just
    # falls through to the bridge-active view.
    view_meta: dict | None = None
    if view_device_id and str(view_device_id) != str(bridge_active_id or ""):
        for d in (cloud_src.get("devices") or []):
            if str(d.get("device_id")) == str(view_device_id):
                view_meta = d
                break

    if view_meta is None:
        device_info = state.device.to_dict() if state.device else None
        view_sn = bridge_active_sn
        view_id = bridge_active_id
        telemetry = state.last_status
        view_packs = state.battery_packs_by_sn.get(view_sn or "", [])
        history = list(state.history)
        model_code = getattr(state.device, "model_code", None)
    else:
        view_sn = str(view_meta.get("device_sn") or "") or None
        view_id = str(view_meta["device_id"])
        device_info = {
            "name": view_meta.get("name") or view_meta.get("model_name"),
            "address": "cloud",
            "rssi": 0,
            "model_code": view_meta.get("model_code"),
            "device_sn": view_sn,
            "device_type": device_type_for(view_meta.get("model_code")),
        }
        devs_t = (cloud_src.get("devices_telemetry") or {})
        entry = devs_t.get(view_sn) or {}
        telemetry = entry.get("telemetry")
        view_packs = state.battery_packs_by_sn.get(view_sn or "", [])
        history = _view_history(view_sn)
        model_code = view_meta.get("model_code")

    # Augment telemetry with the precomputed system SOC so the SOC card
    # renders the right number on the very first paint (no main→system
    # flash). Falls back to the raw telemetry untouched when the device
    # has no expansion packs (e.g. HomePower 3000).
    if telemetry and view_packs:
        main_pct = telemetry.get("battery_percent")
        if main_pct is not None:
            sys_pct = _system_soc_pct(float(main_pct), view_sn, model_code)
            telemetry = {**telemetry,
                         "main_soc_pct": main_pct,
                         "system_soc_pct": sys_pct}

    energy = None
    try:
        if view_sn:
            energy = _decorate_totals_with_savings(
                state.energy.totals(view_sn), view_sn,
            )
    except Exception as e:
        log.debug("energy totals lookup failed: %s", e)

    # Shallow-copy cloud_meta so we can override selected_device_id without
    # mutating the cached state shared with all other clients.
    cloud_out = dict(state.last_cloud_meta) if state.last_cloud_meta else None
    if cloud_out is not None:
        cloud_out["selected_device_id"] = view_id

    return {
        "connection_status": state.connection_status,
        "connection_error": state.connection_error,
        "device": device_info,
        "last_update_ts": state.last_update_ts,
        "telemetry": telemetry,
        # Piggy-back packs on the WS broadcast so per-pack rows update at
        # the same cadence as the SOC card. Empty list for devices without
        # packs (e.g. HomePower 3000) — UI hides the card on empty.
        "battery_packs": view_packs,
        "history": history,
        "mock_mode": state.backend == "mock",
        "backend": state.backend,
        "low_battery_threshold": user_settings.get("low_battery_threshold"),
        "source": state.last_source,
        "cloud": cloud_out,
        "energy": energy,
    }


# ---------- FastAPI ----------
def _find_sun_phases(forecast_hours: list[dict]) -> tuple[int | None, int | None, dict, dict]:
    """Walk a forecast and return (sunset_ts, sunrise_ts, sunset_entry,
    sunrise_entry). Sunset = last hour with solar_w > 0 before a dark run.
    Sunrise = first hour with solar_w > 0 after a dark run. Either may be
    None if not found within the forecast window."""
    sunset_ts: int | None = None
    sunrise_ts: int | None = None
    sunset_entry: dict = {}
    sunrise_entry: dict = {}
    in_day = (forecast_hours[0].get("solar_w") or 0) > 0 if forecast_hours else False
    for i, h in enumerate(forecast_hours):
        sun = (h.get("solar_w") or 0) > 0
        if in_day and not sun:
            # transition day → night: previous hour was sunset
            if i > 0:
                sunset_ts = int(forecast_hours[i - 1]["ts"])
                sunset_entry = forecast_hours[i - 1]
        if not in_day and sun:
            # transition night → day: previous hour was the last dark hour
            # (i.e., sunrise's predicted SOC)
            if i > 0 and sunrise_ts is None:
                sunrise_ts = int(forecast_hours[i - 1]["ts"]) + 3600
                sunrise_entry = forecast_hours[i - 1]
                break
        in_day = sun
    return sunset_ts, sunrise_ts, sunset_entry, sunrise_entry


def _local_date_str(ts: int, tz_offset_seconds: int) -> str:
    """ISO YYYY-MM-DD in the user's local TZ."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(
        ts + tz_offset_seconds, tz=timezone.utc
    ).strftime("%Y-%m-%d")


async def _update_daily_summary(device_sn: str, fcast: dict,
                                tz_offset_seconds: int) -> None:
    """Fill in today's daily_solar_summary row with whatever we currently
    know — predicted values for moments still ahead, actual values pulled
    from the samples table for moments already past. Called from the
    smart-charge tick (every 5 min) so the row converges to ground truth
    progressively."""
    fc = fcast.get("forecast") or []
    if not fc:
        return
    sunset_ts, sunrise_ts, sunset_e, sunrise_e = _find_sun_phases(fc)
    now = int(time.time())
    today = _local_date_str(now, tz_offset_seconds)

    pred_sunset = sunset_e.get("predicted_soc") if sunset_e else None
    pred_sunrise = sunrise_e.get("predicted_soc") if sunrise_e else None

    main_wh, pack_wh = _capacity_hints(device_sn)
    actual_sunset = (state.energy.system_soc_at(
                         device_sn, sunset_ts,
                         main_capacity_wh=main_wh,
                         pack_capacity_wh=pack_wh)
                     if sunset_ts and sunset_ts <= now else None)
    actual_sunrise = (state.energy.system_soc_at(
                          device_sn, sunrise_ts,
                          main_capacity_wh=main_wh,
                          pack_capacity_wh=pack_wh)
                      if sunrise_ts and sunrise_ts <= now else None)

    state.energy.upsert_daily_summary(
        device_sn=device_sn, local_date=today,
        sunset_ts=sunset_ts, sunrise_ts=sunrise_ts,
        predicted_sunset_soc_pct=pred_sunset,
        actual_sunset_soc_pct=actual_sunset,
        predicted_sunrise_soc_pct=pred_sunrise,
        actual_sunrise_soc_pct=actual_sunrise,
    )
    # Back-fill any past rows whose actuals are still null. Each row's
    # `sunrise_ts` typically falls on the FOLLOWING calendar day, so
    # today's tick can never back-fill yesterday's sunrise from the
    # single-row write above. The back-fill walks the last 14 days
    # and fills in actuals for any (sunset_ts, sunrise_ts) that has
    # aged into the past with samples available.
    try:
        state.energy.backfill_daily_actuals(
            device_sn,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        )
    except Exception as e:
        log.debug("daily summary backfill failed: %s", e)


# Per-pack temperature reporting on the Jackery 5000 Plus is unreliable
# across firmwares — observed values include 4°C with 20°C+ ambient,
# 135°C while neighboring packs read 78°C, and other physically
# impossible readings. Only the main unit's `bt` field (rendered as
# `battery_temp_c`) tracks reality; per-pack `it` is dropped entirely
# rather than displayed alongside the trustworthy main reading. If a
# future firmware fixes pack reporting, replace this unconditional
# strip with a plausibility band.
def _sanitize_pack_telemetry(packs: list[dict]) -> list[dict]:
    """Strip the unreliable per-pack `it` (internal temperature) field
    from the cloud's pack list before it lands in the cache or the DB.
    Returns a new list of dicts; everything else passes through."""
    out: list[dict] = []
    for p in packs:
        if not isinstance(p, dict):
            out.append(p)
            continue
        clean = dict(p)
        clean["it"] = None
        out.append(clean)
    return out


_unknown_models_warned: set[int] = set()
# In-flight + completed auto-probes, keyed by device_sn. Each value
# carries the raw probe responses + the extracted capacity candidates
# so the Device tab can render a "we found this in the cloud — use it?"
# prompt. Lives in process memory; rebuilds on restart by re-probing
# any unknown devices we still see.
_auto_probe_results: dict[str, dict[str, Any]] = {}
_auto_probe_in_flight: set[str] = set()


# Plausible Wh range for any "capacity-shaped" number we find. Below
# the floor it's probably a setting in some other unit (Ah, V); above
# the ceiling it's probably a counter or a timestamp leaked through.
_PROBE_WH_MIN = 500.0
_PROBE_WH_MAX = 200_000.0
# Keys whose name suggests battery capacity. Case-insensitive substring.
_PROBE_CAPACITY_KEY_RE = re.compile(
    r"capacity|battery_?wh|cell_?wh|nominal_?wh|rated_?wh", re.IGNORECASE,
)
_PROBE_KWH_KEY_RE = re.compile(r"kwh|kw_?h", re.IGNORECASE)


def _extract_capacity_candidates(probe_results: dict | None) -> list[dict]:
    """Walk a probe-endpoints response tree and find numeric values
    that look like a battery capacity. Returns a list of candidate
    dicts {endpoint, key_path, raw_value, capacity_wh, units}, sorted
    by descending plausibility (currently: prefer 5040, 2042, etc. —
    "round" Wh figures Jackery uses)."""
    if not isinstance(probe_results, dict):
        return []
    candidates: list[dict] = []

    def _walk(obj: Any, endpoint: str, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, endpoint, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, endpoint, f"{path}[{i}]")
        elif isinstance(obj, (int, float)) and obj > 0:
            leaf = path.rsplit(".", 1)[-1].split("[")[0]
            if not _PROBE_CAPACITY_KEY_RE.search(leaf):
                return
            wh: float | None = None
            units = "wh"
            if _PROBE_KWH_KEY_RE.search(leaf) and 0.5 <= obj <= 200:
                wh = float(obj) * 1000.0
                units = "kwh"
            elif _PROBE_WH_MIN <= obj <= _PROBE_WH_MAX:
                wh = float(obj)
            if wh is not None:
                candidates.append({
                    "endpoint": endpoint,
                    "key_path": path,
                    "raw_value": obj,
                    "capacity_wh": wh,
                    "units": units,
                })

    for endpoint, response in probe_results.items():
        if isinstance(response, dict):
            _walk(response, endpoint, "")

    # De-duplicate identical (endpoint, value) pairs — the same number
    # often surfaces multiple times in nested structures.
    seen: set[tuple[str, float]] = set()
    deduped = []
    for c in candidates:
        key = (c["endpoint"], c["capacity_wh"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


async def _auto_probe_device(device_sn: str, device_id: str,
                              model_code: int, model_name: str) -> None:
    """Background task: probe the cloud for an unknown device,
    extract capacity candidates, and stash results in state for the
    Device tab to render. Idempotent per device_sn so a re-trigger
    while a probe is in flight is a no-op."""
    if not device_sn or device_sn in _auto_probe_in_flight:
        return
    _auto_probe_in_flight.add(device_sn)
    try:
        rpc = getattr(state.client, "_rpc", None)
        if rpc is None:
            log.debug("auto-probe skipped: bridge RPC unavailable")
            return
        log.info("Auto-probing cloud for unknown model_code=%s "
                 "(device_sn=%s, name=%r) — looking for capacity hints…",
                 model_code, device_sn, model_name)
        try:
            result = await asyncio.wait_for(
                rpc("cloud_probe", device_id=device_id), timeout=30.0,
            )
        except (TimeoutError, Exception) as e:
            log.warning("auto-probe failed for device_sn=%s: %s", device_sn, e)
            _auto_probe_results[device_sn] = {
                "device_sn": device_sn, "model_code": model_code,
                "model_name": model_name, "device_id": device_id,
                "ts": time.time(), "error": str(e)[:200],
                "candidates": [], "raw": {},
            }
            return
        raw = (result or {}).get("results") or {}
        candidates = _extract_capacity_candidates(raw)
        _auto_probe_results[device_sn] = {
            "device_sn": device_sn, "model_code": model_code,
            "model_name": model_name, "device_id": device_id,
            "ts": time.time(), "error": None,
            "candidates": candidates, "raw": raw,
        }
        if candidates:
            log.info("Auto-probe found %d capacity candidate(s) for "
                     "device_sn=%s: %s", len(candidates), device_sn,
                     [(c["endpoint"], c["key_path"], c["capacity_wh"])
                      for c in candidates[:5]])
        else:
            log.info("Auto-probe found no capacity candidates for "
                     "device_sn=%s; user will need to set a per-device "
                     "override or PR models.json. Probed endpoints: %s",
                     device_sn, list(raw.keys()))
    finally:
        _auto_probe_in_flight.discard(device_sn)


def _flag_unknown_models(cloud_meta: dict | None) -> None:
    """Loudly log devices whose model_code isn't in the catalog. Once
    per process per model_code so the warning stays useful instead of
    spamming. Also kicks off a background auto-probe of the cloud for
    capacity hints. The Device tab surfaces both in the UI."""
    if not isinstance(cloud_meta, dict):
        return
    for d in cloud_meta.get("devices") or []:
        if not isinstance(d, dict):
            continue
        mc = d.get("model_code")
        if mc is None:
            continue
        try:
            mc_int = int(mc)
        except (TypeError, ValueError):
            continue
        if mc_int in forecaster.BATTERY_CAPACITY_WH:
            continue
        if mc_int in _unknown_models_warned:
            continue
        _unknown_models_warned.add(mc_int)
        log.warning(
            "Unknown Jackery model_code=%s (model_name=%r, name=%r, "
            "device_sn=%s); using fallback capacity %d Wh. Consider "
            "adding it to models.json — see README's 'Adding a new "
            "Jackery model' section.",
            mc_int, d.get("model_name"), d.get("name"),
            d.get("device_sn") or "?",
            forecaster.DEFAULT_BATTERY_CAPACITY_WH,
        )
        device_sn = d.get("device_sn") or ""
        device_id = d.get("device_id") or ""
        if device_sn and device_id:
            asyncio.create_task(_auto_probe_device(
                device_sn, device_id, mc_int,
                d.get("model_name") or d.get("name") or "",
            ))


# ============================================================
# Per-device parameter resolution ladder
# ============================================================
# Single entry point for "what value should we use for parameter X on
# device Y?". Walks: user override (DB) → cached fit (DB) → live fit
# → catalog/probe → default → unknown. Every call returns a dict
# {value, source, ...} so callers can render "where this came from"
# in the UI and decide whether to ask the user.
#
# Adding a new resolvable parameter:
#  1. Append to energy_db.DEVICE_PARAM_KEYS so the UI knows about it
#  2. Add a clause below for the live-fit / catalog steps
#
# The DB rows themselves are written by either the UI ("user" source)
# or by background fits ("fit" / "probe" source) so the resolver can
# always read from DB first and avoid recomputing on every call.

# Cache the freshly-fit values on a short timer so the resolver can be
# hit on every API call without re-doing the math each time.
_param_fit_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_PARAM_FIT_CACHE_TTL_S = 60.0


def _resolved_capacity_wh(device_sn: str | None) -> int:
    """The total system capacity (main + N expansion packs) for this
    device, in Wh. Mirrors the `battery_capacity_wh` resolver branch
    so other fits (idle overhead, charge efficiency) compute against
    the same number the simulator uses. Without this helper, callers
    would have to repeat the user-override → catalog → packs ladder
    by hand, and easily skip a step (which we did — the fit branches
    were passing `model_code=None` to _total_capacity_wh, getting the
    3024 Wh fallback * pack_count instead of the real 30240 Wh)."""
    if not device_sn:
        return forecaster.DEFAULT_BATTERY_CAPACITY_WH
    override = state.energy.get_capacity_override(device_sn)
    if override:
        return int(override)
    d = next((x for x in state.energy.list_devices()
              if x.get("device_sn") == device_sn), None)
    mc = (d or {}).get("model_code")
    return _total_capacity_wh(device_sn, mc)


def _cached_history(device_sn: str) -> list[dict]:
    """One-shot 14d history pull, used by the resolver's live-fit
    branch. Memoized via _param_fit_cache so multiple param lookups
    in the same request don't re-query the DB."""
    key = (device_sn, "_history")
    now = time.time()
    cached = _param_fit_cache.get(key)
    if cached and now - cached[0] < _PARAM_FIT_CACHE_TTL_S:
        return cached[1]  # type: ignore[return-value]
    h = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
    _param_fit_cache[key] = (now, h)
    return h


def resolve_device_param(device_sn: str, key: str) -> dict[str, Any]:
    """Walk the resolution ladder for `key` on `device_sn`. Returns a
    dict with at least {value, source}; may also include n_samples,
    confidence, note, updated_at when those make sense.

    Source values, in priority order:
      'user'    — user-set override in DB (Device tab form, etc.)
      'fit'     — cached fit from DB (auto-fit results, persisted)
      'probe'   — cloud-probe result (e.g. discovered capacity)
      'catalog' — bundled lookup (models.json for capacity)
      'default' — population fallback (cold-start)
      'unknown' — none of the above; UI should ask the user

    The resolver writes back fresh fits to DB (source='fit') so a
    subsequent call returns the cached value without recomputing.
    """
    if not device_sn or not key:
        return {"value": None, "source": "unknown"}

    # Step 1: anything stored in DB wins (user overrides + cached fits).
    stored = state.energy.get_device_param(device_sn, key)
    if stored and stored.get("value") is not None:
        if stored.get("source") == "user":
            return {**stored, "source": "user"}
        # Source-of-truth is always the live re-resolution: fits are
        # sub-millisecond once `_cached_history` memoizes the 14d
        # window for the request, and probes are bounded. The DB row
        # is a record of the latest result for UI staleness display
        # and historical analysis, NOT a cache that shadows the
        # current fit. Earlier we cached 'fit'/'probe' results for
        # 24h, which had the awful side effect of locking in buggy
        # values across deploys. Now, only `source='user'` returns
        # early — every other key re-runs the ladder.

    # Step 2: live computation per parameter.
    if key == "battery_capacity_wh":
        # Return the SYSTEM-wide capacity (main + N expansion packs),
        # not just the per-unit catalog value — that's what the
        # simulator actually uses. Source priority: user override
        # (capacity_wh_override) > catalog * packs > default.
        try:
            override = state.energy.get_capacity_override(device_sn)
            if override:
                state.energy.set_device_param(
                    device_sn, key, float(override), source="user",
                    note="capacity_wh_override",
                )
                return {"value": float(override), "source": "user",
                        "updated_at": int(time.time())}
            d = next((x for x in state.energy.list_devices()
                      if x.get("device_sn") == device_sn), None)
            mc = (d or {}).get("model_code")
            total = _total_capacity_wh(device_sn, mc)
            source = "catalog" if mc in forecaster.BATTERY_CAPACITY_WH else "default"
            n_packs = len(state.battery_packs_by_sn.get(device_sn, []))
            state.energy.set_device_param(
                device_sn, key, float(total), source=source,
                note=f"main + {n_packs} pack(s)" if n_packs else "main only",
            )
            return {"value": float(total), "source": source,
                    "note": f"main + {n_packs} pack(s)" if n_packs else "main only",
                    "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s catalog failed: %s", device_sn, key, e)

    elif key == "max_charge_w":
        # Step 0: an explicit value in smart_charge config (set by the
        # user via the Smart-charge form on the Automation tab) wins
        # over the auto-fit. The Device-tab `device_params` 'user'
        # override is already handled by the early-return at the top
        # of this function — this is the SECOND user channel.
        try:
            if smart_charge.has_user_set_field(device_sn, "max_charge_w"):
                cfg = smart_charge.get_config(device_sn)
                cfg_w = float(cfg.get("max_charge_w") or 0)
                if cfg_w > 0:
                    state.energy.set_device_param(
                        device_sn, key, cfg_w, source="user",
                        note="from smart_charge config (Automation tab)",
                    )
                    return {"value": cfg_w, "source": "user",
                            "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s smart_charge lookup failed: %s",
                      device_sn, key, e)

        try:
            tz_off = int(device_location.get_tz_offset() or 0)
            wx = state.energy.list_weather_observations(
                since_ts=int(time.time()) - 14 * 86400, limit=14 * 24,
            )
            w, n = forecaster.fit_max_charge_w(
                _cached_history(device_sn),
                tz_offset_seconds=tz_off, weather_hourly=wx,
            )
            if w is not None:
                state.energy.set_device_param(
                    device_sn, key, w, source="fit", n_samples=n,
                    confidence=("high" if n >= 30 else "medium" if n >= 12 else "low"),
                )
                return {"value": w, "source": "fit", "n_samples": n,
                        "updated_at": int(time.time())}
            # Fit returned None — no qualifying samples after filtering.
            # Write a default ourselves so the next call sees `default`
            # instead of falling through to whatever stale fit was here
            # before. Without this, an old buggy fit's value can't be
            # displaced even with the new filter rejecting all samples.
            default_w = float(smart_charge.DEFAULT_CONFIG.get("max_charge_w") or 800)
            state.energy.set_device_param(
                device_sn, key, default_w, source="default",
                n_samples=n, note="fit found no qualifying samples",
            )
            return {"value": default_w, "source": "default",
                    "n_samples": n, "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s fit failed: %s", device_sn, key, e)

    elif key == "inverter_overhead_pct":
        try:
            cap = _resolved_capacity_wh(device_sn)
            # Use the joint drain-model fit so the percentage and the
            # parasitic baseline come from the same regression — the
            # standalone percentage fit systematically overestimates
            # overhead on devices with significant constant draw.
            _parasitic_w, pct, n = forecaster.fit_drain_model(
                _cached_history(device_sn), cap,
            )
            source = "fit" if n >= 5 else "default"
            state.energy.set_device_param(
                device_sn, key, pct, source=source, n_samples=n,
                confidence=("high" if n >= 20 else "medium" if n >= 10 else "low"),
            )
            return {"value": pct, "source": source, "n_samples": n,
                    "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s fit failed: %s", device_sn, key, e)

    elif key == "parasitic_w":
        try:
            cap = _resolved_capacity_wh(device_sn)
            parasitic, _pct, n = forecaster.fit_drain_model(
                _cached_history(device_sn), cap,
            )
            source = "fit" if n >= 5 else "default"
            state.energy.set_device_param(
                device_sn, key, parasitic, source=source, n_samples=n,
                confidence=("high" if n >= 20 else "medium" if n >= 10 else "low"),
            )
            return {"value": parasitic, "source": source, "n_samples": n,
                    "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s fit failed: %s", device_sn, key, e)

    elif key == "charge_efficiency":
        try:
            cap = _resolved_capacity_wh(device_sn)
            v, n = forecaster.fit_charge_efficiency(_cached_history(device_sn), cap)
            source = "fit" if n >= 5 else "default"
            state.energy.set_device_param(
                device_sn, key, v, source=source, n_samples=n,
                confidence=("high" if n >= 20 else "medium" if n >= 10 else "low"),
            )
            return {"value": v, "source": source, "n_samples": n,
                    "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s fit failed: %s", device_sn, key, e)

    elif key == "solar_coefficient":
        try:
            ehist = _cached_history(device_sn)
            # The solar fit needs paired weather + energy samples; we
            # only have weather observations stored locally. Query the
            # last 14d.
            wx = state.energy.list_weather_observations(
                since_ts=int(time.time()) - 14 * 86400, limit=14 * 24,
            )
            k_val, n = forecaster.fit_solar_coefficient(ehist, wx)
            source = "fit" if n >= forecaster.MIN_FIT_SAMPLES else "default"
            state.energy.set_device_param(
                device_sn, key, k_val, source=source, n_samples=n,
                confidence=("high" if n >= 20 else "medium" if n >= 5 else "low"),
            )
            return {"value": k_val, "source": source, "n_samples": n,
                    "updated_at": int(time.time())}
        except Exception as e:
            log.debug("resolve %s/%s fit failed: %s", device_sn, key, e)

    # Last resort: a fit raised an exception or returned None but we
    # have a previously-stored value — better than telling the caller
    # "unknown" when we have history.
    if stored and stored.get("value") is not None:
        return {**stored, "source": stored.get("source") or "unknown"}

    # Nothing available — caller surfaces "ask user" UI.
    return {"value": None, "source": "unknown"}


def _smart_charge_floor_pct(device_sn: str | None) -> float | None:
    """Always returns None — the displayed/persisted prediction is now
    the unclamped truth, regardless of smart-charge mode.

    Background: an earlier iteration applied a floor in the simulator
    matching `target_sunrise_soc_pct` so that long-lead predictions
    didn't saturate at 0% (the controller would grid-charge in active
    mode, holding SOC at target). The user pushed back 2026-05-06: the
    prediction should show the TRUTH — what the battery will actually
    do without intervention. The controller's intervention is a separate
    concept, surfaced via the smart-charge plan (predicted vs target +
    deficit + charge schedule). Conflating them was a feedback loop
    where the forecast lied to make the controller's behavior look
    prettier, and it hid real model bias from the AI advisor (anomaly
    2026-05-06T03:42 explicitly recommended exposing the unclamped
    forecast).

    The function is kept (returning None) rather than ripped out so the
    git blame / rollback path is obvious. compute_plan still computes a
    deficit against the baseline; with floor=None for everyone, the
    `forecast` and `baseline_forecast` arguments to compute_plan are
    identical, which the deficit math handles correctly."""
    return None


async def _smart_charge_evaluate(record: bool = True,
                                 device_sn: str | None = None):
    """Pull the inputs the smart-charge module needs, compute a Plan, and
    (in active mode) toggle the configured Kasa plug. Per-device — pass
    device_sn to evaluate a specific one; defaults to the active device.
    record=False skips history + side effects."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return None
    cfg = smart_charge.get_config(device_sn)
    if cfg["mode"] == "off":
        return None

    # Resolve metadata for the target device — fall back to active device's
    # model_code if we don't have it stored, since the per-device telemetry
    # cache doesn't carry model_code.
    active_sn = state.device.device_sn if state.device else None
    if device_sn == active_sn:
        model_code = getattr(state.device, "model_code", None)
        cloud = state.last_cloud_meta or {}
        devs_t = (cloud.get("devices_telemetry") or {}) if isinstance(cloud, dict) else {}
        main_soc = float((state.last_status or {}).get("battery_percent") or 50)
    else:
        cloud = state.last_cloud_meta or {}
        devs = (cloud.get("devices") or []) if isinstance(cloud, dict) else []
        meta = next((d for d in devs if str(d.get("device_sn")) == device_sn), {})
        model_code = meta.get("model_code")
        devs_t = (cloud.get("devices_telemetry") or {}) if isinstance(cloud, dict) else {}
        entry = devs_t.get(device_sn) or {}
        t = entry.get("telemetry") or {}
        main_soc = float(t.get("battery_percent") or 50)

    # Inputs: forecast (uses the same cached weather as the Forecast tab),
    # current SOC, capacity (override-aware), TOU plan, tz offset.
    loc = device_location.get() or {}
    if not loc.get("latitude"):
        # No location → no forecast → no decision possible. Surface this in
        # the history so the user knows.
        return smart_charge.compute_plan(
            config=cfg, current_soc_pct=None,
            forecast={"forecast": []}, cost_plan=cost_module.get_plan(),
            capacity_wh=_total_capacity_wh(device_sn, model_code),
        )
    lat, lon = loc["latitude"], loc["longitude"]
    weather = await weather_client.fetch_irradiance(lat, lon)
    if weather.get("error"):
        return None
    # If packs are attached, the forecaster needs the system-wide SOC to
    # match the system-wide capacity it'll be paired with.
    starting_soc = _system_soc_pct(main_soc, device_sn, model_code)
    main_wh, pack_wh = _capacity_hints(device_sn)
    energy_hist = state.energy.history(
        device_sn, hours=14 * 24, bucket_s=3600,
        main_capacity_wh=main_wh, pack_capacity_wh=pack_wh,
    )
    capacity = _total_capacity_wh(device_sn, model_code)
    fcast = forecaster.build_forecast(
        energy_history=energy_hist,
        weather_hourly=weather["hourly"],
        starting_soc_pct=starting_soc,
        capacity_wh=capacity,
        ac_charge_floor_pct=_smart_charge_floor_pct(device_sn),
    )
    # Counterfactual — same forecast computed without the AC charge
    # floor injected. Used by compute_plan to decide if AC is actually
    # needed; with the floor on, predicted_sunrise_soc is at the target
    # by construction and the deficit math collapses to zero.
    baseline_fcast = forecaster.build_forecast(
        energy_history=energy_hist,
        weather_hourly=weather["hourly"],
        starting_soc_pct=starting_soc,
        capacity_wh=capacity,
        ac_charge_floor_pct=None,
    )
    # If we don't have enough history yet to fit a trustworthy forecast,
    # don't act on it — return a no-op plan so the controller stays in
    # "skip" until the forecaster reports ready=True.
    if not fcast.get("ready"):
        readiness = fcast.get("readiness", {})
        return smart_charge.compute_plan(
            config=cfg, current_soc_pct=starting_soc,
            forecast={"forecast": []}, cost_plan=cost_module.get_plan(),
            capacity_wh=capacity,
            tz_offset_seconds=int(loc.get("utc_offset_seconds") or 0),
            forecast_unavailable_reason=(
                f"calibrating: {readiness.get('have_hours', 0)}h of "
                f"{readiness.get('needed_hours', 24)}h captured"
            ),
        )
    # Persist the forecast so we have a continuous trace for predicted-vs-
    # actual analytics, regardless of whether the Forecast tab is open.
    # PK collapses multiple writes within an hour so cost is bounded.
    if record:
        try:
            state.energy.record_forecast(device_sn, time.time(), fcast["forecast"])
        except Exception as e:
            log.debug("forecast persist (smart_charge) failed: %s", e)
    plan = smart_charge.compute_plan(
        config=cfg, current_soc_pct=starting_soc,
        forecast=fcast, baseline_forecast=baseline_fcast,
        cost_plan=cost_module.get_plan(),
        capacity_wh=capacity,
        tz_offset_seconds=int(loc.get("utc_offset_seconds") or 0),
    )

    # Always update the daily sunset/sunrise summary regardless of mode —
    # it's pure data tracking, not a control action.
    if record:
        try:
            await _update_daily_summary(
                device_sn, fcast, int(loc.get("utc_offset_seconds") or 0))
        except Exception as e:
            log.debug("daily summary update failed: %s", e)

    executed = False
    if record and cfg["mode"] == "active" and plan.action in ("on", "off"):
        host = cfg.get("kasa_device_host")
        if host:
            try:
                await kasa_client.set_state(host, plan.action == "on")
                executed = True
            except Exception as e:
                log.warning("smart_charge Kasa toggle failed: %s", e)

    if record:
        narration = ""
        # Belt-and-suspenders: even if the toggle is on, skip narration
        # when no usable key exists — covers the case where the user
        # cleared the key but didn't notice the toggle was still ticked.
        if cfg.get("claude_enabled"):
            try:
                import claude_narrator
                if claude_narrator.has_usable_key():
                    narration = await _smart_charge_narrate(plan)
            except Exception as e:
                log.debug("claude narration skipped: %s", e)
        # The decisions table was silently empty for weeks because we
        # were calling a non-existent smart_charge.record_decision —
        # the AttributeError got eaten by the loop's broad except. Use
        # the actual energy_db method, with the Plan converted to a dict.
        try:
            state.energy.record_smart_charge_decision(
                device_sn=device_sn,
                plan=plan.to_dict() if hasattr(plan, "to_dict") else plan,
                executed=executed,
                narration=narration,
            )
        except Exception as e:
            log.warning("smart_charge: failed to record decision: %s", e)
    return plan


async def _smart_charge_narrate(plan) -> str:
    """Optional Claude narration. Off by default — only invoked when the
    user explicitly enables it in Settings. Stub returns empty string until
    the Anthropic SDK is wired up."""
    try:
        import claude_narrator
        return await claude_narrator.narrate_smart_charge(plan)
    except Exception as e:
        log.debug("claude narration failed: %s", e)
        return ""


async def smart_charge_loop():
    """Periodic tick — every 5 minutes, run the smart-charge evaluator
    for every device that has a per-device config saved (mode != off)."""
    bo = _backoff.LoopBackoff(max_s=30 * 60)
    while True:
        try:
            configs = smart_charge.get_all_configs()
            # Fall back to evaluating just the active device when no
            # per-device configs exist (legacy path).
            if not configs and state.device and state.device.device_sn:
                await _smart_charge_evaluate(record=True)
            for sn, cfg in configs.items():
                if cfg.get("mode") == "off":
                    continue
                try:
                    await _smart_charge_evaluate(record=True, device_sn=sn)
                except Exception as e:
                    log.warning("smart_charge tick failed for %s: %s", sn, e)
            bo.reset()
        except Exception as e:
            bo.record_failure()
            log.warning("smart_charge loop iteration failed: %s", e)
        await asyncio.sleep(bo.next_sleep(5 * 60))


# Per-device probe cadence after a failure: 5 min, 10, 20, 30, 30, ...
# Capped so we don't drift to every-few-hours and miss a recovery for
# an hour after the plug comes back. After a success, resets to 5 min.
KASA_RECONCILER_BASE_S = 5 * 60
KASA_RECONCILER_MAX_BACKOFF_S = 30 * 60


def _kasa_ui_signature(d: dict | None) -> tuple | None:
    """The user-visible bits of a Kasa device record. Used to decide
    whether a probe outcome warrants a WS broadcast — routine reconciler
    polls that don't change online/is_on/error don't need to wake every
    connected browser."""
    if not d:
        return None
    return (
        state.kasa.is_online(d),
        d.get("last_known_is_on"),
        d.get("last_error"),
    )


async def _kasa_update_probe_and_notify(host: str, **kwargs) -> None:
    """Wraps state.kasa.update_probe with a WS broadcast on user-visible
    state change. Cheap no-op when nothing changed (the common case
    during steady-state reconciler polls)."""
    before = _kasa_ui_signature(state.kasa.get(host))
    state.kasa.update_probe(host, **kwargs)
    after = _kasa_ui_signature(state.kasa.get(host))
    if before != after:
        await broadcast({
            "type": "kasa_updated",
            "data": {"host": host},
        })


async def kasa_reconciler_loop():
    """Periodically probe every saved Kasa plug and persist the
    outcome (last_seen, last_error, consecutive_failures) on the
    registry. Lets transient failures self-heal and surfaces
    persistent ones to the UI without making the user click around.

    Per-device exponential backoff so an offline plug doesn't get
    hammered every 5 min — but capped so a recovered plug isn't
    stuck offline forever (max wait between probes is 30 min)."""
    while True:
        try:
            now = time.time()
            for d in state.kasa.list_devices():
                fails = d.get("consecutive_failures") or 0
                last_failed = d.get("last_failed_ts") or 0
                # Exponential: 5min x 2^fails, capped. fails capped at
                # 5 to avoid overflow at extreme values.
                wait_s = min(
                    KASA_RECONCILER_BASE_S * (2 ** min(fails, 5)),
                    KASA_RECONCILER_MAX_BACKOFF_S,
                )
                if fails > 0 and (now - last_failed) < wait_s:
                    continue
                host = d.get("host")
                if not host:
                    continue
                try:
                    info = await kasa_client.status(host)
                    await _kasa_update_probe_and_notify(
                        host, success=True,
                        is_on=info.get("is_on"),
                        model=info.get("model"),
                        alias=info.get("alias"),
                    )
                except kasa_client.KasaConfigError as e:
                    # Bad creds or missing dep — actionable, not a flake.
                    # Surface at warning so it doesn't get lost in the
                    # info-level reconciler chatter.
                    await _kasa_update_probe_and_notify(
                        host, success=False, error=str(e),
                    )
                    log.warning("Kasa reconciler: %s misconfigured (%s)", host, e)
                except Exception as e:
                    # Transient (network blip, device busy). Per-device
                    # backoff handles repeats; log at info to keep the
                    # routine flake noise out of the warning channel.
                    await _kasa_update_probe_and_notify(
                        host, success=False, error=str(e),
                    )
                    log.info("Kasa reconciler: %s offline (%s)", host, e)
        except Exception as e:
            log.warning("kasa reconciler loop iteration failed: %s", e)
        await asyncio.sleep(KASA_RECONCILER_BASE_S)


def _db_pack_to_cloud_shape(row: dict) -> dict:
    """energy_db's per-row shape uses internal names; the UI + smart-charge
    expect the cloud's raw field names. Convert at the boundary so neither
    side has to know about the other.

    `it` is unconditionally dropped — see `_sanitize_pack_telemetry` —
    so historical rows persisted before the filter shipped don't leak
    bad temperatures into the UI or advisor."""
    return {
        "deviceSn": row.get("pack_sn"),
        "deviceOrder": row.get("device_order") or 0,
        "rb": row.get("soc_pct"),
        "ip": row.get("input_w"),
        "op": row.get("output_w"),
        "it": None,
        "ec": row.get("error_code") or 0,
    }


def _hydrate_battery_packs_from_db() -> None:
    """Seed state.battery_packs_by_sn from the latest energy_db snapshot
    for every device so fresh boots paint the packs card immediately on
    device switch, not just for the device that was active at startup.
    Leaves last_packs_ts_by_sn at 0 so the poll loop refreshes on its
    first iteration."""
    try:
        for d in state.energy.list_devices():
            sn = d.get("device_sn")
            if not sn:
                continue
            rows = state.energy.latest_battery_packs(sn)
            if rows:
                state.battery_packs_by_sn[sn] = [
                    _db_pack_to_cloud_shape(r) for r in rows
                ]
                log.info("Hydrated %d battery packs for %s from DB",
                         len(rows), sn)
    except Exception as e:
        log.debug("battery pack hydration skipped: %s", e)


async def _build_advisor_bundle(device_sn: str) -> dict:
    """Gather the data Claude needs to review yesterday's algorithm
    performance for one device. Plain JSON-serialisable dict — see
    claude_advisor._format_starter_bundle for the rendering."""
    from datetime import datetime, timezone
    def _iso(ts: int | float | None) -> str:
        if ts is None:
            return "—"
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

    dev_meta = next(
        (d for d in state.energy.list_devices()
         if d.get("device_sn") == device_sn),
        {},
    )
    main_soc = (state.last_status or {}).get("battery_percent") if state.device and state.device.device_sn == device_sn else None
    model_code = dev_meta.get("model_code")
    capacity = _total_capacity_wh(device_sn, model_code)
    sys_soc = _system_soc_pct(float(main_soc), device_sn, model_code) if main_soc is not None else None
    pack_count = len(state.battery_packs_by_sn.get(device_sn, []))

    cfg = smart_charge.get_config(device_sn)

    # Per-device fitted drain coefficients — let Claude see what the
    # simulator is actually using. Hybrid model:
    #   drain_w = parasitic_w + load_w * (1 + inverter_overhead_pct)
    # Both surfaced; advisor should reason about them together when
    # diagnosing load-accuracy gaps.
    fitted_parasitic_w: float | None = None
    fitted_overhead_pct: float | None = None
    fitted_drain_n: int = 0
    try:
        ehist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
        fitted_parasitic_w, fitted_overhead_pct, fitted_drain_n = (
            forecaster.fit_drain_model(ehist, capacity)
        )
    except Exception as e:
        log.debug("advisor: drain model fit failed: %s", e)

    main_wh = forecaster.battery_capacity_wh(model_code)
    pack_wh = forecaster.expansion_pack_capacity_wh(model_code)

    accuracy_summary = {}
    try:
        samples_acc = state.energy.prediction_accuracy(
            device_sn,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        )
        for s in samples_acc:
            h = s["lead_time_h"]
            bucket = "≤6h" if h <= 6 else "≤24h" if h <= 24 else "≤72h" if h <= 72 else ">72h"
            b = accuracy_summary.setdefault(bucket, {"n": 0, "sum_err": 0.0})
            b["n"] += 1
            b["sum_err"] += s["error"]
        for b in accuracy_summary.values():
            b["mae"] = round(b["sum_err"] / b["n"], 2) if b["n"] else 0
            del b["sum_err"]
    except Exception as e:
        log.debug("advisor: accuracy summary failed: %s", e)

    # Last 24h hourly history (energy_db.history with 1h buckets).
    # IMPORTANT: report the *integrated* W = Wh-per-hour for power
    # fields, not AVG(last_w) which is the average of instantaneous
    # samples. The latter biases low when brief high-load spikes
    # happen between polls (we'd see idle in most samples). The
    # integrated value is the true average power for the hour and
    # what reconciles with SOC drain.
    recent_samples = []
    try:
        for h in state.energy.history(device_sn, hours=24, bucket_s=3600):
            recent_samples.append({
                "hour": _iso(h["ts"]),
                "soc": h.get("battery_pct"),
                # Wh accumulated per 1h bucket → average W during that hour.
                "input_w_avg": int(h.get("input_wh") or 0),
                "output_w_avg": int(h.get("output_wh") or 0),
                "solar_w_avg": int(h.get("solar_wh") or 0),
                "ac_input_w_avg": int(h.get("ac_input_wh") or 0),
                # Also include instantaneous values for comparison —
                # divergence between _avg and _instant reveals brief
                # spikes the poller missed.
                "input_w_instant": h.get("input_w"),
                "output_w_instant": h.get("output_w"),
            })
    except Exception as e:
        log.debug("advisor: samples bundle failed: %s", e)

    # Last 24h weather observations.
    recent_weather = []
    try:
        since = int(time.time()) - 24 * 3600
        for w in state.energy.list_weather_observations(since_ts=since, limit=48):
            recent_weather.append({
                "hour": _iso(w["ts"]),
                "ghi_w_m2": w.get("ghi_w_m2"),
                "cloud_cover_pct": w.get("cloud_cover_pct"),
            })
    except Exception as e:
        log.debug("advisor: weather bundle failed: %s", e)

    # Predicted-vs-actual pairs from the last 48h target window — the
    # raw signal Claude needs to diagnose where the model is missing.
    # `made_iso` is included so Claude can correlate each row to the
    # `recent_code_changes` timestamps and ignore pre-fix predictions.
    recent_predictions = []
    try:
        cutoff = time.time() - 48 * 3600
        for p in state.energy.prediction_accuracy(
            device_sn,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        ):
            if p.get("target", 0) < cutoff:
                continue
            recent_predictions.append({
                "made_iso": _iso(p.get("made_at")),
                "target_iso": _iso(p["target"]),
                "lead_h": p["lead_time_h"],
                "predicted_soc": round(p["predicted_soc"], 1),
                "actual_soc": round(p["actual_soc"], 1),
                "error": round(p["error"], 1),
            })
        # Cap at most 60 rows so we don't blow the prompt budget.
        recent_predictions = recent_predictions[:60]
    except Exception as e:
        log.debug("advisor: predictions bundle failed: %s", e)

    # Smart-charge decisions joined to actuals.
    recent_decisions = []
    try:
        for d in state.energy.smart_charge_analytics(
            device_sn, days=7,
            main_capacity_wh=main_wh,
            pack_capacity_wh=pack_wh,
        ):
            recent_decisions.append({
                "decided_iso": _iso(d.get("decided_at")),
                "action": d.get("action"),
                "mode": d.get("mode"),
                "predicted_sunrise_soc_pct": d.get("predicted_sunrise_soc_pct"),
                "actual_sunrise_soc_pct": d.get("actual_sunrise_soc_pct"),
                "target_sunrise_soc_pct": d.get("target_sunrise_soc_pct"),
                "reason": d.get("reason"),
            })
    except Exception as e:
        log.debug("advisor: decisions bundle failed: %s", e)

    return {
        "window_label": f"last 48h ending {datetime.now().isoformat(timespec='minutes')}",
        "device_label": dev_meta.get("name") or "Jackery",
        "device_sn": device_sn,
        "capacity_wh": capacity,
        "pack_count": pack_count,
        "main_soc_pct": main_soc,
        "system_soc_pct": round(sys_soc, 1) if sys_soc is not None else None,
        "smart_charge_config": cfg,
        # Hybrid drain model: surface both terms so the advisor can
        # reason about parasitic baseline vs throughput-scaled overhead
        # separately. `fitted_idle_overhead_w` keeps its old name for
        # back-compat in the narrator/UI but now holds the parasitic_w
        # term directly (the right interpretation all along).
        "fitted_parasitic_w": (round(fitted_parasitic_w, 1)
                               if fitted_parasitic_w is not None else None),
        "fitted_inverter_overhead_pct": (round(fitted_overhead_pct, 4)
                                         if fitted_overhead_pct is not None else None),
        "fitted_drain_n_windows": fitted_drain_n,
        "fitted_idle_overhead_w": (round(fitted_parasitic_w, 1)
                                   if fitted_parasitic_w is not None else None),
        "fitted_idle_overhead_n_windows": fitted_drain_n,
        "forecast_accuracy_summary": accuracy_summary,
        "recent_samples": recent_samples,
        "recent_weather": recent_weather,
        "recent_predictions": recent_predictions,
        "recent_decisions": recent_decisions,
        # Hand-maintained list of recent fixes that change the meaning of
        # historical data. The advisor sees a 48h window, so every fix
        # less than 48h old has stale data on both sides of it; without
        # this hint Claude re-flags bugs we just shipped. Each entry
        # tells Claude "data older than `ts` was generated by buggy
        # code; don't bill the current code for it." Update by hand
        # when a fix touches forecaster / smart-charge / load model.
        # Keep in chronological order and prune entries older than ~7
        # days (they fall out of all advisor windows by then).
        "recent_code_changes": _recent_code_changes(),
    }


def _recent_code_changes() -> list[dict[str, Any]]:
    """Return the rolling list of fixes the advisor needs to know about
    when interpreting historical samples / predictions / decisions."""
    return [
        {
            "ts_iso": "2026-04-30T14:30:00+00:00",
            "subsystem": "smart_charge",
            "summary": (
                "Fixed smart_charge.record_decision() — was calling a "
                "non-existent method swallowed by a broad except, so the "
                "decisions table was empty for ~14 days. As of this "
                "timestamp, every smart-charge tick (incl. 'test' mode "
                "skips) writes a decisions row. Empty decisions before "
                "this is a logging bug, not a controller failure."
            ),
        },
        {
            "ts_iso": "2026-04-30T18:55:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Two material changes to address bugs YOU surfaced in a "
                "prior review: (1) added IDLE_OVERHEAD_W=600 to every "
                "expected_load_w() lookup so the load model accounts "
                "for inverter idle / DC-bus / balancing draw that "
                "doesn't show up in out_w but does drain the battery; "
                "(2) added ac_charge_floor_pct to simulate_soc — when "
                "smart-charge is enabled, SOC is clamped at the "
                "target_sunrise_soc_pct floor so long-lead predictions "
                "no longer saturate at 0%. Predictions made BEFORE this "
                "timestamp WILL show a 0% long-lead cliff and a 5-15pp "
                "short-lead under-bias; those are the bugs this commit "
                "fixed. Don't re-flag them — assess only predictions "
                "with made_at >= this timestamp."
            ),
        },
        {
            "ts_iso": "2026-05-01T16:00:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Re-tuned IDLE_OVERHEAD_W from 600 → 200 based on YOUR "
                "previous review's empirical reconciliation: steady-state "
                "windows showed actual constant overhead is closer to "
                "145-190W, and 600W was over-predicting drain by 300-450W."
            ),
        },
        {
            "ts_iso": "2026-05-01T17:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Replaced the hardcoded IDLE_OVERHEAD_W constant with a "
                "per-device auto-fit: fit_idle_overhead_w() now walks the "
                "user's own discharge history (bucket pairs with no solar, "
                "no AC charging, ≥1pp SOC drop) and computes the implied "
                "parasitic from observed SOC slope minus reported out_w, "
                "then takes the median across qualifying windows. The 200W "
                "constant is now just the cold-start fallback. The fitted "
                "value for THIS device is in the bundle as "
                "fitted_idle_overhead_w — use that when reasoning about "
                "load accuracy, not the constant. If the fitted value "
                "looks wrong, flag the FIT (data quality, edge cases), "
                "not the constant."
            ),
        },
        {
            "ts_iso": "2026-05-01T17:00:00+00:00",
            "subsystem": "telemetry",
            "summary": (
                "Per-pack temperatures (`internal_temp_c` / `it`) are "
                "now ALWAYS None — the field is unconditionally stripped "
                "at ingestion and at every read path. The Jackery "
                "5000 Plus's BMS reports unreliable per-pack temps "
                "across firmwares (observed 4°C with 20°C+ ambient, "
                "135°C while neighbors read 78°C, etc.) and the user "
                "explicitly asked us to ignore the field. Only the "
                "main unit's `bt` field (rendered as battery_temp_c) is "
                "trustworthy. Do NOT flag missing per-pack temps as a "
                "concern; do NOT propose tunables that depend on pack "
                "temperature. Pack thermal monitoring is out of scope."
            ),
        },
        {
            "ts_iso": "2026-05-01T18:35:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Inverter overhead model switched from a flat watt "
                "constant to a percentage of throughput. Was: "
                "expected_load = base + idle_overhead_w (200W default). "
                "Now: expected_load = base * (1 + inverter_overhead_pct) "
                "with default 0.10 (10%) — modern LiFePO4 inverters lose "
                "~10% as heat in DC->AC conversion, scales with load. "
                "fit_inverter_overhead_pct replaces fit_idle_overhead_w; "
                "the legacy fit_idle_overhead_w is now a thin shim that "
                "converts the percentage to watts at a typical 500W "
                "load. DEVICE_PARAM_KEYS exposes `inverter_overhead_pct` "
                "(unit=ratio); the bundle still carries idle_overhead_w "
                "for back-compat. When reasoning about load accuracy, "
                "prefer the percentage — it's the source of truth."
            ),
        },
        {
            "ts_iso": "2026-05-04T15:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Closed the predicted-vs-actual measurement asymmetry "
                "you flagged on 2026-05-04: prediction_accuracy and "
                "smart_charge_analytics now compute capacity-weighted "
                "system SOC for the actual side too, by joining "
                "battery_packs at target ±30min. Predicted (system) "
                "now compared to actual (system). Single-unit devices "
                "and pre-pack-recording history degenerate to the "
                "main-only behavior, so historical data isn't rewritten. "
                "Headline accuracy summary should drop several pp once "
                "fresh predictions accumulate; if long-lead MAE doesn't "
                "improve, the residual is a real solar/load-model "
                "defect, not the asymmetry."
            ),
        },
        {
            "ts_iso": "2026-05-05T03:00:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Drain model switched from pure-percentage to hybrid: "
                "drain_w = parasitic_w + load_w * (1 + overhead_pct), "
                "fit jointly via 2-param OLS on (load, drain) pairs. "
                "Closes the 'unaccounted ~430W gap' you flagged on "
                "2026-05-04 02:00→12:00 (and similar on 5/3 overnight): "
                "BMS + idle inverter + pack-balancing on multi-pack "
                "rigs is a near-constant baseline that the previous "
                "percentage-only model couldn't represent — its 50% "
                "overhead clamp rejected exactly the windows where "
                "this baseline showed up, falling back to the 10% "
                "default. The bundle now exposes `parasitic_w` "
                "alongside `inverter_overhead_pct`; reason about the "
                "two together when evaluating load accuracy. The legacy "
                "`idle_overhead_w` field now holds parasitic_w directly "
                "(it always meant absolute watts; only the fit was "
                "wrong). User confirmed no DC loads (USB/12V/car port) "
                "so the gap is genuine parasitic, not unmeasured load."
            ),
        },
        {
            "ts_iso": "2026-05-05T03:45:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Followup on the hybrid drain fit: you correctly "
                "flagged 03:13 that the OLS collapsed to (50W, 0.10) "
                "priors when the user's load distribution is narrow "
                "(steady ~470W overnight). Added a load-range gate: "
                "when load is narrow, fall back to a parasitic-only "
                "fit with overhead pinned at the default — solve "
                "parasitic_w = drain - load * (1 + default_pct) per "
                "window, take the median."
            ),
        },
        {
            "ts_iso": "2026-05-05T15:30:00+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Followup on the followup: you flagged 15:04 that "
                "the parasitic-only fallback STILL wasn't firing on "
                "the user's data — fit kept returning the 50W cold-"
                "start default. Root cause: the load-range gate used "
                "raw max/min, which gets fooled by a single outlier "
                "high-load window (1 kettle run during 14d history "
                "pushes max/min to 3.2x even when 99% of windows are "
                "tightly clustered at ~460W). Switched to p90/p10 "
                "percentile-based metric — outlier-resistant, "
                "correctly classifies the device as 'narrow' so the "
                "fallback fires. Should now recover parasitic_w ≈ "
                "80-100W on this device per your reconciliation "
                "(advisor said true value ≈ 84W after subtracting "
                "the 10% pinned overhead from 130W total)."
            ),
        },
        {
            "ts_iso": "2026-05-06T04:14:26+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Smart-charge floor (target_sunrise_soc_pct) is "
                "REMOVED from the displayed/persisted forecast in "
                "every mode. Previously the simulator clamped SOC at "
                "the target in active mode (and originally in test "
                "mode too) so the prediction reflected what the user "
                "would observe given controller intervention. User "
                "explicitly rejected this 2026-05-06: the prediction "
                "should show the TRUTH — what the battery will do "
                "without intervention — and the controller's effect "
                "is shown separately via the Plan (predicted vs "
                "target + deficit + charge schedule). Conflating "
                "them was a feedback loop and was hiding real model "
                "bias (advisor's 03:42 anomaly directly asked for "
                "the unclamped forecast). Post-fix: /api/forecast "
                "and forecast_predictions are baseline. compute_plan "
                "still uses baseline_predicted for deficit math "
                "(unchanged); with floor=None the `forecast` and "
                "`baseline_forecast` arguments are now identical, "
                "and Plan.predicted_sunrise_soc_pct == "
                "Plan.baseline_predicted_sunrise_soc_pct (both = "
                "truth). Predictions made BEFORE this timestamp in "
                "active mode show the floor clamp at target; those "
                "at or after are baseline. When evaluating accuracy "
                "for active-mode nights, expect persisted predictions "
                "to be lower than actuals by ~target-actual_unclamped "
                "— that's the controller's grid-charge work, not "
                "model bias. Use the smart_charge_decisions table to "
                "identify which nights had intervention."
            ),
        },
        {
            "ts_iso": "2026-05-06T14:14:33+00:00",
            "subsystem": "forecaster",
            "summary": (
                "Slope-based fits (drain model, charge efficiency, "
                "inverter overhead) now walk capacity-weighted SYSTEM "
                "SOC instead of main-pack SOC on multi-pack rigs. "
                "Root cause flagged by you 2026-05-06T13:47: with "
                "battery_pct = main pack and capacity_wh = system "
                "(30240 Wh on a 6-pack rig), the implied drain was "
                "the real drain × pack_ratio (~6×), inflating "
                "fitted parasitic_w to 316-370W vs the empirical "
                "~130W. Implementation: energy_db.history() takes "
                "optional (main_capacity_wh, pack_capacity_wh) and "
                "adds `system_soc` per row by joining the closest "
                "battery_packs snapshot (±30 min) — same logic as "
                "the prediction-accuracy capacity-weighting. "
                "forecaster's _row_soc() helper prefers system_soc "
                "and falls back to battery_pct, so single-unit "
                "devices and tests without capacity hints keep the "
                "old behavior. Slope-magnitude thresholds switched "
                "from pp ('≥2pp drop') to Wh ('≥100 Wh drained') so "
                "the gate is device-agnostic — same energy floor "
                "whether walking main or system. Expect parasitic_w "
                "to drop sharply on multi-pack devices when this "
                "rebuilds; the 3-8pp under-bias on long-lead "
                "predictions you flagged in today's INFO anomaly "
                "should resolve. Single-unit devices unchanged."
            ),
        },
    ]


def _parse_iso(s: str | None) -> int | None:
    """Loose ISO-8601 → unix-seconds parser for the advisor's tool args.
    Accepts trailing Z, offsetless naive datetimes (treated as UTC), or
    anything Python's fromisoformat understands."""
    if not s:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _make_advisor_query_fn(device_sn: str):
    """Build a closure that runs Claude's tool calls against the local
    DB. Each tool returns a JSON-serialisable dict; on bad inputs we
    return an `error` field rather than raising — Claude can then
    re-issue the call with corrected args."""
    from datetime import datetime, timezone

    main_wh, pack_wh = _capacity_hints(device_sn)

    def _iso(ts: float | int | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()

    async def query(name: str, args: dict) -> dict:
        if name == "query_samples":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            bucket_s = int(args.get("bucket_s") or 3600)
            if not start or not end:
                return {"error": "start_iso/end_iso required (ISO 8601)"}
            hours = max(1, (end - start) // 3600 + 1)
            rows = state.energy.history(device_sn, hours=hours, bucket_s=bucket_s)
            # Filter to the requested window (history() goes back N hours
            # from now; we then clip).
            out = []
            for r in rows:
                if r["ts"] < start or r["ts"] >= end:
                    continue
                out.append({
                    "ts": _iso(r["ts"]),
                    "soc": r.get("battery_pct"),
                    "in_w_avg": int(r.get("input_wh") or 0)
                              if bucket_s == 3600 else None,
                    "out_w_avg": int(r.get("output_wh") or 0)
                               if bucket_s == 3600 else None,
                    "solar_w_avg": int(r.get("solar_wh") or 0)
                                 if bucket_s == 3600 else None,
                    "ac_input_w_avg": int(r.get("ac_input_wh") or 0)
                                    if bucket_s == 3600 else None,
                    "in_w_instant": r.get("input_w"),
                    "out_w_instant": r.get("output_w"),
                    "solar_w_instant": r.get("solar_w"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_predictions":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            max_lead = args.get("max_lead_h")
            samples = state.energy.prediction_accuracy(
                device_sn,
                main_capacity_wh=main_wh,
                pack_capacity_wh=pack_wh,
            )
            out = []
            for p in samples:
                if start and p.get("target", 0) < start:
                    continue
                if end and p.get("target", 0) >= end:
                    continue
                if max_lead is not None and p.get("lead_time_h", 0) > max_lead:
                    continue
                out.append({
                    "made_at": _iso(p.get("made_at")),
                    "target": _iso(p.get("target")),
                    "lead_time_h": p.get("lead_time_h"),
                    "predicted_soc": round(p.get("predicted_soc", 0), 1),
                    "actual_soc": round(p.get("actual_soc", 0), 1),
                    "error_pp": round(p.get("error", 0), 1),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_decisions":
            start = _parse_iso(args.get("start_iso"))
            end = _parse_iso(args.get("end_iso"))
            samples = state.energy.smart_charge_analytics(
                device_sn, days=90,
                main_capacity_wh=main_wh,
                pack_capacity_wh=pack_wh,
            )
            out = []
            for d in samples:
                if start and (d.get("decided_at") or 0) < start:
                    continue
                if end and (d.get("decided_at") or 0) >= end:
                    continue
                out.append({
                    "decided_at": _iso(d.get("decided_at")),
                    "action": d.get("action"),
                    "mode": d.get("mode"),
                    "predicted_sunrise_soc_pct": d.get("predicted_sunrise_soc_pct"),
                    "actual_sunrise_soc_pct": d.get("actual_sunrise_soc_pct"),
                    "target_sunrise_soc_pct": d.get("target_sunrise_soc_pct"),
                    "reason": d.get("reason"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_weather":
            start = _parse_iso(args.get("start_iso")) or 0
            end = _parse_iso(args.get("end_iso")) or int(time.time())
            obs = state.energy.list_weather_observations(since_ts=start, limit=2000)
            out = []
            for w in obs:
                if w["ts"] >= end:
                    continue
                out.append({
                    "hour": _iso(w["ts"]),
                    "ghi_w_m2": w.get("ghi_w_m2"),
                    "cloud_cover_pct": w.get("cloud_cover_pct"),
                })
            return {"rows": out[:500], "row_count": len(out),
                    "truncated": len(out) > 500}

        if name == "query_battery_packs":
            packs = state.energy.latest_battery_packs(device_sn)
            # Per-pack `it` is unconditionally dropped — see the comment
            # on _sanitize_pack_telemetry. Strip historical rows here so
            # the advisor never sees garbage values from before the
            # filter shipped.
            cleaned = [{**r, "internal_temp_c": None} for r in packs]
            return {"rows": cleaned, "row_count": len(cleaned)}

        return {"error": f"unknown tool: {name}"}

    return query


async def _run_advisor_review(device_sn: str) -> dict:
    """Build the starter bundle, run Claude through the agentic
    multi-turn loop with DB-query tools, persist whatever suggestions
    and anomalies come back."""
    import claude_advisor
    if not claude_advisor.has_usable_key():
        return {"ok": False, "reason": "no_api_key"}
    bundle = await _build_advisor_bundle(device_sn)
    query_fn = _make_advisor_query_fn(device_sn)
    result = await claude_advisor.review(bundle, query_fn=query_fn)
    if result.get("skipped_reason") and result["skipped_reason"] not in ("no_tool_call", "turn_cap_reached"):
        return {"ok": False, "reason": result["skipped_reason"]}

    # Auto-expire stale pending suggestions before adding new ones, so
    # the user's pending list doesn't grow unboundedly.
    state.energy.expire_old_suggestions()

    new_ids: list[int] = []
    for s in result.get("config_suggestions", []):
        try:
            sid = state.energy.insert_suggestion(
                device_sn=device_sn,
                kind="config",
                target=s["target"],
                current_value=s["current_value"],
                proposed_value=s["proposed_value"],
                reasoning=s["reasoning"],
                confidence=s["confidence"],
                severity=None,
            )
            new_ids.append(sid)
        except Exception as e:
            log.warning("advisor: failed to persist suggestion %s: %s", s, e)

    for a in result.get("anomalies", []):
        try:
            sid = state.energy.insert_suggestion(
                device_sn=device_sn,
                kind="anomaly",
                target=None,
                current_value=None,
                proposed_value=None,
                reasoning=a.get("description", ""),
                confidence=None,
                severity=a.get("severity"),
            )
            new_ids.append(sid)
        except Exception as e:
            log.warning("advisor: failed to persist anomaly %s: %s", a, e)

    log.info("advisor: %s — %d suggestions, %d anomalies in %d turns "
             "(%d tool calls, model=%s)",
             device_sn,
             len(result.get("config_suggestions", [])),
             len(result.get("anomalies", [])),
             result.get("turns", 0),
             result.get("tool_calls", 0),
             result.get("model"))
    return {
        "ok": True,
        "summary": result.get("summary", ""),
        "new_suggestion_ids": new_ids,
        "model": result.get("model"),
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", 0),
    }


async def advisor_loop():
    """Run the advisor once per device per day. Anchored to ~8am local
    time when location info is available, otherwise just every 24h
    from the first tick. Skipping is cheap (no key / no SDK) so we
    iterate every hour to keep the wake-up logic simple."""
    bo = _backoff.LoopBackoff(max_s=4 * 3600)
    while True:
        try:
            await asyncio.sleep(60)  # warm-up — let credentials load
            try:
                import claude_advisor
            except Exception:
                claude_advisor = None
            if claude_advisor is None or not claude_advisor.has_usable_key():
                # Try again in an hour — user may save a key later.
                await asyncio.sleep(3600)
                continue
            now = time.time()
            tz_off = device_location.get_tz_offset() or 0
            local_hour = (int(now + tz_off) // 3600) % 24
            # Run once when local hour first equals our trigger hour.
            if local_hour == user_settings.get("advisor_trigger_hour"):
                for d in state.energy.list_devices():
                    sn = d.get("device_sn")
                    if not sn:
                        continue
                    last = state.last_advisor_run_by_sn.get(sn, 0.0)
                    if now - last < 23 * 3600:
                        continue
                    try:
                        await _run_advisor_review(sn)
                    except Exception as e:
                        log.warning("advisor loop: %s failed: %s", sn, e)
                    state.last_advisor_run_by_sn[sn] = now
            bo.reset()
        except Exception as e:
            bo.record_failure()
            log.warning("advisor loop iteration failed: %s", e)
        # Tick every hour. The local-time gate inside ensures we only
        # actually run reviews once per device per day.
        await asyncio.sleep(bo.next_sleep(3600))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Jackery monitor on backend=%s", state.backend)
    # Pre-load the last persisted pack snapshot so the UI shows something
    # immediately on subsequent boots (live refresh overwrites within seconds).
    _hydrate_battery_packs_from_db()
    # try to connect at startup, but don't block app boot if it fails
    asyncio.create_task(connect_device())
    state.poll_task = asyncio.create_task(poll_loop())
    state.smart_charge_task = asyncio.create_task(smart_charge_loop())
    state.advisor_task = asyncio.create_task(advisor_loop())
    state.kasa_reconciler_task = asyncio.create_task(kasa_reconciler_loop())
    state.forecast_recorder_task = asyncio.create_task(forecast_recorder_loop())
    # Backup runs daily at the user-configured local time. The schedule
    # callback re-reads settings on every iteration so the user can
    # change the time at runtime without an app restart.
    state.backup_task = asyncio.create_task(
        backup.backup_loop(
            get_schedule=lambda: user_settings.get("backup_schedule_hour"),
            get_keep_count=lambda: user_settings.get("backup_keep_count"),
        ),
    )
    yield
    if state.poll_task:
        state.poll_task.cancel()
    if getattr(state, "smart_charge_task", None):
        state.smart_charge_task.cancel()
    if getattr(state, "advisor_task", None):
        state.advisor_task.cancel()
    if getattr(state, "kasa_reconciler_task", None):
        state.kasa_reconciler_task.cancel()
    if getattr(state, "forecast_recorder_task", None):
        state.forecast_recorder_task.cancel()
    if getattr(state, "backup_task", None):
        state.backup_task.cancel()
    try:
        await state.client.disconnect()
    except Exception:
        pass


app = FastAPI(title="Jackery 5000 Plus Monitor", lifespan=lifespan)


api_auth.install(app, WEB_DIR)


@app.get("/api/status")
def api_status(request: Request):
    view_id = request.cookies.get(VIEW_DEVICE_COOKIE)
    return serialize_status(view_device_id=view_id)


@app.post("/api/reconnect")
async def api_reconnect():
    try:
        await state.client.disconnect()
    except Exception:
        pass
    state.device = None
    ok = await connect_device()
    return {"ok": ok, "error": state.connection_error, "backend": state.backend}


@app.get("/api/devices/params")
def api_devices_params(device_sn: str | None = None,
                        debug_key: str | None = None):
    """Return every resolvable per-device parameter, with source +
    confidence. The Device tab renders this as a "Learned parameters"
    panel: each row shows the value, where it came from
    (user/fit/probe/catalog/default/unknown), and an override field.

    The resolution ladder lives in `resolve_device_param`. The keys
    exposed here are `energy_db.DEVICE_PARAM_KEYS`, so adding a new
    resolvable param automatically gets it into the UI.

    `debug_key`: when set to a fit-backed param name, returns the
    actual samples that fed the fit so you can inspect why a value
    landed where it did. Currently supported for max_charge_w."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(400, "no active device")
    out = []
    for key, meta in energy_db.DEVICE_PARAM_KEYS.items():
        resolved = resolve_device_param(device_sn, key)
        out.append({"key": key, **meta, **resolved})
    response: dict[str, Any] = {
        "device_sn": device_sn,
        "params": out,
        # Expose the resolution context so the UI can show "we're
        # using tz_offset=-28800s" — useful when debugging why a fit
        # is producing weird values (a 0 here often means the user's
        # location wasn't set, which breaks the night-band fallback).
        "tz_offset_seconds": int(device_location.get_tz_offset() or 0),
    }
    if debug_key == "max_charge_w":
        try:
            ehist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
            tz_off = device_location.get_tz_offset() or 0
            wx = state.energy.list_weather_observations(
                since_ts=int(time.time()) - 14 * 86400, limit=14 * 24,
            )
            samples, n_used = forecaster.fit_max_charge_w(
                ehist, tz_offset_seconds=int(tz_off),
                weather_hourly=wx, return_candidates=True,
            )
            response["debug"] = {
                "key": "max_charge_w",
                "tz_offset_seconds": int(tz_off),
                "weather_observations": len(wx),
                "n_used_in_fit": n_used,
                "samples": samples,  # list of {ts, input_w, ac_input_w, solar_w, ghi_w_m2, value_used, path}
            }
        except Exception as e:
            response["debug"] = {"key": "max_charge_w", "error": str(e)}
    return response


@app.post("/api/devices/params")
async def api_devices_params_set(req: Request):
    """Manually override a per-device parameter. Body: {device_sn, key,
    value}. Writes a 'user' row that takes priority over fit/probe/
    catalog. Pass value=null to clear (resolver falls back to next
    ladder step)."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body") from None
    device_sn = body.get("device_sn")
    key = body.get("key")
    value = body.get("value")
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn or not key:
        raise HTTPException(400, "device_sn and key required")
    if key not in energy_db.DEVICE_PARAM_KEYS:
        raise HTTPException(400, f"unknown param key: {key!r}")
    if value is None:
        state.energy.clear_device_param(device_sn, key)
    else:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "value must be numeric or null") from None
        state.energy.set_device_param(device_sn, key, v, source="user",
                                      note="set via Device tab")
    return {"device_sn": device_sn, "key": key,
            **resolve_device_param(device_sn, key)}


@app.post("/api/devices/params/refit")
async def api_devices_params_refit(req: Request):
    """Force-clear any cached fit/probe/catalog row and re-resolve.
    Useful when you've just deployed a fit improvement and want to see
    the new value immediately instead of waiting for next call."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body") from None
    device_sn = body.get("device_sn")
    key = body.get("key")
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn or not key:
        raise HTTPException(400, "device_sn and key required")
    if key not in energy_db.DEVICE_PARAM_KEYS:
        raise HTTPException(400, f"unknown param key: {key!r}")
    # Drop the stored row (if any) and clear the in-process history
    # memo so the live fit picks up the latest data.
    state.energy.clear_device_param(device_sn, key)
    _param_fit_cache.pop((device_sn, "_history"), None)
    return {"device_sn": device_sn, "key": key,
            **resolve_device_param(device_sn, key)}


@app.get("/api/devices/probe_results")
def api_devices_probe_results(device_sn: str | None = None):
    """Return the latest auto-probe results for one device — the raw
    cloud responses we collected when we first noticed an unknown
    model_code, plus any capacity candidates the heuristic extractor
    found. The Device tab uses this to render a "we found 5040 Wh in
    /v1/device/info, use it?" prompt for unknown-model setups."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"error": "no device", "found": False}
    result = _auto_probe_results.get(device_sn)
    if not result:
        return {"device_sn": device_sn, "found": False,
                "in_flight": device_sn in _auto_probe_in_flight}
    return {"device_sn": device_sn, "found": True,
            "in_flight": device_sn in _auto_probe_in_flight, **result}


@app.post("/api/devices/probe_now")
async def api_devices_probe_now(device_sn: str | None = None):
    """Manually trigger an auto-probe for one device. Useful when the
    automatic trigger missed (e.g. the bridge wasn't ready when the
    device was first seen)."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    cloud_meta = state.last_cloud_meta or {}
    devs = cloud_meta.get("devices") or []
    target = next((d for d in devs if d.get("device_sn") == device_sn), None)
    if not target:
        raise HTTPException(404, "device not found in current cloud_meta")
    mc = target.get("model_code")
    device_id = target.get("device_id")
    if not device_id:
        raise HTTPException(400, "no device_id available for this device")
    asyncio.create_task(_auto_probe_device(
        device_sn, device_id, int(mc) if mc is not None else 0,
        target.get("model_name") or target.get("name") or "",
    ))
    return {"device_sn": device_sn, "started": True}


@app.get("/api/devices")
def api_devices():
    """Return the list of devices on the user's Jackery account.

    Each device is annotated with `model_recognized` (whether its
    model_code is in the bundled `models.json` catalog) and
    `inferred_capacity_wh` (the capacity we'd use if no per-device
    override exists). The Device tab uses these to show a "help us add
    this model" banner when a new device shows up that isn't yet in the
    catalog — see README's "Adding a new Jackery model" for the PR
    workflow."""
    cloud_meta = state.last_cloud_meta or {}
    raw = cloud_meta.get("devices") or []
    annotated = []
    for d in raw:
        if not isinstance(d, dict):
            annotated.append(d)
            continue
        mc = d.get("model_code")
        recognized = (mc is not None
                      and int(mc) in forecaster.BATTERY_CAPACITY_WH)
        annotated.append({
            **d,
            "model_recognized": recognized,
            "inferred_capacity_wh": forecaster.battery_capacity_wh(mc),
        })
    return {
        "devices": annotated,
        "selected_device_id": cloud_meta.get("selected_device_id"),
    }


@app.get("/api/energy/totals")
def api_energy_totals(device_sn: str | None = None):
    """Lifetime + today + 7d + 30d totals + dollar savings.
       If device_sn omitted, returns totals for the currently-active device."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "lifetime": {"input_wh": 0, "output_wh": 0}}
    return _decorate_totals_with_savings(state.energy.totals(device_sn), device_sn)


@app.get("/api/cost/plan")
def api_cost_get():
    """Return the saved plan plus the list of presets the UI can offer."""
    return {"plan": cost_module.get_plan(), "presets": cost_module.list_presets()}


@app.post("/api/cost/plan")
async def api_cost_set(req: Request):
    """Persist a new electricity plan. Body shape per cost.py docstring."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    saved = cost_module.set_plan(body if isinstance(body, dict) else {})
    if saved is None:
        raise HTTPException(status_code=400, detail="invalid plan shape")
    return {"plan": saved}


@app.get("/api/energy/history")
def api_energy_history(hours: int = 24, device_sn: str | None = None):
    """Time-series energy history for a device.
       hours: 6, 24, 168 (=7d), 720 (=30d). Bucket size auto-scales."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "history": []}
    hours = max(1, min(hours, 24 * 365))
    # Auto-pick a sensible bucket size: ~120 points across the window
    bucket_s = max(60, (hours * 3600) // 120)
    return {
        "device_sn": device_sn,
        "hours": hours,
        "bucket_s": bucket_s,
        "history": state.energy.history(device_sn, hours=hours, bucket_s=bucket_s),
    }


@app.get("/api/energy/devices")
def api_energy_devices():
    """All devices ever recorded, with their totals (for cross-device comparison)."""
    return {"devices": state.energy.all_totals()}


async def _build_and_record_forecast(device_sn: str | None) -> dict:
    """Resolve per-device context (model_code, current SOC, capacity)
    and build a forecast, persisting the resulting predictions to
    `prediction_accuracy` when ready. Shared by `/api/forecast` and the
    `forecast_recorder_loop` background task so both paths produce
    identical prediction rows.

    Returns the full response dict the API surfaces, including
    {error, configured} sentinels so the API can pass it through and
    the background loop can log a single line."""
    loc = device_location.get()
    if not loc:
        return {"error": "location not set", "configured": False}

    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"error": "no active device", "configured": True}

    # Resolve model_code + current SOC from the *requested* device's
    # cloud_meta entry, not from `state.device` — otherwise asking
    # about a secondary device while bridge-active is the primary
    # mixes the two devices' data into one forecast.
    bridge_active_sn = state.device.device_sn if state.device else None
    bridge_is_target = bridge_active_sn and str(bridge_active_sn) == str(device_sn)
    model_code: int | None = None
    main_soc = 50.0
    cloud = state.last_cloud_meta or {}
    if bridge_is_target:
        model_code = getattr(state.device, "model_code", None)
        if state.last_status and state.last_status.get("battery_percent") is not None:
            main_soc = float(state.last_status["battery_percent"])
    else:
        for d in (cloud.get("devices") or []):
            if str(d.get("device_sn")) == str(device_sn):
                mc = d.get("model_code")
                model_code = int(mc) if mc is not None else None
                break
        entry = (cloud.get("devices_telemetry") or {}).get(device_sn) or {}
        tele = entry.get("telemetry") or {}
        if tele.get("battery_percent") is not None:
            main_soc = float(tele["battery_percent"])

    capacity = _total_capacity_wh(device_sn, model_code)
    starting_soc = _system_soc_pct(main_soc, device_sn, model_code)
    main_wh, pack_wh = _capacity_hints(device_sn)

    energy_hist = state.energy.history(
        device_sn, hours=14 * 24, bucket_s=3600,
        main_capacity_wh=main_wh, pack_capacity_wh=pack_wh,
    )
    weather = await weather_client.fetch_irradiance(loc["latitude"], loc["longitude"])
    if weather.get("error"):
        return {"error": f"weather fetch failed: {weather['error']}", "configured": True}

    result = forecaster.build_forecast(
        energy_history=energy_hist,
        weather_hourly=weather["hourly"],
        starting_soc_pct=starting_soc,
        capacity_wh=capacity,
        ac_charge_floor_pct=_smart_charge_floor_pct(device_sn),
    )
    # Only persist when the forecast is actually fit — recording an
    # empty placeholder would corrupt prediction-accuracy analytics.
    if result.get("ready"):
        state.energy.record_forecast(device_sn, time.time(), result["forecast"])
    return {
        "device_sn": device_sn,
        "low_battery_threshold": user_settings.get("low_battery_threshold"),
        "main_soc_pct": main_soc,
        "system_soc_pct": starting_soc,
        "pack_count": len(state.battery_packs_by_sn.get(device_sn, [])),
        **result,
        "configured": True,
    }


@app.get("/api/forecast")
async def api_forecast(device_sn: str | None = None, _diag: int = 0):
    """SOC forecast for the next ~5 days based on weather + per-device history.

    Returns the simulated SOC curve plus the fitted model coefficients so the
    UI can show how confident the prediction is.

    `_diag=1` augments the response with `idle_window_diag`, a per-rejection-
    reason breakdown for the inverter-overhead-fit gate. Use this when a
    device is stuck in `calibrating` and you want to know which usage
    pattern is the blocker (e.g. AC always plugged in vs solar always
    connected vs loads too light)."""
    result = await _build_and_record_forecast(device_sn)
    if _diag and device_sn:
        try:
            ehist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
            result["idle_window_diag"] = forecaster.diagnose_idle_windows(ehist)
        except Exception as e:
            log.debug("forecast diag failed: %s", e)
            result["idle_window_diag"] = {"error": str(e)}
    return result


# Hourly cadence for the periodic forecast recorder. The advisor's
# accuracy join needs predictions that have aged into the past — at
# this cadence each device gets ~24 fresh prediction snapshots per
# day, plenty for the lead-time-bucket MAE summaries even when smart-
# charge is off and the user never opens the Forecast tab. Open-Meteo
# itself updates hourly, and weather_client caches between calls so
# multi-device accounts don't re-hit the API.
FORECAST_RECORDER_INTERVAL_S = 3600
# Wait for the bridge to populate cloud_meta + state.device before
# the first iteration; otherwise we'd skip that pass with "no
# devices" warnings on every container restart.
FORECAST_RECORDER_INITIAL_DELAY_S = 90


async def _record_forecasts_for_all_devices() -> None:
    """Build + persist a forecast for every device on the account that
    we have recent cloud_meta for. Skips devices whose forecaster isn't
    ready (returns ready=False); those will start landing rows once
    enough history accumulates. Errors are caught per-device so one
    bad device can't sink the rest of the iteration."""
    cloud = state.last_cloud_meta or {}
    devs = cloud.get("devices") or []
    if not devs:
        log.debug("forecast_recorder: no devices in cloud_meta yet, skipping")
        return
    if not device_location.get():
        log.debug("forecast_recorder: location not set, skipping")
        return
    recorded = skipped = 0
    for d in devs:
        sn = d.get("device_sn")
        if not sn:
            continue
        try:
            result = await _build_and_record_forecast(sn)
            if result.get("ready") and result.get("forecast"):
                recorded += 1
            else:
                skipped += 1
        except Exception as e:
            log.warning("forecast_recorder: build for %s failed: %s", sn, e)
            skipped += 1
    log.info("forecast_recorder: recorded predictions for %d device(s); skipped %d (calibrating or error)",
             recorded, skipped)


async def forecast_recorder_loop() -> None:
    """Build per-device forecasts on a fixed cadence so the prediction-
    accuracy table keeps accumulating data regardless of smart-charge
    state or Forecast-tab traffic. Without this, prediction_accuracy
    only gets new rows when (a) the user opens the Forecast tab or
    (b) the smart-charge controller is enabled and ticking — both can
    be quiet for days, which then leaves the daily advisor with no
    post-deploy predictions to evaluate (advisor anomaly flagged
    2026-05-02)."""
    await asyncio.sleep(FORECAST_RECORDER_INITIAL_DELAY_S)
    while True:
        try:
            await _record_forecasts_for_all_devices()
        except Exception as e:
            log.warning("forecast_recorder loop iteration failed: %s", e)
        await asyncio.sleep(FORECAST_RECORDER_INTERVAL_S)


def _bucket_accuracy(samples: list[dict]) -> dict[str, dict[str, float]]:
    """Aggregate prediction-accuracy rows into MAE-per-lead-bucket. Pure;
    no I/O. Used for both the legacy 14d summary and the post-fix slice."""
    summary: dict[str, dict[str, float]] = {}
    for s in samples:
        h = s["lead_time_h"]
        bucket = "≤6h" if h <= 6 else "≤24h" if h <= 24 else "≤72h" if h <= 72 else ">72h"
        b = summary.setdefault(bucket, {"n": 0, "sum_err": 0.0})
        b["n"] += 1
        b["sum_err"] += s["error"]
    for b in summary.values():
        b["mae"] = round(b["sum_err"] / b["n"], 2) if b["n"] else 0
        del b["sum_err"]
    return summary


@app.get("/api/forecast/accuracy")
def api_forecast_accuracy(device_sn: str | None = None,
                          since_ts: int | None = None):
    """Predicted vs actual SOC for past forecasts. Joins each saved
    prediction to the average actual battery_pct in the ±30 min window
    around its target. Useful for evaluating how the model improves
    as more data accumulates.

    Returns two summaries:
      - `summary`: legacy 14d window over ALL stored predictions
        (including any made by older code versions)
      - `summary_post_fix`: same buckets restricted to predictions made
        AT OR AFTER the forecaster's most recent breaking change
        (`FORECASTER_BREAKING_CHANGE_TS`). This is the more honest
        signal of current model behavior.

    `since_ts`: optional override for the post-fix cutoff. Defaults
    to FORECASTER_BREAKING_CHANGE_TS when not provided."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {
            "device_sn": None, "samples": [],
            "summary": {}, "summary_post_fix": {},
            "cutoff_ts": FORECASTER_BREAKING_CHANGE_TS,
        }
    main_wh, pack_wh = _capacity_hints(device_sn)
    samples = state.energy.prediction_accuracy(
        device_sn,
        main_capacity_wh=main_wh,
        pack_capacity_wh=pack_wh,
    )
    cutoff = int(since_ts) if since_ts is not None else FORECASTER_BREAKING_CHANGE_TS
    samples_post_fix = [s for s in samples if (s.get("made_at") or 0) >= cutoff]
    return {
        "device_sn": device_sn,
        "samples": samples,
        "summary": _bucket_accuracy(samples),
        "summary_post_fix": _bucket_accuracy(samples_post_fix),
        "cutoff_ts": cutoff,
    }


@app.get("/api/daily_summary")
def api_daily_summary(device_sn: str | None = None, days: int = 7):
    """Daily sunset/sunrise predicted vs actual SOC rows. Sourced from
    daily_solar_summary, written by the smart-charge tick. Used by the
    Logs → Debug panel for at-a-glance comparison."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "rows": []}
    days = max(1, min(int(days), 90))
    return {"device_sn": device_sn, "days": days,
            "rows": state.energy.list_daily_summary(device_sn, days=days)}


@app.get("/api/smart_charge/config")
def api_smart_charge_get(device_sn: str | None = None):
    """Per-device smart-charge config. Defaults to the active device
    when device_sn is omitted. Kasa devices are fetched separately via
    /api/kasa/saved so the UI can filter by Jackery assignment."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    return {
        "device_sn": device_sn,
        "config": smart_charge.get_config(device_sn),
    }


@app.post("/api/smart_charge/config")
async def api_smart_charge_set(req: Request, device_sn: str | None = None):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(status_code=400,
                            detail="no active device — pass device_sn explicitly")
    saved = smart_charge.set_config(body if isinstance(body, dict) else {},
                                    device_sn=device_sn)
    return {"device_sn": device_sn, "config": saved}


@app.get("/api/smart_charge/decision_details")
def api_smart_charge_decision_details(decided_at: int, device_sn: str | None = None):
    """Drill-down for a single smart-charge decision row. Returns the
    decision plus everything the controller saw at that moment:

      - The full decision dict (raw fields incl. deficit_kwh, window
        timestamps, sunrise_ts, cheapest_rate).
      - The forecast trace that was used (forecast_predictions for this
        device with made_at within ±10min of decided_at), so you can
        see hour-by-hour predicted SOC into the next day.
      - Weather observations from sunset through sunrise (GHI + cloud
        cover) — the inputs that drove the solar piece of the forecast.
      - Sample trace from sunset through "now" (or sunrise if past) —
        the actual SOC trajectory, for visual predicted-vs-actual.
      - Current resolved device parameters (capacity, idle_overhead,
        charge_efficiency, max_charge_w) — the values the simulator
        would have used for this run.

    Most data comes from existing tables; no fresh fits or RPCs.
    """
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(400, "no active device")
    decisions = state.energy.list_smart_charge_decisions(device_sn, limit=200)
    decision = next((d for d in decisions
                     if int(d.get("decided_at") or 0) == int(decided_at)), None)
    if not decision:
        raise HTTPException(404, "decision not found")

    # Forecast trace from the snapshot taken closest to (and at-or-
    # before) this decision. `record_forecast` floors made_at to the
    # hour, so the matching row is typically up to 1h earlier than
    # decided_at — we find that hour explicitly rather than a tight
    # symmetric window.
    forecast_trace: list[dict] = []
    forecast_made_at: int | None = None
    try:
        with state.energy._conn() as c:
            row = c.execute(
                """SELECT MAX(made_at) FROM forecast_predictions
                    WHERE device_sn = ? AND made_at <= ?""",
                (device_sn, int(decided_at)),
            ).fetchone()
            forecast_made_at = int(row[0]) if row and row[0] else None
            if forecast_made_at is not None:
                rows = c.execute(
                    """SELECT made_at, target, predicted_soc
                         FROM forecast_predictions
                        WHERE device_sn = ? AND made_at = ?
                          AND target >= ?
                        ORDER BY target
                        LIMIT 200""",
                    (device_sn, forecast_made_at, int(decided_at)),
                ).fetchall()
                forecast_trace = [
                    {"made_at": r[0], "target": r[1],
                     "predicted_soc": float(r[2])} for r in rows
                ]
    except Exception as e:
        log.debug("forecast trace lookup failed: %s", e)

    # Weather sunset→sunrise. Pull a 24h window starting at decided_at
    # to keep the query simple; the UI filters to the sunset/sunrise
    # band visually.
    weather_obs: list[dict] = []
    try:
        for w in state.energy.list_weather_observations(
            since_ts=decided_at - 3600, limit=72,
        ):
            if w.get("ts", 0) > decided_at + 24 * 3600:
                continue
            weather_obs.append(w)
    except Exception as e:
        log.debug("weather lookup failed: %s", e)

    # Actual SOC trajectory from decided_at through now (or sunrise+1h
    # if past, so the user can see the actual sunrise SOC alongside
    # the predicted one). Use the existing history() helper at 10-min
    # resolution since the bucket aggregator runs at minute-level.
    samples_trace: list[dict] = []
    try:
        sunrise_ts = int(decision.get("sunrise_ts") or 0)
        end_ts = min(int(time.time()),
                     sunrise_ts + 3600 if sunrise_ts else int(time.time()))
        hours = max(1, (end_ts - decided_at) // 3600 + 1)
        all_history = state.energy.history(device_sn, hours=hours, bucket_s=600)
        samples_trace = [
            {"ts": r["ts"], "soc": r.get("battery_pct"),
             "input_w": r.get("input_w"), "output_w": r.get("output_w"),
             "solar_w": r.get("solar_w")}
            for r in all_history
            if r.get("ts", 0) >= decided_at and r.get("ts", 0) <= end_ts
        ]
    except Exception as e:
        log.debug("samples trace lookup failed: %s", e)

    # Resolved device params (current values — useful for "is the
    # simulator using something reasonable now?"). Same shape as
    # /api/devices/params.
    resolved_params = []
    for k, meta in energy_db.DEVICE_PARAM_KEYS.items():
        try:
            r = resolve_device_param(device_sn, k)
            resolved_params.append({"key": k, **meta, **r})
        except Exception:
            continue

    return {
        "device_sn": device_sn,
        "decision": decision,
        "forecast_trace": forecast_trace,
        "forecast_made_at": forecast_made_at,
        "weather": weather_obs,
        "samples_trace": samples_trace,
        "resolved_params": resolved_params,
    }


@app.get("/api/smart_charge/status")
def api_smart_charge_status(device_sn: str | None = None):
    """Latest decision + recent history + observed AC charging rate for
    the UI status panel. History pulls from the persisted log in
    energy_db so it survives container restarts.

    `observed_max_charge_w` is fit from the user's own input_w samples
    (95th percentile of values >= 100W), so the UI can show their
    actual charging rate instead of a hardcoded 5000-Plus number. Falls
    back to None when too few charging observations exist yet."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    history: list[dict] = []
    observed_w: float | None = None
    observed_n: int = 0
    if device_sn:
        history = state.energy.list_smart_charge_decisions(device_sn, limit=50)
        try:
            ehist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
            tz_off = device_location.get_tz_offset() or 0
            wx = state.energy.list_weather_observations(
                since_ts=int(time.time()) - 14 * 86400, limit=14 * 24,
            )
            observed_w, observed_n = forecaster.fit_max_charge_w(
                ehist, tz_offset_seconds=int(tz_off), weather_hourly=wx,
            )
        except Exception as e:
            log.debug("observed_max_charge_w fit failed: %s", e)
    return {"device_sn": device_sn,
            "config": smart_charge.get_config(device_sn),
            "observed_max_charge_w": (round(observed_w) if observed_w is not None else None),
            "observed_max_charge_n": observed_n,
            "history": history}


@app.get("/api/smart_charge/analytics")
def api_smart_charge_analytics(device_sn: str | None = None, days: int = 14):
    """Predicted-vs-actual sunrise SOC pairs for the last N days. Joins
    every decision row whose sunrise_ts is in the past with the actual
    last_battery_pct from the samples table at that moment."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "samples": []}
    days = max(1, min(int(days), 90))
    samples = state.energy.smart_charge_analytics(device_sn, days=days)
    summary = {"n": len(samples), "target_hit_rate": None, "mae_pp": None}
    if samples:
        hits = sum(1 for s in samples if s.get("target_hit"))
        errors = [abs(s["prediction_error_pp"]) for s in samples
                  if s.get("prediction_error_pp") is not None]
        summary["target_hit_rate"] = round(hits / len(samples), 3)
        if errors:
            summary["mae_pp"] = round(sum(errors) / len(errors), 2)
    return {"device_sn": device_sn, "days": days,
            "summary": summary, "samples": samples}


@app.get("/api/smart_charge/backtest")
def api_smart_charge_backtest(
    device_sn: str | None = None,
    days: int = 7,
    target_override: float | None = None,
    limit: int = 1000,
):
    """Replay recorded smart-charge decisions through the *current*
    compute_plan, so behavior changes can be validated without waiting
    for fresh ticks to accumulate. Returns per-decision diff + a
    summary. `target_override` lets you stress-test discontinuous
    schedules by pushing target above the natural sunrise trough."""
    import backtest as bt
    if not device_sn and state.device:
        device_sn = state.device.device_sn
    if not device_sn:
        raise HTTPException(400, "device_sn required (no active device)")

    days = max(1, min(int(days), 30))
    since_ts = int(time.time()) - days * 86400
    decisions = state.energy.list_smart_charge_decisions(
        device_sn, limit=limit, since_ts=since_ts
    )
    if not decisions:
        return {
            "device_sn": device_sn, "days": days,
            "target_override": target_override,
            "summary": {"n": 0}, "results": [],
        }

    # History needs ~14d lookback for solar-coefficient + idle-overhead
    # fits at the OLDEST replay point, plus the days we're replaying.
    history = state.energy.history(
        device_sn, hours=(days + 14) * 24, bucket_s=3600,
    )
    weather_obs = state.energy.list_weather_observations(
        since_ts=since_ts - 14 * 86400, limit=24 * (days + 14) + 72,
    )

    cfg = smart_charge.get_config(device_sn)
    cfg_max_charge = float(cfg.get("max_charge_w") or 800)

    # Capacity + tz mirror what _smart_charge_evaluate would resolve.
    active_sn = state.device.device_sn if state.device else None
    if device_sn == active_sn:
        model_code = getattr(state.device, "model_code", None)
    else:
        cloud = state.last_cloud_meta or {}
        devs = (cloud.get("devices") or []) if isinstance(cloud, dict) else []
        meta = next((d for d in devs if str(d.get("device_sn")) == device_sn), {})
        model_code = meta.get("model_code")
    capacity = _total_capacity_wh(device_sn, model_code)
    loc = device_location.get() or {}
    tz_offset = int(loc.get("utc_offset_seconds") or 0)

    results = bt.replay_decisions(
        decisions=decisions,
        full_energy_history=history,
        weather_observations=weather_obs,
        capacity_wh=capacity,
        max_charge_w=cfg_max_charge,
        cost_plan=cost_module.get_plan(),
        tz_offset_seconds=tz_offset,
        target_override=target_override,
    )
    return {
        "device_sn": device_sn,
        "days": days,
        "target_override": target_override,
        "capacity_wh": capacity,
        "max_charge_w": cfg_max_charge,
        "tz_offset_seconds": tz_offset,
        "summary": bt.summarize(results),
        "results": results,
    }


@app.post("/api/smart_charge/evaluate_now")
async def api_smart_charge_evaluate_now(device_sn: str | None = None):
    """Compute a decision RIGHT NOW (no execution, no history write).
    Used by the UI's "Evaluate now" button to show what the controller
    would currently decide. Also returns narration when both
    claude_enabled is on AND a key is configured — lets the user verify
    the full pipeline without waiting for the next 5-min tick."""
    plan = await _smart_charge_evaluate(record=False, device_sn=device_sn)
    if not plan:
        return {"plan": None, "narration": None}
    narration = ""
    cfg_sn = device_sn or (state.device.device_sn if state.device else None)
    if cfg_sn:
        cfg = smart_charge.get_config(cfg_sn)
        if cfg.get("claude_enabled"):
            try:
                import claude_narrator
                if claude_narrator.has_usable_key():
                    narration = await claude_narrator.narrate_smart_charge(plan)
            except Exception as e:
                log.debug("evaluate_now narration failed: %s", e)
    return {"plan": plan.to_dict(), "narration": narration or None}


# ---- Algorithm advisor (Claude Opus + extended thinking) ----
@app.get("/api/algorithm/suggestions")
def api_alg_suggestions(device_sn: str | None = None,
                        status: str | None = "pending"):
    """List algorithm suggestions. Defaults to status=pending so the UI
    shows what's awaiting the user's decision; pass status='applied' /
    'dismissed' / null (all) to see history."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    return {
        "device_sn": device_sn,
        "status_filter": status,
        "suggestions": state.energy.list_suggestions(
            device_sn=device_sn, status=status, limit=100,
        ),
    }


async def _advisor_review_job(device_sn: str) -> None:
    """Background task body. Updates state.advisor_jobs[device_sn] in place
    so the polling endpoint can report progress + final result without
    holding the HTTP request open through the entire 60-180s review."""
    job = state.advisor_jobs.setdefault(device_sn, {})
    job.update({
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    })
    try:
        result = await _run_advisor_review(device_sn)
        job["finished_at"] = time.time()
        if result.get("ok"):
            job["status"] = "done"
            job["result"] = result
        else:
            job["status"] = "error"
            job["error"] = str(result.get("reason") or "unknown")
    except Exception as e:
        log.exception("advisor: background job failed for %s", device_sn)
        job["finished_at"] = time.time()
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


@app.post("/api/algorithm/review_now", status_code=202)
async def api_alg_review_now(device_sn: str | None = None):
    """Kick off a Claude review in the background and return immediately.

    Reviews routinely run 60-180s with adaptive thinking + multi-turn
    tool calls, which exceeds Cloudflare's 100s edge timeout (HTTP 524).
    So we spawn the review as a background asyncio task and let the UI
    poll /api/algorithm/review_status until done. Re-clicking while one
    is in flight is a no-op (returns the existing job)."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(400, "no active device")
    existing = state.advisor_jobs.get(device_sn)
    if existing and existing.get("status") == "running":
        return {"status": "running", "device_sn": device_sn,
                "started_at": existing.get("started_at"),
                "already_running": True}
    asyncio.create_task(_advisor_review_job(device_sn))
    return {"status": "running", "device_sn": device_sn,
            "started_at": time.time(), "already_running": False}


@app.get("/api/algorithm/review_status")
async def api_alg_review_status(device_sn: str | None = None):
    """Poll for the latest review job's state for one device."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(400, "no active device")
    job = state.advisor_jobs.get(device_sn)
    if not job:
        return {"status": "idle", "device_sn": device_sn}
    out = {"device_sn": device_sn, **job}
    if job.get("status") == "running":
        out["elapsed_s"] = round(time.time() - (job.get("started_at") or time.time()), 1)
    return out


@app.post("/api/algorithm/suggestions/{suggestion_id}/apply")
async def api_alg_suggestion_apply(suggestion_id: int):
    """Apply a single pending suggestion. Re-validates against the
    advisor's whitelist + safety floors at apply time so a config tweak
    that was valid at suggestion time but isn't now (e.g. user lowered
    capacity_wh override) gets rejected. Writes an audit row."""
    import claude_advisor
    s = state.energy.get_suggestion(suggestion_id)
    if not s:
        raise HTTPException(404, "suggestion not found")
    if s["status"] != "pending":
        raise HTTPException(400, f"suggestion is {s['status']}, not pending")
    if s["kind"] != "config":
        raise HTTPException(400, "anomalies are not directly applicable; use dismiss/acknowledge")

    target = s["target"]
    rules = claude_advisor.ALLOWED_TARGETS.get(target)
    if not rules:
        raise HTTPException(400, f"target {target!r} no longer in whitelist")
    proposed = s["proposed_value"]
    try:
        proposed_n = float(proposed)
    except Exception:
        raise HTTPException(400, "proposed_value not numeric") from None
    if proposed_n < rules["min"] or proposed_n > rules["max"]:
        raise HTTPException(400, f"proposed value out of safe range [{rules['min']}, {rules['max']}]")

    # Per-device smart-charge config tweaks are the only kind we
    # currently apply. Forecaster-global params would need a
    # runtime-config layer that we haven't built yet; advisor can
    # surface them as anomalies, but we won't auto-apply them here.
    if not target.startswith("smart_charge."):
        raise HTTPException(400, f"applying {target!r} is not yet supported")
    if rules.get("scope") == "device" and not s.get("device_sn"):
        raise HTTPException(400, "device-scoped suggestion missing device_sn")

    field = target.split(".", 1)[1]
    cfg = smart_charge.get_config(s["device_sn"])
    old = cfg.get(field)
    cfg[field] = int(proposed_n) if isinstance(old, int) else proposed_n
    smart_charge.set_config(cfg, device_sn=s["device_sn"])

    # Persist the audit row + flip suggestion to applied.
    state.energy.record_change(
        suggestion_id=suggestion_id, device_sn=s["device_sn"],
        target=target, old_value=old, new_value=cfg[field],
        reasoning=s.get("reasoning"),
    )
    state.energy.update_suggestion_status(suggestion_id, "applied")
    return {"ok": True, "applied": {target: cfg[field]}, "previous": old}


@app.post("/api/algorithm/suggestions/{suggestion_id}/dismiss")
def api_alg_suggestion_dismiss(suggestion_id: int):
    s = state.energy.get_suggestion(suggestion_id)
    if not s:
        raise HTTPException(404, "suggestion not found")
    if s["status"] != "pending":
        return {"ok": True, "already": s["status"]}
    state.energy.update_suggestion_status(suggestion_id, "dismissed")
    return {"ok": True}


@app.get("/api/algorithm/preview")
async def api_alg_preview(device_sn: str | None = None):
    """Return the exact starter bundle the advisor sends to Claude as
    its opening user message — minus the system prompt and the tool
    schema. Claude follows up with DB query tools, but this is the
    initial context. Used by the UI's "Show what Claude sees" button
    so the user can verify the data flow without burning an API call."""
    import claude_advisor
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        raise HTTPException(400, "no active device")
    bundle = await _build_advisor_bundle(device_sn)
    # Resolve the model at call time — same precedence the actual review
    # uses (env var > anthropic_prefs > DEFAULT_MODEL). Don't reach for
    # a module-level constant; there isn't one any more.
    return {
        "device_sn": device_sn,
        "rendered": claude_advisor._format_starter_bundle(bundle),
        "model": claude_advisor._get_model(),
        "thinking_budget": claude_advisor.THINKING_BUDGET,
        "raw_bundle": bundle,
    }


@app.get("/api/algorithm/changes")
def api_alg_changes(device_sn: str | None = None):
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    return {
        "device_sn": device_sn,
        "changes": state.energy.list_changes(device_sn, limit=50),
    }


@app.get("/api/devices/capacity")
def api_devices_capacity():
    """List every recorded device with its current capacity (default vs
    user override). Used by the Device tab to render the capacity editor."""
    out = []
    for d in state.energy.list_devices():
        default_wh = forecaster.battery_capacity_wh(d.get("model_code"))
        override = d.get("capacity_wh_override")
        # Auto-derived from this device's own pack cache.
        sn = d["device_sn"]
        pack_count = len(state.battery_packs_by_sn.get(sn, []))
        auto_wh: int | None = None
        if pack_count:
            pack_wh = forecaster.expansion_pack_capacity_wh(d.get("model_code"))
            auto_wh = default_wh + pack_count * pack_wh
        effective = override or auto_wh or default_wh
        out.append({
            "device_sn": sn,
            "name": d.get("name"),
            "model_code": d.get("model_code"),
            "default_capacity_wh": default_wh,
            "capacity_wh_override": override,
            "auto_capacity_wh": auto_wh,
            "pack_count": pack_count,
            "effective_capacity_wh": effective,
        })
    return {"devices": out}


@app.post("/api/devices/capacity")
async def api_devices_capacity_set(req: Request):
    """Set or clear the manual total-capacity override for a device. Pass
    `capacity_wh: null` (or omit) to clear; the forecast then falls back
    to the model-default capacity."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    device_sn = (body or {}).get("device_sn")
    if not device_sn:
        raise HTTPException(status_code=400, detail="device_sn required")
    raw = (body or {}).get("capacity_wh")
    capacity_wh = None if raw in (None, "", 0) else raw
    if not state.energy.set_capacity_override(device_sn, capacity_wh):
        raise HTTPException(status_code=400, detail="capacity_wh out of range (500..200000) or device unknown")
    return {"ok": True, "device_sn": device_sn,
            "capacity_wh_override": state.energy.get_capacity_override(device_sn)}


@app.get("/api/debug/cloud_probe")
async def api_debug_cloud_probe():
    """Speculative probe of multiple cloud API endpoints to find data we
    don't currently parse — per-battery state, expansion-pack metadata,
    etc. Returns raw responses; the user can scan for useful keys."""
    rpc = getattr(state.client, "_rpc", None)
    if rpc is None:
        return {"error": "bridge not available", "results": {}}
    try:
        result = await rpc("cloud_probe")
    except Exception as e:
        return {"error": str(e), "results": {}}
    return result


@app.get("/api/debug/raw_props")
async def api_debug_raw_props(device_sn: str | None = None):
    """Diagnostic dump of the raw cloud properties dict for a device.
    Used to identify property keys we don't currently parse — extension-
    battery state, per-PV-port solar, etc. Goes through the bridge RPC."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"error": "no device", "props": {}}
    rpc = getattr(state.client, "_rpc", None)
    if rpc is None:
        return {"error": "bridge not available", "props": {}}
    try:
        result = await rpc("get_raw_props", device_sn=device_sn)
    except Exception as e:
        return {"error": str(e), "props": {}}
    return {"device_sn": device_sn, "props": (result or {}).get("props", {})}


@app.get("/api/devices/battery_packs")
async def api_devices_battery_packs(device_sn: str | None = None,
                                    fresh: bool = False):
    """Per-expansion-battery state. Defaults to the poll-loop cache (refreshed
    every BATTERY_PACK_REFRESH_S). Pass fresh=true to force a live RPC fetch
    — useful for a manual refresh button in the UI."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    active_sn = state.device.device_sn if state.device else None
    cached_packs = state.battery_packs_by_sn.get(device_sn or "", [])
    last_ts = state.last_packs_ts_by_sn.get(device_sn or "", 0.0)
    diag = {
        "active_sn": active_sn,
        "device_sn": device_sn,
        "cached_count": len(cached_packs),
        "last_packs_ts": last_ts,
        "backend": state.backend,
    }
    if not device_sn:
        return {"error": "no device", "packs": [], "_diag": diag}
    # Only return live main_soc_pct for the *active* device — that's the
    # one whose battery_percent is in state.last_status.
    main_pct = (state.last_status or {}).get("battery_percent") if device_sn == active_sn else None
    if not fresh and cached_packs:
        return {"device_sn": device_sn,
                "packs": cached_packs,
                "main_soc_pct": main_pct,
                "fetched_at": last_ts,
                "cached": True,
                "_diag": diag}
    # No cache for this device. If we've already learned (via a prior
    # successful fetch with empty packs) that this device has no packs,
    # short-circuit so the UI hides the card without another RPC.
    if not fresh and device_sn in state.battery_packs_by_sn and not cached_packs:
        return {"device_sn": device_sn,
                "packs": [],
                "main_soc_pct": main_pct,
                "fetched_at": last_ts,
                "cached": True,
                "no_packs": True,
                "_diag": diag}
    rpc = getattr(state.client, "_rpc", None)
    if rpc is None:
        return {"error": "bridge not available (backend=" + state.backend + ")",
                "packs": [], "_diag": diag}
    try:
        result = await rpc("get_battery_packs", device_sn=device_sn)
    except Exception as e:
        log.warning("battery_packs API RPC failed: %s", e)
        return {"error": f"rpc failed: {e}", "packs": [], "_diag": diag}
    packs = (result or {}).get("packs", [])
    rpc_err = (result or {}).get("error")
    if rpc_err:
        log.warning("battery_packs API RPC returned error: %s", rpc_err)
    # Cache the result regardless of which device — switching back later
    # should hit the cache instantly. Only record empty results explicitly
    # (no_packs sentinel) so the cache doesn't claim a device has no packs
    # just because the RPC failed.
    if packs:
        state.battery_packs_by_sn[device_sn] = packs
        state.last_packs_ts_by_sn[device_sn] = time.time()
        try:
            state.energy.record_battery_packs(device_sn, packs)
        except Exception as e:
            log.debug("record_battery_packs failed: %s", e)
    elif not rpc_err:
        # Successful RPC with empty packs = device has no expansion packs.
        state.battery_packs_by_sn[device_sn] = []
        state.last_packs_ts_by_sn[device_sn] = time.time()
    return {"device_sn": device_sn,
            "packs": packs,
            "main_soc_pct": main_pct,
            "fetched_at": time.time(),
            "cached": False,
            "error": rpc_err,
            "_diag": diag}


@app.get("/api/location")
async def api_location_get():
    """Return the stored device location, if any.

    Lazily backfills the human-readable `label` on records that were
    saved via geolocation or raw coords (where the user never typed a
    city name). One reverse-geocode call per location update — the
    label gets persisted, so subsequent GETs return immediately."""
    loc = device_location.get()
    if not loc:
        return {"latitude": None, "longitude": None}
    if not loc.get("label") and loc.get("latitude") is not None:
        try:
            label = await weather_client.reverse_geocode(
                loc["latitude"], loc["longitude"],
            )
            if label and device_location.set_label(label):
                loc["label"] = label
        except Exception as e:
            log.debug("location label backfill failed: %s", e)
    return loc


@app.get("/api/location/geocode")
async def api_location_geocode(q: str = "", count: int = 5):
    """Free-text city/place lookup for the manual-location override UI.
    Proxies Open-Meteo's free geocoding API (no key required) so the
    browser doesn't have to know the upstream URL or hold a CORS
    relationship with it.

    Returns {results: [{name, admin1, country, latitude, longitude,
    timezone}, ...]}. Empty list on miss or transient network failure
    — callers should treat "no results" as a search miss, not a hard
    error.
    """
    return await weather_client.geocode(q, count=count)


@app.post("/api/location")
async def api_location_set(req: Request):
    """Persist the device's latitude + longitude. Called by the browser
       after the user grants the geolocation prompt on the Forecast tab."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    record = device_location.set(
        body.get("latitude"),
        body.get("longitude"),
        label=body.get("label"),
    )
    if record is None:
        raise HTTPException(status_code=400,
                            detail="latitude/longitude out of range")
    # Bust the weather cache so the next forecast pulls for the new coords.
    weather_client.clear_cache()
    return record


@app.get("/api/auth/status")
async def api_auth_status():
    """Tell the UI whether the bridge has cloud credentials, and the cloud state.
       This drives the login screen."""
    auth = getattr(state.client, "auth_status", None)
    if not auth:
        # backends like 'mock' or 'native' don't talk to a bridge
        return {
            "has_credentials": True,        # not applicable -> don't show login
            "cloud_state": "n/a",
            "backend": state.backend,
        }
    try:
        info = await auth()
    except Exception as e:
        # bridge unreachable -> show login as a safe default? No — surface error,
        # let UI keep retrying. Return has_credentials=true so we don't trap user
        # behind a login modal during a transient bridge outage.
        return {
            "has_credentials": True,
            "cloud_state": "bridge-unreachable",
            "error": str(e),
            "backend": state.backend,
        }
    info["backend"] = state.backend
    return info


@app.post("/api/auth/credentials")
async def api_set_credentials(body: dict):
    """Validate + persist Jackery cloud credentials. Bridge writes them to keychain
       and restarts the cloud poller."""
    email = (body or {}).get("email", "").strip()
    password = (body or {}).get("password", "")
    region = ((body or {}).get("region") or "US").strip().upper() or "US"
    if not email or not password:
        raise HTTPException(400, "email and password are required")
    setter = getattr(state.client, "set_credentials", None)
    if not setter:
        raise HTTPException(501, "This backend does not support setting credentials")
    try:
        result = await setter(email, password, region)
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    # Kick a fresh connect so connection state updates fast
    asyncio.create_task(connect_device())
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@app.post("/api/auth/forget")
async def api_clear_credentials():
    """Wipe stored Jackery cloud credentials. Bridge stops the cloud poller and
       returns to needs-credentials state. UI will show the sign-in screen again."""
    clearer = getattr(state.client, "clear_credentials", None)
    if not clearer:
        raise HTTPException(501, "This backend does not support clearing credentials")
    try:
        result = await clearer()
    except DeviceClientError as e:
        # most common: env vars are pinning the creds
        raise HTTPException(400, str(e)) from e
    # Clear cached telemetry so the UI immediately reflects logged-out state
    state.device = None
    state.last_status = None
    state.last_update_ts = None
    await broadcast_status("status")
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@app.get("/api/automation/rules")
def api_automation_list():
    return {"rules": state.automation.list_rules()}


@app.post("/api/automation/rules")
def api_automation_upsert(body: dict):
    try:
        rule = state.automation.upsert(body or {})
    except AutomationError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "rule": rule}


@app.delete("/api/automation/rules/{rule_id}")
def api_automation_delete(rule_id: str):
    deleted = state.automation.delete(rule_id)
    return {"ok": True, "deleted": deleted}


@app.get("/api/automation/rules/{rule_id}/history")
def api_automation_rule_history(rule_id: str, days: int = 30):
    """Persistent firing log for one rule over the last N days, plus
    paired ON/OFF intervals for the rule's target Kasa plug.

    Returns:
      - `firings`: every successful edge-triggered fire, newest first.
      - `intervals`: ON-time intervals built by walking ALL firings on
        the same kasa_host (so an ON from rule-X paired with an OFF
        from rule-Y still counts as one interval — what the user
        actually experienced on the plug).
      - `total_on_seconds`: sum of interval durations in the window.
    """
    days = max(1, min(int(days), 365))
    firings = state.energy.list_automation_firings(
        rule_id=rule_id, days=days, limit=2000,
    )
    # Find the rule's current Kasa host so we can pair intervals across
    # complementary rules. If the rule has been deleted, fall back to
    # the host recorded on the most recent firing.
    rule = next((r for r in state.automation.list_rules()
                 if r.get("id") == rule_id), None)
    kasa_host = (rule or {}).get("kasa_host")
    if not kasa_host and firings:
        kasa_host = firings[0]["kasa_host"]
    intervals = (state.energy.automation_on_intervals(kasa_host, days=days)
                 if kasa_host else [])
    total_on = sum(i["duration_s"] for i in intervals)
    return {
        "rule_id": rule_id,
        "rule_exists": rule is not None,
        "kasa_host": kasa_host,
        "days": days,
        "firings": firings,
        "intervals": intervals,
        "total_on_seconds": total_on,
    }


@app.get("/api/kasa/credentials")
def api_kasa_creds_status():
    """Tell the UI whether Kasa cloud credentials are saved (without
       returning the password). Used to decide whether to show the
       "credentials needed" banner on newer-device test failures."""
    creds = kasa_creds.load()
    return {
        "has_credentials": creds is not None,
        "email": creds.get("email") if creds else None,
    }


@app.post("/api/kasa/credentials")
def api_kasa_creds_save(body: dict):
    """Persist Kasa cloud-account email + password (encrypted at rest).
       Required for newer Kasa SMART devices (KP125M, EP25, KP405, etc.)."""
    email = ((body or {}).get("email") or "").strip()
    password = ((body or {}).get("password") or "")
    if not email or not password:
        raise HTTPException(400, "email and password are required")
    if not kasa_creds.save(email, password):
        raise HTTPException(500, "failed to save credentials")
    return {"ok": True, "email": email}


@app.delete("/api/kasa/credentials")
def api_kasa_creds_clear():
    kasa_creds.clear()
    return {"ok": True}


@app.get("/api/anthropic/key")
def api_anthropic_key_status():
    """Tell the UI whether an Anthropic API key is saved (without
    returning the key itself). Drives the Settings page status badge
    and gates the smart-charge `claude_enabled` toggle."""
    import anthropic_creds as ac
    return {
        "has_key": ac.has_key() or bool(os.environ.get("ANTHROPIC_API_KEY")),
        "source": "env" if (not ac.has_key() and os.environ.get("ANTHROPIC_API_KEY"))
                  else ("saved" if ac.has_key() else None),
    }


@app.post("/api/anthropic/key")
async def api_anthropic_key_save(body: dict):
    """Validate the candidate key by making a 1-token API call, then
    persist it. We refuse to save a key that doesn't actually work —
    user-side guarantee that flipping `claude_enabled` will produce
    narration instead of silent empty strings."""
    import anthropic_creds as ac
    import claude_narrator
    api_key = ((body or {}).get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key required")
    ok, msg = await claude_narrator.validate_key(api_key)
    if not ok:
        raise HTTPException(400, f"key validation failed: {msg}")
    if not ac.save(api_key):
        raise HTTPException(500, "failed to save key")
    return {"ok": True}


@app.delete("/api/anthropic/key")
def api_anthropic_key_clear():
    """Forget the saved key. The env-var fallback (if set) keeps working."""
    import anthropic_creds as ac
    ac.clear()
    return {"ok": True}


# Server-side model-list cache. Anthropic's /v1/models is cheap, but we
# don't want to hammer it on every Settings tab open + we need a
# graceful fallback when the API key is missing or the network is down.
_anthropic_models_cache: dict[str, Any] = {"ts": 0.0, "models": []}
ANTHROPIC_MODELS_CACHE_TTL_S = 5 * 60

# Static fallback list — what the UI offers when no key is configured
# or the live fetch fails. Order = recommended first. IDs match the
# aliases users typically see in Anthropic's docs.
ANTHROPIC_MODELS_FALLBACK: list[dict[str, str]] = [
    {"id": "claude-opus-4-7", "display_name": "Claude Opus 4.7"},
    {"id": "claude-sonnet-4-7", "display_name": "Claude Sonnet 4.7"},
    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
]

# Substring patterns for model IDs that support the 1M-context beta.
# Applied case-insensitively. Override via env var when Anthropic
# extends 1M to a new family — no code change needed.
#   JACKERY_1M_MODEL_PATTERNS="opus,sonnet"  → flags both families
# As of this writing, only the Opus 4.x line ships with 1M support.
ANTHROPIC_1M_PATTERNS: tuple[str, ...] = tuple(
    p.strip().lower()
    for p in os.environ.get("JACKERY_1M_MODEL_PATTERNS", "opus").split(",")
    if p.strip()
)


def _model_supports_1m(model_id: str) -> bool:
    """Heuristic: does this model id match any configured 1M-capable
    pattern? Used by the UI to decide whether to synthesize a "(1M
    context)" entry alongside the bare model entry in the dropdown."""
    if not model_id:
        return False
    needle = model_id.lower()
    return any(p in needle for p in ANTHROPIC_1M_PATTERNS)


def _annotate_models(models: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Tag each model with `supports_1m` so the UI can decide which
    ones to synthesize a `(1M context)` dropdown variant for. Pure;
    just adds a flag, doesn't filter."""
    return [
        {**m, "supports_1m": _model_supports_1m(m.get("id", ""))}
        for m in models
    ]


@app.get("/api/anthropic/models")
async def api_anthropic_models(refresh: bool = False):
    """Return the list of Claude models the user can pick from, each
    annotated with `supports_1m` (matched against
    JACKERY_1M_MODEL_PATTERNS, default `opus`).

    When an Anthropic API key is configured, fetches the live list
    from the Anthropic API (cached 5 min). When no key is configured
    or the fetch fails, falls back to a static list of well-known
    aliases. Always succeeds — the UI dropdown will always have
    options to render.

    `refresh=true` busts the cache."""
    import anthropic_creds as ac
    now = time.time()
    if not refresh and (now - _anthropic_models_cache["ts"]) < ANTHROPIC_MODELS_CACHE_TTL_S:
        cached = _anthropic_models_cache["models"]
        if cached:
            return {"models": _annotate_models(cached), "source": "cache"}

    api_key = ac.load() or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"models": _annotate_models(ANTHROPIC_MODELS_FALLBACK),
                "source": "fallback_no_key"}

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"models": _annotate_models(ANTHROPIC_MODELS_FALLBACK),
                "source": "fallback_no_sdk"}

    try:
        client = AsyncAnthropic(api_key=api_key)
        # SDK exposes models.list() but the page is small enough that
        # one call is fine.
        page = await client.models.list(limit=50)
        models = [
            {"id": m.id, "display_name": getattr(m, "display_name", m.id)}
            for m in (page.data or [])
        ]
    except Exception as e:
        log.info("anthropic models list failed (%s); using fallback", e)
        return {"models": _annotate_models(ANTHROPIC_MODELS_FALLBACK),
                "source": "fallback_fetch_failed",
                "error": str(e)[:200]}

    if not models:
        return {"models": _annotate_models(ANTHROPIC_MODELS_FALLBACK),
                "source": "fallback_empty"}
    _anthropic_models_cache["ts"] = now
    _anthropic_models_cache["models"] = models
    return {"models": _annotate_models(models), "source": "live"}


@app.get("/api/anthropic/prefs")
def api_anthropic_prefs_get():
    """Current model preference per role (advisor, narrator). Defaults
    fill in missing keys so the UI always renders something."""
    import anthropic_prefs
    return anthropic_prefs.get_all()


@app.post("/api/anthropic/prefs")
async def api_anthropic_prefs_save(body: dict):
    """Persist any subset of the user's preferences. Missing keys
    leave the existing preference untouched.

    Accepted fields:
      - advisor_model:           Claude model id for the advisor role
      - advisor_1m_context:      bool — send context-1m beta header
      - advisor_thinking_effort: "low" | "medium" | "high"
      - narrator_model:          Claude model id for the narrator role

    Returns the resulting full snapshot so the UI can re-render."""
    import anthropic_prefs
    body = body or {}
    advisor_model = body.get("advisor_model")
    narrator_model = body.get("narrator_model")
    raw_1m = body.get("advisor_1m_context")
    advisor_1m = bool(raw_1m) if raw_1m is not None else None
    advisor_effort = body.get("advisor_thinking_effort")
    if (advisor_model is None and narrator_model is None
            and advisor_1m is None and advisor_effort is None):
        raise HTTPException(400, "at least one preference field required")
    return anthropic_prefs.set_models(
        advisor_model=advisor_model,
        advisor_1m_context=advisor_1m,
        advisor_thinking_effort=advisor_effort,
        narrator_model=narrator_model,
    )


@app.get("/api/kasa/devices")
async def api_kasa_devices():
    """Discover Kasa devices on the LAN. May return [] if Docker bridge
       networking blocks UDP broadcasts — caller should still allow manual
       IP entry as a fallback."""
    try:
        return {"devices": await kasa_client.discover()}
    except kasa_client.KasaError as e:
        raise HTTPException(500, str(e)) from e


@app.post("/api/kasa/test")
async def api_kasa_test(body: dict):
    """Toggle a specific Kasa device by IP — used on the rule-editor "Test"
       button so the user can confirm the IP works before saving the rule."""
    host = (body or {}).get("host")
    on = bool((body or {}).get("on"))
    if not host:
        raise HTTPException(400, "host required")
    try:
        result = await kasa_client.set_state(str(host), on)
    except kasa_client.KasaError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **result}


@app.get("/api/kasa/status")
async def api_kasa_status(host: str):
    """Read the current on/off state of a single Kasa device."""
    try:
        return {"ok": True, **(await kasa_client.status(host))}
    except kasa_client.KasaError as e:
        raise HTTPException(400, str(e)) from e


# ---- saved Kasa device registry (separate from rules) ----
def _enrich_kasa_for_ui(d: dict) -> dict:
    """Surface the registry's cached probe state on the wire shape the
    UI expects: `online`/`is_on`/`error`/`status` derived from the
    last reconciler outcome."""
    return {
        **d,
        "is_on": d.get("last_known_is_on"),
        "online": state.kasa.is_online(d),
        "status": state.kasa.status_of(d),
        "error": d.get("last_error"),
        "last_seen_ts": d.get("last_seen_ts"),
    }


@app.get("/api/kasa/saved")
async def api_kasa_saved_list(refresh: bool = False,
                              jackery_sn: str | None = None):
    """Return saved Kasa devices. If `jackery_sn` is provided, restrict
    to plugs assigned to that Jackery (or unassigned legacy entries).

    By default returns CACHED probe state from the kasa_reconciler_loop
    background task (every ~5 min, exponential backoff per device on
    failure). If `refresh=true`, force an immediate parallel probe and
    update the cache — useful for the user's "I just plugged it back
    in, refresh now" path. Both routes update the persisted
    consecutive_failures counter."""
    devices = state.kasa.list_devices(jackery_device_sn=jackery_sn)
    if not refresh or not devices:
        return {"devices": [_enrich_kasa_for_ui(d) for d in devices]}

    async def _probe(d):
        try:
            info = await kasa_client.status(d["host"])
            await _kasa_update_probe_and_notify(
                d["host"], success=True,
                is_on=info.get("is_on"),
                model=info.get("model"),
                alias=info.get("alias"),
            )
        except Exception as e:
            await _kasa_update_probe_and_notify(
                d["host"], success=False, error=str(e),
            )
        return _enrich_kasa_for_ui(state.kasa.get(d["host"]) or d)

    enriched = await asyncio.gather(*[_probe(d) for d in devices])
    return {"devices": list(enriched)}


@app.get("/api/kasa/health")
def api_kasa_health():
    """Lightweight summary the dashboard polls to decide whether to
    show the Automation tab dot. No probing — reads cached state."""
    return {
        "offline_count": state.kasa.offline_count(),
        "device_count": len(state.kasa.list_devices()),
    }


@app.post("/api/kasa/saved")
async def api_kasa_saved_upsert(body: dict):
    """Add or update a saved Kasa device. Probes the device first so the
    saved record always has accurate model/alias and `last_tested`
    reflects a real successful contact. Pass `jackery_device_sn` to
    assign the plug to a specific Jackery (smart-charge / per-device
    rule pickers filter by this); empty string explicitly unassigns;
    omitting the field leaves the existing assignment alone."""
    host = ((body or {}).get("host") or "").strip()
    requested_alias = ((body or {}).get("alias") or "").strip()
    if not host:
        raise HTTPException(400, "host required")
    try:
        info = await kasa_client.status(host)
    except kasa_client.KasaError as e:
        raise HTTPException(400, str(e)) from e
    # Distinguish "not provided" (None) from "explicit unassign" ("").
    j_sn = body.get("jackery_device_sn", None) if isinstance(body, dict) else None
    saved = state.kasa.upsert(
        host=host,
        alias=requested_alias or info.get("alias") or "",
        model=info.get("model"),
        type_=info.get("type"),
        mark_tested=True,
        jackery_device_sn=j_sn,
    )
    return {"ok": True, "device": {**saved, "is_on": info.get("is_on")}}


@app.delete("/api/kasa/saved/{host:path}")
async def api_kasa_saved_delete(host: str):
    """Delete a saved device. Doesn't touch any rule that references it —
       those rules will start failing on next evaluation, visible in Logs."""
    deleted = state.kasa.delete(host)
    in_use = [r for r in state.automation.list_rules() if r.get("kasa_host") == host]
    return {"ok": True, "deleted": deleted,
            "rules_referencing": [r["id"] for r in in_use]}


@app.get("/api/events")
async def api_events(limit: int = 100, since: float = 0.0):
    """Return the bridge's recent event log (auth, poll, mqtt, session, etc.)
       for the dashboard's Logs tab. `since` is unix-seconds; older events
       are filtered out so the UI can do incremental polling cheaply."""
    fetcher = getattr(state.client, "get_events", None)
    if not fetcher:
        # Mock backend has no event log — return empty so the tab still loads.
        return {"events": []}
    try:
        events = await fetcher(limit=limit, since=since)
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    return {"events": events}


@app.get("/api/settings")
def api_settings_get():
    """Return the schema (label/hint/min/max/value for each setting) so the
       UI can render the form without hardcoding it."""
    return {"settings": user_settings.schema()}


@app.post("/api/settings")
def api_settings_post(body: dict):
    """Persist setting overrides to /data/settings.json. Out-of-range values
       are clamped to the schema bounds. Changes take effect on next poll
       cycle of the server / bridge — no restart needed."""
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    new_values = user_settings.update(body)
    return {"ok": True, "settings": new_values}


@app.post("/api/set_output")
async def api_set_output(body: dict):
    """Toggle one of the device's outputs (AC/DC/USB/Car) via the cloud MQTT
       channel. Body: {port: 'ac'|'dc'|'usb'|'car', on: bool,
                       device_sn: optional}.

    `device_sn` lets the per-browser view route the toggle to the device
    the user is actually looking at — without it, the bridge sends the
    command to whatever device it happens to be polling, which silently
    targets the wrong Jackery for any browser whose view doesn't match
    the bridge-active device. Defaults to the bridge-active SN when
    omitted (back-compat for older clients)."""
    port = (body or {}).get("port")
    on = bool((body or {}).get("on"))
    device_sn = (body or {}).get("device_sn") or None
    if port not in ("ac", "dc", "usb", "car"):
        raise HTTPException(400, "port must be one of: ac, dc, usb, car")
    setter = getattr(state.client, "set_output", None)
    if not setter:
        raise HTTPException(501, "Backend does not support output toggles")
    try:
        await setter(port, on, device_sn=device_sn)
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "port": port, "on": on, "device_sn": device_sn}


@app.post("/api/pause_polling")
async def api_pause_polling(body: dict | None = None):
    """Pause the cloud poller so the user can use the phone app without the
       bridge stealing the session back. Body: {seconds: int} (default 600)."""
    seconds = int((body or {}).get("seconds") or 600)
    pauser = getattr(state.client, "pause_polling", None)
    if not pauser:
        raise HTTPException(501, "Backend does not support pause_polling")
    try:
        result = await pauser(seconds)
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    await broadcast_status("status")
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@app.post("/api/resume_polling")
async def api_resume_polling():
    """Cancel any active pause / contested cooldown and reclaim the cloud session."""
    resumer = getattr(state.client, "resume_polling", None)
    if not resumer:
        raise HTTPException(501, "Backend does not support resume_polling")
    try:
        result = await resumer()
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    await broadcast_status("status")
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@app.post("/api/select_device")
async def api_select_device(body: dict):
    device_id = (body or {}).get("device_id")
    if not device_id:
        raise HTTPException(400, "device_id required")
    select = getattr(state.client, "select_device", None)
    if not select:
        raise HTTPException(501, "Backend does not support device switching")
    try:
        result = await select(str(device_id))
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    # Clear cached device + telemetry IMMEDIATELY so the UI stops showing the
    # old device while the next poll is in flight. The Device tab will go to
    # "—" for ~1-2s, then refill with the new device's name/SN.
    state.device = None
    state.last_status = None
    state.last_update_ts = None
    state.reset_live_history()
    await broadcast_status("status")

    asyncio.create_task(force_poll())
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


@app.post("/api/view/select_device")
async def api_view_select_device(body: dict, request: Request, response: Response):
    """Per-browser device-view picker. Sets a `view_device_id` cookie so
    this browser's UI renders that Jackery's data, without changing what
    the bridge polls (every device is polled on every tick) or which
    device the automation worker manages. Two browsers can therefore
    look at different Jackerys at the same time without stomping each
    other.

    Validates against the current account's known devices so a typo
    can't poison the cookie. Also bumps the connected WS (if any) so
    the same browser's open WebSocket switches view immediately
    instead of waiting until reconnect."""
    device_id = (body or {}).get("device_id")
    if not device_id:
        raise HTTPException(400, "device_id required")
    cloud = state.last_cloud_meta or {}
    devs = cloud.get("devices") or []
    if not any(str(d.get("device_id")) == str(device_id) for d in devs):
        raise HTTPException(404, "device not found in current account")
    api_auth.set_app_cookie(response, VIEW_DEVICE_COOKIE, str(device_id),
                            VIEW_DEVICE_COOKIE_TTL_S)
    # Update only THIS browser's open WS connections so the next
    # broadcast switches view without waiting for a reconnect. We
    # identify "this browser" by the auth session cookie — uniquely
    # per-login — instead of by prior view_device_id, which would
    # incorrectly bump another browser that happened to share the same
    # prior selection (e.g. both still on the bridge-active default).
    # That earlier prior-view match caused a 2s "jump back and forth"
    # on the other screen: the WS pushed the wrong-bumped view while
    # the safety-net /api/status poll continued to read its actual
    # cookie and snapped back.
    request_auth = request.cookies.get(auth.COOKIE_NAME)
    new_id = str(device_id)
    for _ws, info in state.ws_clients.items():
        # Only bump the requester's own session. If auth isn't enabled
        # (no users yet), request_auth is None — fall back to the old
        # prior-view match because there's no per-browser identifier.
        if request_auth is not None:
            if info.get("auth_token") == request_auth:
                info["view_id"] = new_id
        else:
            if info.get("view_id") == request.cookies.get(VIEW_DEVICE_COOKIE):
                info["view_id"] = new_id
    # Push an immediate refresh so the UI doesn't wait for the next
    # poll tick to repaint.
    await broadcast_status("status")
    return {"ok": True, "device_id": new_id}


async def force_poll():
    # Give the bridge a moment to swap its active device + clear stale telemetry
    await asyncio.sleep(0.5)
    try:
        status_dict = await state.client.poll()
    except Exception:
        return

    # Refresh the cached DeviceInfo regardless of whether telemetry came back —
    # the bridge clears its cloud_telemetry on device-switch, so the very next
    # poll often returns telemetry=None but DOES return the new device dict.
    new_dev = getattr(state.client, "device_info", None)
    if new_dev is not None:
        state.device = new_dev

    if status_dict:
        state.last_source = status_dict.pop("_source", state.last_source)
        state.last_cloud_meta = status_dict.pop("_cloud", state.last_cloud_meta)
        state.last_status = status_dict
        state.last_update_ts = time.time()

    await broadcast_status("telemetry")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Mirror the HTTP-side auth gate. WS doesn't go through the FastAPI
    # http middleware, so we have to check the same session cookie here.
    auth_token: str | None = None
    if auth.has_user():
        auth_token = ws.cookies.get(auth.COOKIE_NAME)
        if not auth.verify_session(auth_token):
            await ws.close(code=1008)  # policy violation
            return
    # Stash both the per-browser view selection AND the auth session
    # token from the cookie. The auth token uniquely identifies a
    # browser session — used by /api/view/select_device to bump only
    # this browser's WSes when the cookie changes, instead of
    # spuriously dragging other browsers along.
    view_id = ws.cookies.get(VIEW_DEVICE_COOKIE) or None
    await ws.accept()
    state.ws_clients[ws] = {"view_id": view_id, "auth_token": auth_token}
    try:
        await ws.send_text(json.dumps({
            "type": "snapshot",
            "data": serialize_status(view_device_id=view_id),
        }))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.pop(ws, None)


# ==========================================================================
# Backup & Restore
# ==========================================================================
# Daily snapshot of /data to a remote SMB/CIFS share. Configured + monitored
# from the Settings page. The actual work lives in backup.py — this section
# is a thin REST veneer.
#
# Two auth tiers:
#   - Normal endpoints: require an authenticated session (covered by the
#     global /api/* middleware).
#   - "setup_restore_*" endpoints: usable BEFORE first-run setup, so a
#     user installing on a new NAS can pull their data back from backup
#     without first creating a fresh admin account on top of an empty DB.
#     They self-disable as soon as auth.has_user() becomes True.

@app.get("/api/backup/credentials")
def api_backup_creds_status():
    """Return the saved SMB credentials with the password redacted.
    Drives the "configured / not configured" UI state in the Settings
    page."""
    return {
        "has_credentials": backup_creds.has_credentials(),
        "remote": backup_creds.public_view(),
    }


# Required fields per transport. Kept here (rather than imported from
# backup_creds) so the API can return a clean 400 with a "missing fields"
# message before we even try to save / test. backup_creds.save() also
# validates, but this layer gives nicer error feedback for the UI.
_BACKUP_REQUIRED_PER_TRANSPORT: dict[str, tuple[str, ...]] = {
    "smb": ("host", "share", "username", "password"),
    "rsync_ssh": ("host", "ssh_user", "ssh_key", "target_dir"),
    "rsyncd": ("host", "rsync_module", "rsyncd_user", "rsyncd_password"),
    "rsyncd_ssh": ("host", "ssh_user", "ssh_password", "rsync_module"),
}


def _build_backup_creds_from_body(body: dict) -> dict:
    """Normalise an incoming /api/backup/* body into a creds dict.
    Defaults `transport` to "smb" so callers (login.html's setup
    restore wizard, the legacy SMB UI before transport-aware JS lands)
    that don't pass it keep working unchanged.

    Raises HTTPException(400) on unknown transport or missing fields.
    """
    transport = (body.get("transport") or "smb").strip() or "smb"
    if transport not in _BACKUP_REQUIRED_PER_TRANSPORT:
        raise HTTPException(400, f"unknown transport: {transport!r}")
    required = _BACKUP_REQUIRED_PER_TRANSPORT[transport]
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(
            400,
            f"required fields missing for {transport}: {', '.join(missing)}",
        )
    return {**body, "transport": transport}


@app.post("/api/backup/credentials")
def api_backup_creds_save(body: dict):
    """Persist remote-backup credentials. Body shape varies by transport:

      smb:       {transport:"smb",       host, share, subdir?, username,
                  password, domain?}
      rsync_ssh: {transport:"rsync_ssh", host, ssh_user, ssh_key,
                  target_dir}
      rsyncd:    {transport:"rsyncd",    host, rsync_module,
                  target_subpath?, rsyncd_user, rsyncd_password}

    `transport` defaults to "smb" if omitted. Saving overwrites any
    prior config. The save itself doesn't validate connectivity — the
    UI is expected to call /api/backup/test immediately after to
    surface errors before the user leaves the form."""
    creds = _build_backup_creds_from_body(body or {})
    if not backup_creds.save(**creds):
        raise HTTPException(500, "failed to save backup credentials")
    return {"ok": True}


@app.delete("/api/backup/credentials")
def api_backup_creds_clear():
    backup_creds.clear()
    return {"ok": True}


@app.post("/api/backup/test")
async def api_backup_test(body: dict | None = None):
    """Connectivity test: write a probe file, read it back, delete it.
    Doesn't run a real backup. Returns {ok, latency_ms} on success or
    {ok: False, error: ...} on failure. Runs in a thread since the
    underlying smbclient/rsync calls block.

    If `body` includes a `host` field, the supplied creds are tested
    instead of the saved ones — lets the UI validate before persisting.
    Otherwise the saved creds are loaded and tested.
    """
    creds = None
    if body and body.get("host"):
        creds = _build_backup_creds_from_body(body)
    return await asyncio.to_thread(backup.test_connectivity, creds)


@app.get("/api/backup/discover")
async def api_backup_discover():
    """Sweep the container's local subnet for SMB hosts (port 445).
    Drives the auto-discovery hint on the Settings → Backup card when
    no credentials are saved yet. Returns:
        {"hosts": [{"ip":..., "name":..., "port":445}, ...]}
    Latency is bounded (~5s worst case on a /24). Best-effort: returns
    an empty list rather than erroring if the subnet can't be inferred.
    """
    hosts = await asyncio.to_thread(backup_discover.discover_smb_hosts)
    return {"hosts": hosts}


@app.post("/api/backup/list-shares")
async def api_backup_list_shares(body: dict):
    """Enumerate share names on a host using supplied creds. The UI uses
    this to populate the share dropdown after the user has typed host +
    username + password. Returns {ok, shares} or {ok: False, error}.

    We deliberately don't fall back to saved creds here — discovery is
    only useful while the user is actively configuring a NEW destination.
    """
    body = body or {}
    host = (body.get("host") or "").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    domain = (body.get("domain") or "WORKGROUP").strip()
    if not host or not username or not password:
        raise HTTPException(400, "host, username, password are required")
    return await asyncio.to_thread(
        backup_discover.list_shares,
        host, username, password, domain=domain,
    )


@app.post("/api/backup/list-rsync-modules")
async def api_backup_list_rsync_modules(body: dict):
    """Enumerate rsyncd modules on a server. Used by the UI to populate
    the module dropdown after the user has filled in host + creds for
    the rsyncd or rsyncd_ssh transports — same UX as Synology Hyper
    Backup's "Backup module" auto-fill. Returns {ok, modules} or
    {ok: False, error}.

    For rsyncd_ssh: needs host + ssh_port + ssh_user + ssh_password.
    For rsyncd:     needs host + rsyncd_user + rsyncd_password.
    """
    body = body or {}
    transport = (body.get("transport") or "").strip()
    if transport not in ("rsyncd", "rsyncd_ssh"):
        raise HTTPException(400, "transport must be 'rsyncd' or 'rsyncd_ssh'")
    host = (body.get("host") or "").strip()
    if not host:
        raise HTTPException(400, "host is required")
    if transport == "rsyncd_ssh":
        creds = {
            "transport": "rsyncd_ssh",
            "host": host,
            "ssh_port": int(body.get("ssh_port") or 22),
            "ssh_user": (body.get("ssh_user") or "").strip(),
            "ssh_password": body.get("ssh_password") or "",
        }
        if not creds["ssh_user"] or not creds["ssh_password"]:
            raise HTTPException(400, "ssh_user and ssh_password are required")
    else:
        creds = {
            "transport": "rsyncd",
            "host": host,
            "rsyncd_user": (body.get("rsyncd_user") or "").strip(),
            "rsyncd_password": body.get("rsyncd_password") or "",
        }
        if not creds["rsyncd_user"] or not creds["rsyncd_password"]:
            raise HTTPException(400, "rsyncd_user and rsyncd_password are required")
    return await asyncio.to_thread(backup.list_rsync_modules, creds)


@app.get("/api/backup/status")
def api_backup_status():
    """Top-level UI status: whether backups are configured, when the last
    successful run was, and the last 20 run results (success+failure).
    Cheap — reads /data/backup-status.json."""
    return backup.get_status()


@app.post("/api/backup/run")
async def api_backup_run_now():
    """Trigger a one-shot backup right now. Same code path as the
    scheduled run, including post-upload retention pruning. Returns
    the BackupResult so the UI can show inline success/failure
    without waiting for the next status poll."""
    keep = user_settings.get("backup_keep_count")
    result = await asyncio.to_thread(backup.run_backup, keep_count=keep)
    return result.as_dict()


@app.get("/api/backup/snapshots")
async def api_backup_list_snapshots():
    """List every snapshot directory on the remote share. Used by the
    Restore picker."""
    snaps = await asyncio.to_thread(backup.list_remote_snapshots)
    return {"snapshots": snaps}


@app.post("/api/backup/restore")
async def api_backup_restore(body: dict):
    """Restore a named snapshot into /data.

    Body:
      {
        "snapshot": "2026-05-02_030000",
        "scope":    {"full": true}        // OR
                    {"files": ["energy.db", "settings.json"]}
      }

    The encryption key is restored only if scope.include_key is true
    AND the manifest contains it.
    """
    body = body or {}
    snapshot = (body.get("snapshot") or "").strip()
    if not snapshot:
        raise HTTPException(400, "snapshot is required")
    scope = body.get("scope") or {"full": True}
    return await asyncio.to_thread(
        backup.run_restore,
        snapshot_dir_name=snapshot, scope=scope,
    )


# ---- fresh-install restore ----------------------------------------------
# These three endpoints are exempt from the auth middleware (see
# _AUTH_PUBLIC_PREFIXES below). They self-disable once the admin user is
# created. The UX flow on a new NAS install:
#   1. User hits the dashboard, gets redirected to /setup.
#   2. /setup page offers "Restore from backup" alongside "Create account".
#   3. If they pick restore: enter SMB creds -> test -> list snapshots ->
#      pick one -> restore -> redirect to /setup so they can sign in (the
#      restored auth.json contains their old credentials, so login works).
#
# Why a separate endpoint set instead of just exempting the main ones:
# we don't want pre-auth callers poking at /api/backup/run (which writes
# fresh data on a remote) or modifying credentials in a way that
# affects an already-set-up app.

def _require_no_user():
    if auth.has_user():
        raise HTTPException(403, "already_set_up")


@app.post("/api/backup/setup_restore/test")
async def api_backup_setup_restore_test(body: dict):
    """Pre-auth connectivity test. Same logic as /api/backup/test but
    only works on a fresh install. Accepts the same per-transport body
    shape as /api/backup/credentials."""
    _require_no_user()
    creds = _build_backup_creds_from_body(body or {})
    return await asyncio.to_thread(backup.test_connectivity, creds)


@app.post("/api/backup/setup_restore/snapshots")
async def api_backup_setup_restore_snapshots(body: dict):
    """Pre-auth snapshot listing."""
    _require_no_user()
    creds = _build_backup_creds_from_body(body or {})
    snaps = await asyncio.to_thread(backup.list_remote_snapshots, creds)
    return {"snapshots": snaps}


@app.post("/api/backup/setup_restore/restore")
async def api_backup_setup_restore_run(body: dict):
    """Pre-auth restore. Drops the snapshot into /data and persists the
    creds for the daily backup loop. After this returns ok=True the
    UI redirects to /login (the restored auth.json has the old user)."""
    _require_no_user()
    body = body or {}
    snapshot = (body.get("snapshot") or "").strip()
    if not snapshot:
        raise HTTPException(400, "snapshot is required")
    creds = _build_backup_creds_from_body(body)
    scope = body.get("scope") or {"full": True}
    result = await asyncio.to_thread(
        backup.run_restore,
        snapshot_dir_name=snapshot, scope=scope, creds=creds,
    )
    if result.get("ok"):
        # Persist the same creds so the daily loop can keep
        # backing up to the same destination after the user signs in.
        backup_creds.save(**creds)
    return result


# Static UI
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


# PWA manifest + service worker MUST live at the site root for the browser
# to honor them as PWA assets — `/static/sw.js` would have a scope limited
# to `/static/`, breaking the install + offline-shell flow.
@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(WEB_DIR / "sw.js",
                        media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


# Wrap StaticFiles to send `Cache-Control: no-cache` so caching CDNs (e.g.
# Cloudflare in front of a Tunnel) revalidate every request instead of
# happily serving 14-hour-old CSS after a deploy. ETag handling already
# makes revalidation cheap — `no-cache` doesn't mean "don't store", just
# "always check with origin first".
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if "cache-control" not in {k.lower() for k in resp.headers.keys()}:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
