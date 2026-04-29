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
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import auth
import cost as cost_module
import forecaster
import kasa_client
import kasa_creds
import location as device_location
import settings as user_settings
import smart_charge
import weather_client
from automation import AutomationEngine, AutomationError
from device_client import DeviceClient, DeviceClientError, DeviceInfo, make_client
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
# Per-expansion-battery refresh cadence. The cloud's `updateTime` field
# moves at roughly 30s resolution so anything sub-minute is wasted; 5 min
# is plenty for a daily-learning trace + UI freshness.
BATTERY_PACK_REFRESH_S = 300


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
        self.ws_clients: set[WebSocket] = set()
        self.last_source: str | None = None
        self.last_cloud_meta: dict | None = None
        # Per-expansion-battery cache. Refreshed every BATTERY_PACK_REFRESH_S
        # by the poll loop so the UI gets near-realtime per-pack SOC without
        # hammering the cloud. Populated only when the active device has
        # at least one expansion battery.
        self.battery_packs: list[dict] = []
        self.last_packs_ts: float = 0.0
        # Battery-SOC automation engine — rules persisted to /data/automation.json,
        # evaluated each poll cycle, edge-triggered so a rule fires once per
        # threshold crossing instead of every single poll.
        self.automation: AutomationEngine = AutomationEngine()
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
    await broadcast({"type": "status", "data": serialize_status()})

    try:
        ok, info, err = await state.client.connect()
    except Exception as e:
        log.exception("connect raised")
        state.connection_status = "error"
        state.connection_error = f"{type(e).__name__}: {e}"
        await broadcast({"type": "status", "data": serialize_status()})
        return False

    if not ok:
        state.connection_status = "error"
        state.connection_error = err or "connect failed"
        log.warning(state.connection_error)
        await broadcast({"type": "status", "data": serialize_status()})
        return False

    state.device = info
    state.connection_status = "connected"
    state.connection_error = None
    log.info("Connected via %s backend: %s", state.backend,
             info.name if info else "?")
    await broadcast({"type": "status", "data": serialize_status()})
    return True


