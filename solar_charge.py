"""
Solar-divert controller (forecast-driven).

Inverse of smart_charge: when the forecast says the battery has enough
overnight headroom, divert the surplus capacity to a downstream load
(typically an EV charger plugged into the Jackery's AC output). The
battery acts as the buffer between solar surplus and EV demand:
discharges during dark hours, refills during sunny hours.

Three modes (per-device, picked via the Solar-charge tab):
  - off:    no decisions, no toggling. Idle.
  - test:   decisions computed and logged, but the plug is NEVER toggled.
            Use this for a few days to validate before going live.
  - active: full control — toggles Kasa plug per decisions.

Decision algorithm per tick (every ~30s, self-regulating):

  Compute baseline_sunrise_soc from build_forecast(no AC floor injected).
  This is the projected SOC at tomorrow morning's sunrise assuming
  nothing intervenes.

  ON state (turn ON when):
    - baseline_sunrise_soc >= target + safety_margin + on_hysteresis_pp
      (forecast says we have headroom to spare — divert into car)
    - current_soc > comfort_low_pct (don't drain past hard floor)
    - telemetry fresh, forecast available
    - min_hold_s elapsed since last toggle

  OFF state (turn OFF when):
    - baseline_sunrise_soc < target + safety_margin
      (headroom exhausted — the controller's own past draining has
      pulled the projected sunrise down to the safety floor)
    - current_soc <= comfort_low_pct (hard SOC floor)
    - telemetry stale or forecast unavailable
    - min_hold_s elapsed

  In the hysteresis band (between the OFF and ON thresholds): keep
  current plug state.

Self-regulation: when the plug is ON, the car pulls car_load_w from
the Jackery, draining the battery faster than the natural load. Each
30s tick recomputes the forecast from the *current* (now lower) SOC,
so baseline_sunrise_soc walks down as we charge. Eventually it crosses
the OFF threshold and the controller stops the charge. If we'd undershoot,
the projection catches it BEFORE we hit the actual floor.

Safety:
  - Test mode lets the user observe a few days of decisions without any
    plug movement.
  - comfort_low is a hard SOC floor that overrides the forecast — if
    SOC dips to it right now, OFF immediately regardless of projection.
  - Min-hold time gates both directions, so a forecast wobble doesn't
    rapidly cycle the EVSE.
  - Cloud-disconnect / forecast-unavailable → return action="off" with
    reason. The caller (server.py) is responsible for fail-safe behavior
    (turn off the plug after a sustained disconnect).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger("solar_charge")

CONFIG_PATH = os.environ.get("JACKERY_SOLAR_CHARGE_FILE", "/data/solar_charge.json")

# Default-of-defaults for fresh installs. The Settings UI lets the user
# tune everything per device.
DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "off",                       # off | test | active
    "kasa_device_host": None,            # which saved Kasa plug = car charger
    # The downstream load draws this much when the plug is ON. L1 EVSE
    # typical = 1400W (12A @ 120V). Tune to match your actual hardware;
    # if the plug supports emeter we could measure it, but this stays
    # a user-set assumption for simplicity.
    "car_load_w": 1400,
    # Only consider turning ON when system SOC is at or above this.
    # 70% gives the battery comfortable headroom and ensures we're
    # diverting REAL surplus, not robbing the battery's reserve.
    "comfort_high_pct": 70,
    # Turn OFF immediately if SOC drops to this level. Protects the
    # battery from going below the overnight reserve even if everything
    # else lines up.
    "comfort_low_pct": 30,
    # Minimum time between toggles. 30s default per user pick; tighter
    # is risky (EVSE handshake takes 5-30s) but the user's L1 charger
    # tolerates it. Larger values reduce chatter at the cost of
    # responsiveness to cloud cover.
    "min_hold_s": 30,
    # Hysteresis on the forecast gate: ON fires when predicted sunrise
    # SOC >= target + safety_margin + on_hysteresis_pp, OFF when it
    # drops below target + safety_margin. The pp band prevents
    # oscillation around the boundary as the forecast recomputes
    # tick-to-tick. Old name `surplus_buffer_w` kept for back-compat
    # but no longer consulted by the forecast-driven controller.
    "on_hysteresis_pp": 3,
    "surplus_buffer_w": 100,  # legacy; ignored by forecast-driven gate
    # Predicted sunrise SOC must remain at least this many pp above
    # target to keep the plug ON. Bigger = safer (more pessimistic
    # about forecast error), smaller = more aggressive diversion.
    "safety_margin_pp": 5,
}

# When solar drops below this for SUNSET_SUSTAIN_S seconds, treat it as
# end-of-solar-day: graceful OFF so we don't drain the battery into
# the car after dark. Hardcoded (not user-configurable) — the
# rationale is structural ("the sun has set"), not preference.
SUNSET_SOLAR_W = 200
SUNSET_SUSTAIN_S = 5 * 60

# Cloud telemetry must be no more than this old to act on. Beyond this,
# we don't have confidence the live solar/load numbers reflect reality.
# Bridge stall safety: returns action="off" reason="stale telemetry".
MAX_TELEMETRY_AGE_S = 90

_config_lock = threading.Lock()


# ---------- Config persistence ----------
def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Coerce a config dict into a normalized shape; bad fields fall back
    to defaults."""
    out = dict(DEFAULT_CONFIG)
    if not isinstance(cfg, dict):
        return out
    mode = str(cfg.get("mode") or "off").lower()
    if mode in ("off", "test", "active"):
        out["mode"] = mode
    if cfg.get("kasa_device_host"):
        out["kasa_device_host"] = str(cfg["kasa_device_host"])[:128]

    def _int_in_range(key: str, lo: int, hi: int) -> None:
        try:
            v = int(cfg.get(key) if cfg.get(key) is not None else DEFAULT_CONFIG[key])
            if lo <= v <= hi:
                out[key] = v
        except (TypeError, ValueError):
            pass

    _int_in_range("car_load_w", 50, 7000)
    _int_in_range("comfort_high_pct", 30, 99)
    _int_in_range("comfort_low_pct", 5, 90)
    _int_in_range("min_hold_s", 10, 3600)
    _int_in_range("surplus_buffer_w", 0, 1000)
    _int_in_range("on_hysteresis_pp", 0, 30)
    _int_in_range("safety_margin_pp", 0, 50)
    # Defensive sanity: comfort_low must be < comfort_high or we'd
    # paint an unreachable state (plug can never be on).
    if out["comfort_low_pct"] >= out["comfort_high_pct"]:
        out["comfort_low_pct"] = DEFAULT_CONFIG["comfort_low_pct"]
        out["comfort_high_pct"] = DEFAULT_CONFIG["comfort_high_pct"]
    return out


