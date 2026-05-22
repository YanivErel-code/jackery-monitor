"""
Solar-divert controller.

Inverse of smart_charge: when the battery is full enough and solar is
producing more than home demand, divert the surplus to a downstream load
(typically an EV charger plugged into the Jackery's AC output). Toggles a
Kasa smart plug ON when surplus is real and the overnight reserve is
safe; OFF when surplus collapses, the battery is low, or the forecast
says we won't reach the morning target.

Three modes (per-device, picked via the Solar-charge tab):
  - off:    no decisions, no toggling. Idle.
  - test:   decisions computed and logged, but the plug is NEVER toggled.
            Use this for a few days to validate before going live.
  - active: full control — toggles Kasa plug per decisions.

Decision policy is deterministic and rule-based. Hysteresis bands
(comfort_low/high SOC + surplus buffer) absorb sensor noise so the plug
doesn't chatter. Minimum hold time prevents back-to-back toggles below
the EVSE's reasonable cycling tolerance.

Algorithm per tick (every ~30s, driven by the bridge poll cadence):

  ON state preconditions (turn ON when ALL true):
    - plug currently OFF
    - system_soc >= comfort_high_pct (don't fight battery charging on top)
    - solar_w - load_w >= car_load_w + surplus_buffer_w
      (real surplus, with a buffer against load spike noise)
    - predicted_sunrise_soc_with_car >= target + safety_margin_pp
      (we're confident the overnight reserve survives)
    - now - last_toggle_ts >= min_hold_s

  OFF state triggers (turn OFF when ANY true):
    - plug currently ON
    - system_soc <= comfort_low_pct (protect battery from over-discharge)
    - solar_w - load_w < car_load_w - surplus_buffer_w
      (we're net draining battery — solar collapsed or load spiked)
    - predicted_sunrise_soc < target + safety_margin_pp
      (forecast says we won't make it; bail)
    - solar_w < SUNSET_SOLAR_W sustained for SUNSET_SUSTAIN_S
      (graceful end-of-day; don't keep charging from battery alone)
    - now - last_toggle_ts >= min_hold_s

Safety:
  - Test mode lets the user observe a few days of decisions without any
    plug movement.
  - Min-hold time gates both directions, so a brief solar dip doesn't
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
    # Surplus must exceed (car_load_w + buffer) to turn ON, and must
    # drop below (car_load_w - buffer) to turn OFF. The hysteresis band
    # absorbs noise on solar_w / load_w samples.
    "surplus_buffer_w": 100,
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
    comfort_high = float(config.get("comfort_high_pct") or 70)
    comfort_low = float(config.get("comfort_low_pct") or 30)
    min_hold = float(config.get("min_hold_s") or 30)
    buffer_w = float(config.get("surplus_buffer_w") or 100)
    safety_pp = float(config.get("safety_margin_pp") or 5)

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
    if current_soc_pct is None or solar_w is None or load_w is None:
        return _plan("off", "missing telemetry (soc/solar/load)")
    if predicted_sunrise_soc_with_diversion is None:
        return _plan("off", "forecast unavailable")

    surplus_w = solar_w - load_w
    safe_sunrise_floor = target_sunrise_soc_pct + safety_pp

    # Hard floor: SOC drops below comfort_low → always OFF, regardless
    # of everything else. Protects against forecaster error.
    if current_soc_pct <= comfort_low:
        return _plan("off",
                     f"SOC {current_soc_pct:.0f}% ≤ comfort_low {comfort_low:.0f}%")

    # The plug's effective ON/OFF intent based on the current input vector.
    # Notice the symmetry: ON gate is `surplus > car_load + buffer`, OFF
    # gate is `surplus < car_load - buffer`. The dead zone in between
    # leaves whatever state we were in.
    want_on_by_inputs = (
        current_soc_pct >= comfort_high
        and surplus_w >= car_load + buffer_w
        and predicted_sunrise_soc_with_diversion >= safe_sunrise_floor
    )
    want_off_by_inputs = (
        surplus_w < car_load - buffer_w
        or predicted_sunrise_soc_with_diversion < safe_sunrise_floor
    )

    # Pick action.
    if want_on_by_inputs:
        return _plan(
            "on",
            f"surplus {surplus_w:.0f}W ≥ car {car_load:.0f}W+buf{buffer_w:.0f} "
            f"and predicted sunrise {predicted_sunrise_soc_with_diversion:.0f}% "
            f"≥ target {target_sunrise_soc_pct:.0f}%+margin{safety_pp:.0f}",
        )
    if want_off_by_inputs:
        # Why are we turning off / staying off?
        if surplus_w < car_load - buffer_w:
            why = (f"surplus {surplus_w:.0f}W < car {car_load:.0f}W-buf"
                   f"{buffer_w:.0f}; net draining battery")
        else:
            why = (f"predicted sunrise {predicted_sunrise_soc_with_diversion:.0f}% "
                   f"< target {target_sunrise_soc_pct:.0f}%+margin{safety_pp:.0f}; "
                   f"diversion would risk overnight floor")
        return _plan("off", why)

    # Dead zone: keep whatever state we were in. Caller decides the
    # "skip" vs explicit echo based on plug_state_before (we don't
    # know it here without tighter coupling).
    return _plan(
        "skip",
        f"in hysteresis dead zone (surplus {surplus_w:.0f}W vs car "
        f"{car_load:.0f}W±buf{buffer_w:.0f}); maintaining current plug state",
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