async def poll_loop() -> None:
    while True:
        try:
            # Auto-reconnect if we're not connected (e.g. bridge was down at startup,
            # or the container raced ahead of the host bridge). Without this we'd
            # sit forever with is_connected=False and never poll again.
            if not state.client.is_connected and state.backend != "mock":
                log.info("poll_loop: client not connected, attempting reconnect...")
                ok = await connect_device()
                if not ok:
                    await asyncio.sleep(user_settings.get("poll_interval_s"))
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
                await broadcast({"type": "status", "data": serialize_status()})

            if status_dict:
                ts = time.time()
                # Strip and stash source metadata before storing telemetry.
                source = status_dict.pop("_source", None)
                cloud_meta = status_dict.pop("_cloud", None)
                state.last_source = source
                state.last_cloud_meta = cloud_meta
                state.last_status = status_dict
                state.last_update_ts = ts

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

                # Energy aggregation: integrate W over time per device
                dev = state.device
                dev_sn = dev.device_sn if dev and dev.device_sn else None
                if dev_sn:
                    state.energy.upsert_device(
                        dev_sn,
                        getattr(dev, "name", None),
                        getattr(dev, "model_code", None),
                        None,
                    )
                    state.energy.record(
                        dev_sn, ts,
                        float(status_dict.get("input_power_w") or 0),
                        float(status_dict.get("output_power_w") or 0),
                        int(status_dict.get("battery_percent") or 0),
                        solar_w=float(status_dict.get("solar_input_w") or 0),
                        ac_input_w=float(status_dict.get("ac_input_w") or 0),
                    )

                    # Hydrate the live chart from the energy DB on the first
                    # successful poll after startup, so the chart shows the
                    # last LIVE_CHART_HOURS even immediately after a restart.
                    if not state.history_hydrated:
                        try:
                            past = state.energy.history(
                                dev_sn,
                                hours=LIVE_CHART_HOURS,
                                bucket_s=LIVE_CHART_INTERVAL_S,
                            )
                            for p in past:
                                state.history.append({
                                    "ts": p["ts"],
                                    "battery_percent": p["battery_pct"] or 0,
                                    "input_power_w": p["input_w"] or 0,
                                    "output_power_w": p["output_w"] or 0,
                                })
                            log.info("Live chart hydrated with %d historical points (last %dh)",
                                     len(past), LIVE_CHART_HOURS)
                        except Exception as e:
                            log.warning("history hydrate failed: %s", e)
                        state.history_hydrated = True

                # Per-expansion-battery refresh. Throttled to BATTERY_PACK_REFRESH_S
                # since pack state moves slowly. Cached on state for the API,
                # persisted to energy_db for the daily-learning job. On failure
                # we deliberately DO NOT advance last_packs_ts — that would
                # delay the retry by a full refresh window.
                if dev_sn and ts - state.last_packs_ts >= BATTERY_PACK_REFRESH_S:
                    rpc = getattr(state.client, "_rpc", None)
                    if rpc is not None:
                        try:
                            result = await rpc("get_battery_packs", device_sn=dev_sn)
                            err = (result or {}).get("error")
                            packs = (result or {}).get("packs") or []
                            if err:
                                log.warning("battery_packs RPC returned error: %s", err)
                            elif packs:
                                state.battery_packs = packs
                                state.last_packs_ts = ts
                                state.energy.record_battery_packs(dev_sn, packs, int(ts))
                            else:
                                # Empty list with no error means the device has
                                # no expansion packs — record that and back off.
                                state.battery_packs = []
                                state.last_packs_ts = ts
                        except Exception as e:
                            log.warning("battery_packs refresh failed: %s", e)

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
                await broadcast({"type": "telemetry", "data": serialize_status()})

                threshold = user_settings.get("low_battery_threshold")
                bp = status_dict["battery_percent"]
                if bp <= threshold and not state.low_battery_alerted:
                    state.low_battery_alerted = True
                    await broadcast({
                        "type": "alert",
                        "data": {"level": "warning",
                                 "message": f"Battery low: {bp}%"},
                    })
                elif bp > threshold + 5:
                    state.low_battery_alerted = False

                # Run automation rules. The bridge polls every Jackery device
                # so rules can target any of them, not just the active one;
                # we build a {device_sn: soc} dict from cloud_meta and let
                # the engine pick each rule's target.
                cloud = cloud_meta or {}
                devs_telemetry = (cloud.get("devices_telemetry") or {}) if isinstance(cloud, dict) else {}
                soc_by_sn: dict[str, float] = {}
                for sn, entry in devs_telemetry.items():
                    t = (entry or {}).get("telemetry") or {}
                    bp_dev = t.get("battery_percent")
                    if bp_dev is not None:
                        soc_by_sn[sn] = float(bp_dev)
                # Always include the active device too (for legacy rules
                # without an explicit jackery_device_sn).
                active_sn = state.device.device_sn if state.device else None
                if active_sn and bp is not None and active_sn not in soc_by_sn:
                    soc_by_sn[active_sn] = float(bp)
                if soc_by_sn:
                    try:
                        fired = await state.automation.evaluate(soc_by_sn, active_sn=active_sn)
                        for rule in fired:
                            await broadcast({
                                "type": "automation_fired",
                                "data": {
                                    "name": rule.get("name"),
                                    "action": rule.get("action"),
                                    "kasa_alias": rule.get("kasa_alias"),
                                    "jackery_device_sn": rule.get("jackery_device_sn"),
                                    "jackery_device_name": rule.get("jackery_device_name"),
                                },
                            })
                    except Exception as e:
                        log.warning("automation evaluate failed: %s", e)
        except Exception as e:
            log.exception("Poll loop error: %s", e)

        # Re-read each iteration so a settings change applies on the next
        # cycle (instead of at restart).
        await asyncio.sleep(user_settings.get("poll_interval_s"))


# ---------- WebSocket fan-out ----------
async def broadcast(message: dict[str, Any]) -> None:
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
        state.ws_clients.discard(ws)