def _load_raw() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("solar_charge config unreadable (%s); using defaults", e)
        return {}


def _is_per_device_shape(data: dict[str, Any]) -> bool:
    return isinstance(data, dict) and isinstance(data.get("by_device"), dict)


def get_config(device_sn: str | None = None) -> dict[str, Any]:
    """Read this device's solar-charge config. Defaults merged."""
    with _config_lock:
        data = _load_raw()
    if _is_per_device_shape(data):
        if device_sn:
            return _validate_config(data["by_device"].get(device_sn) or {})
        return dict(DEFAULT_CONFIG)
    # Legacy single-config — treat as the requested device's config.
    return _validate_config(data)


def set_config(cfg: dict[str, Any], device_sn: str | None = None) -> dict[str, Any]:
    """Validate + persist + return the saved config for a device."""
    validated = _validate_config(cfg)
    with _config_lock:
        existing = _load_raw()
        if device_sn:
            if not _is_per_device_shape(existing):
                # First write: migrate.
                migrated = {"by_device": {}}
                if existing and "mode" in existing:
                    migrated["by_device"]["__legacy__"] = existing
                existing = migrated
            existing["by_device"][device_sn] = validated
            payload = existing
        else:
            payload = validated
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    log.info("solar_charge config saved: device=%s mode=%s car=%dW",
             device_sn or "(legacy)", validated["mode"], validated["car_load_w"])
    return validated


def get_all_configs() -> dict[str, dict[str, Any]]:
    """Map of device_sn → config for every device that has one."""
    with _config_lock:
        data = _load_raw()
    if _is_per_device_shape(data):
        return {
            sn: _validate_config(cfg)
            for sn, cfg in data["by_device"].items()
            if sn != "__legacy__"
        }
    return {}


# ---------- Runtime state ----------
# Per-device runtime: last_toggle_ts (when did we last flip the plug),
# plug_is_on (cached state — authoritative source is Kasa, but we
# need our own view for min_hold gating), sunset_since (timestamp solar
# first dipped below SUNSET_SOLAR_W; reset whenever solar climbs back).
@dataclass
class RuntimeState:
    plug_is_on: bool = False
    last_toggle_ts: float = 0.0
    sunset_since: float = 0.0  # 0 = solar above threshold


_runtime: dict[str, RuntimeState] = {}
_runtime_lock = threading.Lock()


