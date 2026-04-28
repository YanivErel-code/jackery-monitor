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
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import kasa_client
import kasa_creds
import settings as user_settings
from automation import AutomationEngine, AutomationError
from kasa_devices import KasaRegistry
from device_client import DeviceClient, DeviceInfo, DeviceClientError, make_client
from energy_db import EnergyDB

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


# ---------- app state ----------
class AppState:
    def __init__(self) -> None:
        self.client: DeviceClient = make_client()
        self.device: Optional[DeviceInfo] = None
        self.energy = EnergyDB()
        self.last_status: Optional[dict[str, Any]] = None
        self.last_update_ts: Optional[float] = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        # Last append timestamp so we sample the live chart exactly every
        # LIVE_CHART_INTERVAL_S regardless of how fast we poll the bridge.
        self.last_history_ts: float = 0.0
        # Set the first time we hydrate the deque from the energy DB after
        # startup so a container restart doesn't blank the chart.
        self.history_hydrated: bool = False
        self.connection_status = "disconnected"   # disconnected | scanning | connecting | connected | error
        self.connection_error: Optional[str] = None
        self.low_battery_alerted = False
        self.poll_task: Optional[asyncio.Task] = None
        self.ws_clients: set[WebSocket] = set()
        self.last_source: Optional[str] = None
        self.last_cloud_meta: Optional[dict] = None
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
                state.device = new_dev
                # Broadcast device-change immediately so the Device tab updates fast.
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

                # Run automation rules against the latest SOC. Engine is
                # edge-triggered, so this is cheap on cycles where nothing
                # crosses a threshold.
                if bp is not None:
                    try:
                        fired = await state.automation.evaluate(float(bp))
                        for rule in fired:
                            await broadcast({
                                "type": "automation_fired",
                                "data": {
                                    "name": rule.get("name"),
                                    "action": rule.get("action"),
                                    "kasa_alias": rule.get("kasa_alias"),
                                    "soc": bp,
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


def serialize_status() -> dict[str, Any]:
    device_info = state.device.to_dict() if state.device else None
    energy = None
    try:
        if state.device and state.device.device_sn:
            energy = state.energy.totals(state.device.device_sn)
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
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Jackery monitor on backend=%s", state.backend)
    # try to connect at startup, but don't block app boot if it fails
    asyncio.create_task(connect_device())
    state.poll_task = asyncio.create_task(poll_loop())
    yield
    if state.poll_task:
        state.poll_task.cancel()
    try:
        await state.client.disconnect()
    except Exception:
        pass


app = FastAPI(title="Jackery 5000 Plus Monitor", lifespan=lifespan)


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
def api_energy_totals(device_sn: Optional[str] = None):
    """Lifetime + today + 7d + 30d totals.
       If device_sn omitted, returns totals for the currently-active device."""
    if not device_sn:
        device_sn = state.device.device_sn if state.device else None
    if not device_sn:
        return {"device_sn": None, "lifetime": {"input_wh": 0, "output_wh": 0}}
    return state.energy.totals(device_sn)


@app.get("/api/energy/history")
def api_energy_history(hours: int = 24, device_sn: Optional[str] = None):
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
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
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
        raise HTTPException(500, str(e))


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
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.get("/api/kasa/status")
async def api_kasa_status(host: str):
    """Read the current on/off state of a single Kasa device."""
    try:
        return {"ok": True, **(await kasa_client.status(host))}
    except kasa_client.KasaError as e:
        raise HTTPException(400, str(e))


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
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
    return {"ok": True, "port": port, "on": on}


@app.post("/api/pause_polling")
async def api_pause_polling(body: Optional[dict] = None):
    """Pause the cloud poller so the user can use the phone app without the
       bridge stealing the session back. Body: {seconds: int} (default 600)."""
    seconds = int((body or {}).get("seconds") or 600)
    pauser = getattr(state.client, "pause_polling", None)
    if not pauser:
        raise HTTPException(501, "Backend does not support pause_polling")
    try:
        result = await pauser(seconds)
    except DeviceClientError as e:
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
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
        raise HTTPException(400, str(e))
    # Clear cached device + telemetry IMMEDIATELY so the UI stops showing the
    # old device while the next poll is in flight. The Device tab will go to
    # "—" for ~1-2s, then refill with the new device's name/SN.
    state.device = None
    state.last_status = None
    state.last_update_ts = None
    await broadcast({"type": "status", "data": serialize_status()})

    # Force a fresh poll so the UI updates immediately
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


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")