def _system_soc_pct(main_pct: float, model_code: int | None = None) -> float:
    """Combined SOC across the main unit + every cached expansion pack,
    weighted by capacity. The cloud's main `battery_percent` describes
    the host unit only — using it as `starting_soc` in the forecaster
    while pairing it with the FULL system capacity (main + N packs)
    silently over-counts energy. This helper keeps the two numbers
    consistent.

    Returns main_pct unchanged if no packs are cached, so single-unit
    setups keep behaving exactly as before.
    """
    packs = state.battery_packs or []
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
    active_sn = state.device.device_sn if state.device else None
    if device_sn and device_sn == active_sn and state.battery_packs:
        pack_wh = forecaster.expansion_pack_capacity_wh(model_code)
        return main_wh + len(state.battery_packs) * pack_wh
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


def serialize_status() -> dict[str, Any]:
    device_info = state.device.to_dict() if state.device else None
    energy = None
    try:
        if state.device and state.device.device_sn:
            energy = _decorate_totals_with_savings(
                state.energy.totals(state.device.device_sn),
                state.device.device_sn,
            )
    except Exception as e:
        log.debug("energy totals lookup failed: %s", e)
    return {
        "connection_status": state.connection_status,
        "connection_error": state.connection_error,
        "device": device_info,
        "last_update_ts": state.last_update_ts,
        "telemetry": state.last_status,
        "history": list(state.history),
        "mock_mode": state.backend == "mock",
        "backend": state.backend,
        "low_battery_threshold": user_settings.get("low_battery_threshold"),
        "source": state.last_source,
        "cloud": state.last_cloud_meta,
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

    actual_sunset = (state.energy.actual_soc_at(device_sn, sunset_ts)
                     if sunset_ts and sunset_ts <= now else None)
    actual_sunrise = (state.energy.actual_soc_at(device_sn, sunrise_ts)
                      if sunrise_ts and sunrise_ts <= now else None)

    state.energy.upsert_daily_summary(
        device_sn=device_sn, local_date=today,
        sunset_ts=sunset_ts, sunrise_ts=sunrise_ts,
        predicted_sunset_soc_pct=pred_sunset,
        actual_sunset_soc_pct=actual_sunset,
        predicted_sunrise_soc_pct=pred_sunrise,
        actual_sunrise_soc_pct=actual_sunrise,
    )


async def _smart_charge_evaluate(record: bool = True):
    """Pull the inputs the smart-charge module needs, compute a Plan, and
    (in active mode) toggle the configured Kasa plug. Used by the
    periodic tick AND the UI's "Evaluate now" button (record=False
    skips history + side effects)."""
    cfg = smart_charge.get_config()
    if cfg["mode"] == "off":
        return None
    device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return None

    # Inputs: forecast (uses the same cached weather as the Forecast tab),
    # current SOC, capacity (override-aware), TOU plan, tz offset.
    loc = device_location.get() or {}
    if not loc.get("latitude"):
        # No location → no forecast → no decision possible. Surface this in
        # the history so the user knows.
        return smart_charge.compute_plan(
            config=cfg, current_soc_pct=None,
            forecast={"forecast": []}, cost_plan=cost_module.get_plan(),
            capacity_wh=_total_capacity_wh(
                device_sn, getattr(state.device, "model_code", None)),
        )
    lat, lon = loc["latitude"], loc["longitude"]
    weather = await weather_client.fetch_irradiance(lat, lon)
    if weather.get("error"):
        return None
    main_soc = float((state.last_status or {}).get("battery_percent") or 50)
    model_code = getattr(state.device, "model_code", None)
    # If packs are attached, the forecaster needs the system-wide SOC to
    # match the system-wide capacity it'll be paired with.
    starting_soc = _system_soc_pct(main_soc, model_code)
    energy_hist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
    capacity = _total_capacity_wh(device_sn, model_code)
    fcast = forecaster.build_forecast(
        energy_history=energy_hist,
        weather_hourly=weather["hourly"],
        starting_soc_pct=starting_soc,
        capacity_wh=capacity,
    )
    plan = smart_charge.compute_plan(
        config=cfg, current_soc_pct=starting_soc,
        forecast=fcast, cost_plan=cost_module.get_plan(),
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
        if cfg.get("claude_enabled"):
            narration = await _smart_charge_narrate(plan)
        smart_charge.record_decision(plan, executed=executed, narration=narration)
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
    """Periodic tick — every 5 minutes, run the smart-charge evaluator."""
    while True:
        try:
            await _smart_charge_evaluate(record=True)
        except Exception as e:
            log.warning("smart_charge tick failed: %s", e)
        await asyncio.sleep(5 * 60)


def _db_pack_to_cloud_shape(row: dict) -> dict:
    """energy_db's per-row shape uses internal names; the UI + smart-charge
    expect the cloud's raw field names. Convert at the boundary so neither
    side has to know about the other."""
    return {
        "deviceSn": row.get("pack_sn"),
        "deviceOrder": row.get("device_order") or 0,
        "rb": row.get("soc_pct"),
        "ip": row.get("input_w"),
        "op": row.get("output_w"),
        "it": row.get("internal_temp_c"),
        "ec": row.get("error_code") or 0,
    }


def _hydrate_battery_packs_from_db() -> None:
    """Seed state.battery_packs from the latest energy_db snapshot so a
    fresh server boot doesn't show an empty packs card while waiting for
    the first cloud fetch (~5-15s on the first poll-loop iteration; longer
    if the cloud is slow / contested). The hydrated data may be up to
    BATTERY_PACK_REFRESH_S stale; the live refresh will overwrite it as
    soon as it lands."""
    try:
        for d in state.energy.list_devices():
            sn = d.get("device_sn")
            if not sn:
                continue
            rows = state.energy.latest_battery_packs(sn)
            if rows:
                state.battery_packs = [_db_pack_to_cloud_shape(r) for r in rows]
                # Don't pretend this is a fresh fetch — leave last_packs_ts at
                # 0 so the poll loop refreshes on its first iteration.
                log.info("Hydrated %d battery packs for %s from DB",
                         len(rows), sn)
                break
    except Exception as e:
        log.debug("battery pack hydration skipped: %s", e)


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
    yield
    if state.poll_task:
        state.poll_task.cancel()
    if getattr(state, "smart_charge_task", None):
        state.smart_charge_task.cancel()
    try:
        await state.client.disconnect()
    except Exception:
        pass


app = FastAPI(title="Jackery 5000 Plus Monitor", lifespan=lifespan)


# ---------- App-level authentication ----------
# Optional layer. The first time the app starts with no /data/auth.json,
# a one-time /setup flow lets the operator pick a username/password. After
# that, every request must carry a valid session cookie (HMAC-signed) or
# it gets a 401 + redirect to /login.
#
# Routes exempt from auth: /login, /setup, /static/*, /manifest.webmanifest,
# /sw.js, /api/auth/* (the auth endpoints themselves), and /ws (handled
# separately in the WebSocket handler).
_AUTH_PUBLIC_PREFIXES = (
    "/static",
    "/manifest.webmanifest",
    "/sw.js",
    "/login",
    "/setup",
    "/api/auth/",
)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p) for p in _AUTH_PUBLIC_PREFIXES):
        return await call_next(request)
    # First-time-setup gate: no user yet → force /setup
    if not auth.has_user():
        if path.startswith("/api/"):
            return JSONResponse({"detail": "setup_required"}, status_code=401)
        return RedirectResponse("/setup", status_code=303)
    # Auth check
    token = request.cookies.get(auth.COOKIE_NAME)
    payload = auth.verify_session(token)
    if not payload:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def _set_session_cookie(response: Response, username: str) -> None:
    token = auth.make_session(username)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        # Secure cookies require HTTPS — Cloudflare Tunnel terminates TLS at
        # the edge, so the request to our origin is HTTP and `Secure` would
        # block the cookie. The CF-injected `X-Forwarded-Proto` header is
        # how we know the original was HTTPS.
        secure=False,
        path="/",
    )