def get_runtime(device_sn: str) -> RuntimeState:
    with _runtime_lock:
        if device_sn not in _runtime:
            _runtime[device_sn] = RuntimeState()
        return _runtime[device_sn]


def _update_runtime(device_sn: str, **kwargs) -> RuntimeState:
    with _runtime_lock:
        rs = _runtime.setdefault(device_sn, RuntimeState())
        for k, v in kwargs.items():
            setattr(rs, k, v)
        return rs


def reset_runtime(device_sn: str | None = None) -> None:
    """Test hook: clear cached runtime so tests don't bleed state."""
    with _runtime_lock:
        if device_sn:
            _runtime.pop(device_sn, None)
        else:
            _runtime.clear()


# ---------- Decision plan ----------
@dataclass
class Plan:
    """A snapshot of what the controller has decided to do RIGHT NOW.

    The server caller turns `action` into a Kasa toggle in active mode
    or just logs it in test mode. `predicted_sunrise_soc_pct` is the
    "with diversion" projection; `baseline_predicted_sunrise_soc_pct`
    is the "no diversion" counterfactual — useful to see how much
    headroom the diversion is consuming.
    """
    action: str                                # "on" | "off" | "skip"
    reason: str
    mode: str                                  # off | test | active
    decided_at: int
    current_soc_pct: float | None = None
    predicted_sunrise_soc_pct: float | None = None
    baseline_predicted_sunrise_soc_pct: float | None = None
    target_sunrise_soc_pct: float = 25.0
    solar_w: float | None = None
    load_w: float | None = None
    surplus_w: float | None = None             # solar_w - load_w (net of plug)
    car_load_w: float | None = None            # configured assumption
    plug_state_before: str | None = None       # "on" | "off"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_plan(
    *,
    config: dict[str, Any],
    current_soc_pct: float | None,
    solar_w: float | None,
    load_w: float | None,
    telemetry_age_s: float,
    target_sunrise_soc_pct: float,
    predicted_sunrise_soc_with_diversion: float | None,
    predicted_sunrise_soc_baseline: float | None,
    now_ts: float | None = None,
) -> Plan:
    """Pure decision function. All inputs explicit; no globals read.

    Args:
        config: validated solar_charge config (see DEFAULT_CONFIG).
        current_soc_pct: system SOC (capacity-weighted on multi-pack).
        solar_w: instantaneous solar input.
        load_w: instantaneous output (NET of the plug — i.e., if the
            plug is currently ON and pulling car_load_w, that's already
            included in load_w because output_w from the Jackery is the
            sum of everything downstream).
        telemetry_age_s: seconds since the values above were sampled.
            > MAX_TELEMETRY_AGE_S → action=off (fail closed).
        target_sunrise_soc_pct: floor we must maintain (from smart_charge).
        predicted_sunrise_soc_with_diversion: forecaster's projection
            assuming the controller continues to divert as planned.
            None if forecast unavailable → action=off (fail closed).
        predicted_sunrise_soc_baseline: forecaster's projection assuming
            the plug stays OFF for the rest of the day. Used to display
            the cost of diversion; not used for the gating decision
            (we gate on the with-diversion projection — that's the one
            that actually predicts what'll happen if we run).
        now_ts: clock injection for tests; defaults to time.time().
    """
    now = float(now_ts if now_ts is not None else time.time())
    mode = str(config.get("mode") or "off").lower()
    car_load = float(config.get("car_load_w") or 1400)
    comfort_low = float(config.get("comfort_low_pct") or 30)
    # Note: `min_hold_s` is read inside gate_min_hold (separate function)
    # so compute_plan stays pure — pure decision in, dirty toggle gate out.
    safety_pp = float(config.get("safety_margin_pp") or 5)
    on_hysteresis_pp = float(config.get("on_hysteresis_pp") or 3)

    # Build skeleton Plan and the caller fills in plug_state_before before
    # calling — we can't read it from runtime here without coupling.
    def _plan(action: str, reason: str) -> Plan:
        return Plan(
            action=action, reason=reason,
            mode=mode, decided_at=int(now),
            current_soc_pct=current_soc_pct,
            predicted_sunrise_soc_pct=predicted_sunrise_soc_with_diversion,
            baseline_predicted_sunrise_soc_pct=predicted_sunrise_soc_baseline,
            target_sunrise_soc_pct=target_sunrise_soc_pct,
            solar_w=solar_w, load_w=load_w,
            surplus_w=(None if (solar_w is None or load_w is None)
                       else (solar_w - load_w)),
            car_load_w=car_load,
        )

    if mode == "off":
        return _plan("skip", "mode=off")

    # Fail-closed checks: any of these → OFF.
    if telemetry_age_s > MAX_TELEMETRY_AGE_S:
        return _plan("off", f"stale telemetry ({telemetry_age_s:.0f}s old)")
    if current_soc_pct is None:
        return _plan("off", "missing telemetry (soc)")
    if predicted_sunrise_soc_with_diversion is None:
        return _plan("off", "forecast unavailable")

    safe_sunrise_floor = target_sunrise_soc_pct + safety_pp
    on_threshold = safe_sunrise_floor + on_hysteresis_pp

    # Hard floor: SOC drops to comfort_low → always OFF regardless of
    # what the forecast says. Protects against forecast error during
    # the current tick — never drain past the immediate floor even if
    # the simulation projects we'll recover by sunrise.
    if current_soc_pct <= comfort_low:
        return _plan("off",
                     f"SOC {current_soc_pct:.0f}% ≤ comfort_low {comfort_low:.0f}%")

    # Forecast-driven decision. The baseline forecast (computed by the
    # caller with no AC charging injected) projects where SOC will land
    # at sunrise if nothing else changes. We use it directly as the
    # headroom signal:
    #   - ON when projected sunrise is comfortably above floor (i.e.
    #     above the safety floor + a hysteresis band). The hysteresis
    #     prevents toggling around the boundary as the forecast
    #     wobbles tick-to-tick.
    #   - OFF when projected sunrise drops to or below the safety floor.
    #     The plug's actual draw will pull SOC down faster than the
    #     baseline projects, and each subsequent tick recomputes the
    #     forecast from the (now lower) current SOC, so the projection
    #     naturally regresses toward the floor and triggers OFF.
    # This is the "forecast as truth, battery as buffer" model — the
    # controller doesn't gate on real-time solar surplus, it gates on
    # whether the overnight reserve is safe. Battery drains during dark
    # hours, refills during sunny hours; the controller keeps charging
    # as long as the simulator says we'll still hit the morning target.
    pred = predicted_sunrise_soc_with_diversion
    if pred >= on_threshold:
        return _plan(
            "on",
            f"predicted sunrise {pred:.1f}% ≥ target {target_sunrise_soc_pct:.0f}%"
            f"+margin{safety_pp:.0f}+hyst{on_hysteresis_pp:.0f} = {on_threshold:.0f}%; "
            f"battery has headroom to divert",
        )
    if pred < safe_sunrise_floor:
        return _plan(
            "off",
            f"predicted sunrise {pred:.1f}% < target {target_sunrise_soc_pct:.0f}%"
            f"+margin{safety_pp:.0f} = {safe_sunrise_floor:.0f}%; diversion would "
            f"risk overnight floor",
        )

    # In the hysteresis band — keep current state. (gate_min_hold
    # downgrades to "skip" when already in the requested state.)
    return _plan(
        "skip",
        f"in hysteresis band: predicted sunrise {pred:.1f}% in "
        f"[{safe_sunrise_floor:.0f}%, {on_threshold:.0f}%); holding "
        f"current plug state",
    )