@app.post("/api/auth/setup")
async def api_auth_setup(body: dict, response: Response):
    """One-time bootstrap: create the admin user if none exists yet."""
    if auth.has_user():
        raise HTTPException(403, "already set up")
    username = ((body or {}).get("username") or "").strip()
    password = (body or {}).get("password") or ""
    if not username or len(password) < 6:
        raise HTTPException(400, "username and password (>=6 chars) required")
    if not auth.save_user(username, password):
        raise HTTPException(500, "failed to save user")
    _set_session_cookie(response, username)
    return {"ok": True, "username": username}


@app.post("/api/auth/login")
async def api_auth_login(body: dict, response: Response):
    if not auth.has_user():
        raise HTTPException(403, "setup required")
    username = ((body or {}).get("username") or "").strip()
    password = (body or {}).get("password") or ""
    user = auth.load_user()
    if not user or username != user.get("username") or \
       not auth.verify_password(password, user.get("password_hash", "")):
        raise HTTPException(401, "invalid credentials")
    _set_session_cookie(response, username)
    return {"ok": True, "username": username}


@app.post("/api/auth/logout")
async def api_auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    payload = auth.verify_session(request.cookies.get(auth.COOKIE_NAME))
    if not payload:
        raise HTTPException(401, "auth_required")
    return {"username": payload.get("u")}


@app.post("/api/auth/change_password")
async def api_auth_change_password(body: dict, request: Request):
    payload = auth.verify_session(request.cookies.get(auth.COOKIE_NAME))
    if not payload:
        raise HTTPException(401, "auth_required")
    user = auth.load_user()
    if not user:
        raise HTTPException(500, "no user")
    current = (body or {}).get("current") or ""
    new = (body or {}).get("new") or ""
    if not auth.verify_password(current, user.get("password_hash", "")):
        raise HTTPException(401, "current password is wrong")
    if len(new) < 6:
        raise HTTPException(400, "new password too short (>=6 chars)")
    if not auth.save_user(user["username"], new):
        raise HTTPException(500, "failed to save")
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/setup")
def setup_page():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/api/status")
def api_status():
    return serialize_status()


@app.post("/api/reconnect")
async def api_reconnect():
    try:
        await state.client.disconnect()
    except Exception:
        pass
    state.device = None
    ok = await connect_device()
    return {"ok": ok, "error": state.connection_error, "backend": state.backend}


@app.get("/api/devices")
def api_devices():
    """Return the list of devices on the user's Jackery account."""
    cloud_meta = state.last_cloud_meta or {}
    return {
        "devices": cloud_meta.get("devices") or [],
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


@app.get("/api/forecast")
async def api_forecast(device_sn: str | None = None):
    """SOC forecast for the next ~5 days based on weather + per-device history.

    Returns the simulated SOC curve plus the fitted model coefficients so the
    UI can show how confident the prediction is."""
    loc = device_location.get()
    if not loc:
        return {"error": "location not set", "configured": False}

    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"error": "no active device", "configured": True}

    # Starting SOC: the latest battery reading we have. Falls back to 50% if
    # the bridge hasn't returned a fresh poll yet (the simulation still works,
    # the curve will just be offset by the SOC error).
    main_soc = 50.0
    if state.last_status and state.last_status.get("battery_percent") is not None:
        main_soc = float(state.last_status["battery_percent"])

    model_code = getattr(state.device, "model_code", None) if state.device else None
    # _total_capacity_wh() auto-derives total capacity from the live
    # battery_packs cache (main + N x pack), with the manual override
    # winning if set and the spec capacity as the fallback.
    capacity = _total_capacity_wh(device_sn, model_code)
    # Pair the system-wide capacity with the system-wide SOC so the
    # simulation starts from the right energy level.
    starting_soc = _system_soc_pct(main_soc, model_code)

    # 14 days of hourly-bucketed history is plenty for both the regression and
    # the load profile.
    energy_hist = state.energy.history(device_sn, hours=14 * 24, bucket_s=3600)
    weather = await weather_client.fetch_irradiance(loc["latitude"], loc["longitude"])
    if weather.get("error"):
        return {"error": f"weather fetch failed: {weather['error']}", "configured": True}

    result = forecaster.build_forecast(
        energy_history=energy_hist,
        weather_hourly=weather["hourly"],
        starting_soc_pct=starting_soc,
        capacity_wh=capacity,
    )
    # Persist this snapshot for later accuracy tracking. The PK is
    # (device_sn, made_at_hour, target_hour) so multiple calls in the same
    # hour collapse to one row per (device, target).
    state.energy.record_forecast(device_sn, time.time(), result["forecast"])
    return {
        "device_sn": device_sn,
        "low_battery_threshold": user_settings.get("low_battery_threshold"),
        "main_soc_pct": main_soc,
        "system_soc_pct": starting_soc,
        "pack_count": len(state.battery_packs or []),
        **result,
        "configured": True,
    }