def gate_min_hold(plan: Plan, last_toggle_ts: float,
                  min_hold_s: float, plug_state_before: str,
                  now_ts: float | None = None) -> Plan:
    """Post-process a Plan against the min_hold timer. If the controller
    wants to flip the plug but the min_hold hasn't elapsed, downgrade
    the action to 'skip' with an explanatory reason. Otherwise pass
    through. Also fills in `plug_state_before` on the Plan for audit.

    Separate from compute_plan so the pure-function tests don't need
    to thread runtime state through every test fixture.
    """
    plan.plug_state_before = plug_state_before
    if plan.action == "skip":
        return plan
    now = float(now_ts if now_ts is not None else time.time())
    elapsed = now - float(last_toggle_ts or 0)
    desired_state = plan.action  # "on" or "off"
    if desired_state == plug_state_before:
        # Already in the desired state; not a flip, no min-hold gate.
        # Downgrade to skip for log clarity ("redundant on/off" is just
        # the controller affirming the current state).
        plan.action = "skip"
        plan.reason = f"already {plug_state_before}; {plan.reason}"
        return plan
    if elapsed < min_hold_s:
        plan.action = "skip"
        plan.reason = (f"min_hold not elapsed ({elapsed:.0f}s < {min_hold_s:.0f}s) "
                       f"— would have gone {desired_state}: {plan.reason}")
    return plan