@app.get("/api/forecast/accuracy")
def api_forecast_accuracy(device_sn: str | None = None):
    """Predicted vs actual SOC for past forecasts. Joins each saved
    prediction to the average actual battery_pct in the ±30 min window
    around its target. Useful for evaluating how the model improves
    as more data accumulates."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "samples": [], "summary": {}}
    samples = state.energy.prediction_accuracy(device_sn)
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
    return {"device_sn": device_sn, "samples": samples, "summary": summary}


@app.get("/api/smart_charge/config")
def api_smart_charge_get():
    """Current smart-charge config + saved Kasa devices for the picker."""
    return {
        "config": smart_charge.get_config(),
        "kasa_devices": state.kasa.list(),
    }


@app.post("/api/smart_charge/config")
async def api_smart_charge_set(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return {"config": smart_charge.set_config(body if isinstance(body, dict) else {})}


@app.get("/api/smart_charge/status")
def api_smart_charge_status(device_sn: str | None = None):
    """Latest decision + recent history for the UI status panel.
    History pulls from the persisted log in energy_db so it survives
    container restarts."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    history: list[dict] = []
    if device_sn:
        history = state.energy.list_smart_charge_decisions(device_sn, limit=50)
    return {"config": smart_charge.get_config(), "history": history}


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


@app.post("/api/smart_charge/evaluate_now")
async def api_smart_charge_evaluate_now():
    """Compute a decision RIGHT NOW (no execution, no history write).
    Used by the UI's "Evaluate now" button to show what the controller
    would currently decide. Same pure logic as the periodic tick."""
    plan = await _smart_charge_evaluate(record=False)
    return {"plan": plan.to_dict() if plan else None}


@app.get("/api/devices/capacity")
def api_devices_capacity():
    """List every recorded device with its current capacity (default vs
    user override). Used by the Device tab to render the capacity editor."""
    out = []
    active_sn = state.device.device_sn if state.device else None
    pack_count = len(state.battery_packs) if state.battery_packs else 0
    for d in state.energy.list_devices():
        default_wh = forecaster.battery_capacity_wh(d.get("model_code"))
        override = d.get("capacity_wh_override")
        # Auto-derived from live battery_packs cache for the active device.
        auto_wh: int | None = None
        if pack_count and d["device_sn"] == active_sn:
            pack_wh = forecaster.expansion_pack_capacity_wh(d.get("model_code"))
            auto_wh = default_wh + pack_count * pack_wh
        effective = override or auto_wh or default_wh
        out.append({
            "device_sn": d["device_sn"],
            "name": d.get("name"),
            "model_code": d.get("model_code"),
            "default_capacity_wh": default_wh,
            "capacity_wh_override": override,
            "auto_capacity_wh": auto_wh,
            "pack_count": pack_count if d["device_sn"] == active_sn else 0,
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
    if not device_sn:
        return {"error": "no device", "packs": []}
    active_sn = state.device.device_sn if state.device else None
    if not fresh and device_sn == active_sn and state.battery_packs:
        return {"device_sn": device_sn,
                "packs": state.battery_packs,
                "fetched_at": state.last_packs_ts,
                "cached": True}
    rpc = getattr(state.client, "_rpc", None)
    if rpc is None:
        return {"error": "bridge not available", "packs": []}
    try:
        result = await rpc("get_battery_packs", device_sn=device_sn)
    except Exception as e:
        return {"error": str(e), "packs": []}
    packs = (result or {}).get("packs", [])
    if device_sn == active_sn and packs:
        state.battery_packs = packs
        state.last_packs_ts = time.time()
        try:
            state.energy.record_battery_packs(device_sn, packs)
        except Exception as e:
            log.debug("record_battery_packs failed: %s", e)
    return {"device_sn": device_sn,
            "packs": packs,
            "fetched_at": time.time(),
            "cached": False,
            "error": (result or {}).get("error")}


@app.get("/api/location")
def api_location_get():
    """Return the stored device location, if any."""
    return device_location.get() or {"latitude": None, "longitude": None}


@app.post("/api/location")
async def api_location_set(req: Request):
    """Persist the device's latitude + longitude. Called by the browser
       after the user grants the geolocation prompt on the Forecast tab."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    record = device_location.set(body.get("latitude"), body.get("longitude"))
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
    await broadcast({"type": "status", "data": serialize_status()})
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
@app.get("/api/kasa/saved")
async def api_kasa_saved_list(refresh: bool = False):
    """Return all saved Kasa devices. If `refresh=true`, also probe each
       one in parallel so the UI can display current on/off state."""
    devices = state.kasa.list_devices()
    if not refresh or not devices:
        return {"devices": [{**d, "is_on": None, "online": None} for d in devices]}

    async def _probe(d):
        try:
            info = await kasa_client.status(d["host"])
            return {**d, "is_on": info.get("is_on"),
                    "model": d.get("model") or info.get("model"),
                    "alias": d.get("alias") or info.get("alias"),
                    "online": True}
        except Exception:
            return {**d, "is_on": None, "online": False}

    enriched = await asyncio.gather(*[_probe(d) for d in devices])
    return {"devices": list(enriched)}


@app.post("/api/kasa/saved")
async def api_kasa_saved_upsert(body: dict):
    """Add or update a saved Kasa device. We probe the device first so the
       saved record always has accurate model/alias and `last_tested`
       reflects a real successful contact."""
    host = ((body or {}).get("host") or "").strip()
    requested_alias = ((body or {}).get("alias") or "").strip()
    if not host:
        raise HTTPException(400, "host required")
    try:
        info = await kasa_client.status(host)
    except kasa_client.KasaError as e:
        raise HTTPException(400, str(e)) from e
    saved = state.kasa.upsert(
        host=host,
        alias=requested_alias or info.get("alias") or "",
        model=info.get("model"),
        type_=info.get("type"),
        mark_tested=True,
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
       channel. Body: {port: 'ac'|'dc'|'usb'|'car', on: bool}."""
    port = (body or {}).get("port")
    on = bool((body or {}).get("on"))
    if port not in ("ac", "dc", "usb", "car"):
        raise HTTPException(400, "port must be one of: ac, dc, usb, car")
    setter = getattr(state.client, "set_output", None)
    if not setter:
        raise HTTPException(501, "Backend does not support output toggles")
    try:
        await setter(port, on)
    except DeviceClientError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "port": port, "on": on}


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
    await broadcast({"type": "status", "data": serialize_status()})
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
    await broadcast({"type": "status", "data": serialize_status()})
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
    await broadcast({"type": "status", "data": serialize_status()})

    asyncio.create_task(force_poll())
    return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}


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

    await broadcast({"type": "telemetry", "data": serialize_status()})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Mirror the HTTP-side auth gate. WS doesn't go through the FastAPI
    # http middleware, so we have to check the same session cookie here.
    if auth.has_user():
        token = ws.cookies.get(auth.COOKIE_NAME)
        if not auth.verify_session(token):
            await ws.close(code=1008)  # policy violation
            return
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": serialize_status()}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(ws)


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
