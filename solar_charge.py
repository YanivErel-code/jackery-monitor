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
# Shared file used to coordinate the inverter-overload trip between
# bridge (writer; fires from the MQTT push handler when output_power_w
# exceeds threshold) and server (reader; gates re-engagement in the
# 30s eval loop). Lives in /data so both containers see the same view.
OVERLOAD_STATE_PATH = os.environ.get(
    "JACKERY_SOLAR_CHARGE_OVERLOAD_FILE",
    "/data/solar_charge_overload.json",
)

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
    # No-load detection: after the controller toggles the plug ON, wait
    # `no_load_verify_delay_s` then compare Jackery output_power_w to
    # the pre-toggle baseline. If delta < `no_load_threshold_w`, we
    # conclude nothing is plugged into the outlet (car not connected,
    # EVSE idle, etc.) — force the plug OFF and set a cooldown of
    # `no_load_cooldown_s` before trying again. Defaults: verify after
    # 90s (one cloud-poll cycle), threshold 500W (>~third of car_load
    # = unambiguously something drawing), cooldown 15min.
    "no_load_verify_delay_s": 90,
    "no_load_threshold_w": 500,
    "no_load_cooldown_s": 900,
    # Inverter overload protection. If the Jackery's instantaneous
    # output_power_w climbs to/above `inverter_protect_load_w`, the
    # bridge fires the diversion plug OFF immediately (push-driven,
    # sub-second) and stamps an overload timestamp in a shared file
    # (/data/solar_charge_overload.json). The eval loop then refuses
    # to re-engage until `inverter_protect_cooldown_s` has elapsed
    # since the LAST overload sample — so the inverter gets a clean
    # 30 min below threshold before the load returns. Default 2100W
    # is a conservative soft cap well below the 5000+'s 5000W
    # continuous rating; the Jackery has its own hardware-level
    # protection at higher thresholds. Tune up if you have heavier
    # baseline house loads you don't want false-tripping on.
    "inverter_protect_load_w": 2100,
    "inverter_protect_cooldown_s": 1800,
    # Pre-engage load ceiling. Refuse to flip the plug OFF→ON when
    # the current system load_w is at or above this value. Prevents
    # the common foot-gun of engaging diversion right as a heavy
    # appliance kicks in (microwave, dryer, EV onboard charger) and
    # immediately tripping inverter-protect: 800W house + 1400W car
    # = 2200W which is over the 2100W default trip. The gate is a
    # no-op when the plug is already ON (load_w then includes the
    # diversion itself, so blocking would be wrong). Default 800W
    # leaves comfortable headroom under the 2100W trip threshold.
    "max_system_load_w": 800,
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

# If the device's AC input (acip) is above this, treat it as actively
# grid-charging and force the diversion plug OFF. The 5000+ shares a
# single input/output power budget during AC pass-through charging:
# grid feeds the battery AND the AC output simultaneously, so adding
# a heavy diversion load (1.4kW EV charger) on top of grid charging
# (~800W) on top of normal house load (~300W) can exceed the wall
# circuit's 15A budget (~1800W @ 120V) or trip the inverter's thermal
# protection. Yielding to grid charging is the right invariant — the
# user's smart-charge / automation rules use AC to refill the battery
# specifically because the battery is low, and that needs to happen
# unimpeded. Set at 50W (well above sensor noise / idle adapter draw,
# well below any real grid-charging rate the unit produces).
AC_INPUT_GRID_CHARGE_THRESHOLD_W = 50.0

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
    _int_in_range("no_load_verify_delay_s", 30, 600)
    _int_in_range("no_load_threshold_w", 50, 5000)
    _int_in_range("no_load_cooldown_s", 60, 7200)
    _int_in_range("inverter_protect_load_w", 500, 4500)
    _int_in_range("inverter_protect_cooldown_s", 60, 86400)
    _int_in_range("max_system_load_w", 100, 4500)
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
    # No-load verification state. When the controller toggles the plug
    # ON, it records the Jackery's output_power_w right before the
    # toggle (verify_pre_output_w) and the timestamp by which the load
    # MUST have shown up to count as "car is plugged in"
    # (verify_deadline_ts). After the deadline, the next tick checks
    # whether current output_w jumped enough vs the pre-toggle value;
    # if not, the plug is forced OFF and no_load_cooldown_until is set
    # to prevent immediate re-trigger. Cleared on voluntary OFF.
    verify_pre_output_w: float | None = None
    verify_deadline_ts: float = 0.0
    no_load_cooldown_until: float = 0.0
    # Live plug-reported AC draw (from kasa_client.status). Cached here
    # so the 2-second telemetry tick has a fresh-enough reading without
    # hitting the plug each time. Refreshed by the 30s solar_charge
    # evaluate loop while plug_is_on. None on plugs without emeter
    # (older HS103/EP10-class) — diverted_w then falls back to the
    # learned-load delta estimator below.
    plug_power_w: float | None = None
    plug_power_ts: float = 0.0
    # Learned downstream-load size (W) for non-emeter plugs. Captured at
    # the moment the no-load verification PASSES: `output_w − baseline`
    # at that instant is the size of the load that just appeared, which
    # IS the plug's true draw on this hardware. Used by
    # `_solar_charge_current_diverted_w` to (a) report a constant
    # diverted_w during the session instead of a noisy delta and
    # (b) detect mid-session disconnects by watching for the delta to
    # collapse below half of learned_load_w (the car unplugged).
    learned_load_w: float | None = None


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
    # Cloudy-tomorrow guard input: baseline forecast's minimum predicted
    # SOC over the guard horizon. Surfaced for audit/backtest parity with
    # the other gating inputs (not persisted to the decisions table).
    predicted_min_soc_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_plan(
    *,
    config: dict[str, Any],
    current_soc_pct: float | None,
    solar_w: float | None,
    load_w: float | None,
    ac_input_w: float | None = None,
    telemetry_age_s: float,
    target_sunrise_soc_pct: float,
    predicted_sunrise_soc_with_diversion: float | None,
    predicted_sunrise_soc_baseline: float | None,
    capacity_wh: float | None = None,
    hours_to_sunrise: float | None = None,
    household_load_w: float | None = None,
    predicted_min_soc_pct: float | None = None,
    guard_horizon_h: float = 36.0,
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
        capacity_wh, hours_to_sunrise, household_load_w: inputs to the
            model-free overnight-reserve guard (see below). All three
            must be provided for the guard to run; otherwise it's a
            no-op (back-compat). `household_load_w` is the live load with
            the EV charger EXCLUDED — i.e. what the battery would drain at
            if the plug were OFF right now.
        predicted_min_soc_pct: the BASELINE (no-diversion) forecast's
            MINIMUM predicted SOC over `guard_horizon_h` (through the
            NEXT day's recharge, not just sunrise). Gates the
            cloudy-tomorrow guard: the sunrise target alone approved an
            evening car charge on 2026-07-12 assuming a sunny refill, and
            the overcast 07-13 then dropped the pack to 20% by midday.
            MUST come from the baseline trajectory — the with-diversion
            simulator rides its trough down to its allocation floor
            (target+margin+hyst) by design, which would false-block sunny
            days. None → guard is a no-op (back-compat).
        guard_horizon_h: hours the cloudy-tomorrow guard looks ahead;
            audit/reason strings quote it, so keep it in sync with the
            window the caller used to compute predicted_min_soc_pct.
        now_ts: clock injection for tests; defaults to time.time().
    """
    now = float(now_ts if now_ts is not None else time.time())
    mode = str(config.get("mode") or "off").lower()
    car_load = float(config.get("car_load_w") or 1400)
    comfort_low = float(config.get("comfort_low_pct") or 30)
    # comfort_high: minimum SOC to START a new charge session. Asymmetric
    # with comfort_low so the controller doesn't immediately resume
    # charging right after the hard floor catches it (which would put
    # us at the edge of the floor whenever any forecast wobble fires).
    # Once ON, the OFF gate is comfort_low (NOT comfort_high) — so
    # in the band between [comfort_low, comfort_high], an already-ON
    # session keeps going, but an already-OFF state holds off until
    # SOC recovers past comfort_high.
    comfort_high = float(config.get("comfort_high_pct") or 50)
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
            predicted_min_soc_pct=predicted_min_soc_pct,
        )

    if mode == "off":
        return _plan("skip", "mode=off")

    # Fail-closed checks: any of these → OFF.
    if telemetry_age_s > MAX_TELEMETRY_AGE_S:
        return _plan("off", f"stale telemetry ({telemetry_age_s:.0f}s old)")
    if current_soc_pct is None:
        return _plan("off", "missing telemetry (soc)")

    # Hard yield to grid charging. Comes BEFORE the forecast check so
    # it wins even when the projection says we have headroom — the
    # invariant is "never compete with AC input for the inverter's
    # power budget." Universal signal: covers smart_charge automation,
    # battery-low automation rules, manual grid plug-in, etc. The 50W
    # threshold sits above sensor noise but well below any actual
    # grid-charging rate, so it triggers reliably the moment AC input
    # starts but doesn't false-fire on idle adapter draw.
    if ac_input_w is not None and ac_input_w > AC_INPUT_GRID_CHARGE_THRESHOLD_W:
        return _plan(
            "off",
            f"grid charging active ({ac_input_w:.0f}W AC input ≥ "
            f"{AC_INPUT_GRID_CHARGE_THRESHOLD_W:.0f}W); yielding to avoid "
            f"inverter overdraw",
        )

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

    # Overnight-reserve guard (model-free, forecast-independent). The
    # gating forecast above can be over-optimistic about overnight drain
    # (on 2026-06-18 it projected 26% sunrise while SOC actually bottomed
    # at 17%), and we deliberately keep the car load OUT of that forecast
    # (modeling it as demand would stop diversion from ever starting —
    # the circularity the user flagged). So independently verify against
    # LIVE telemetry: when there is no solar surplus (diversion comes
    # straight out of the battery, not the panels), check that stopping
    # NOW and coasting on HOUSEHOLD load alone to sunrise still clears
    # target+margin. The car is excluded from this projection, so it is
    # not circular — it answers "if I stop the car now, does the house
    # alone still make sunrise?" If not, we'd be diverting the overnight
    # reserve into the car → force OFF.
    #
    # Skipped entirely when solar covers the load (daytime surplus
    # diversion is self-funding) or when any input is missing (the live
    # telemetry path always supplies them; tests/back-compat callers may
    # not, and then this is a no-op).
    if (capacity_wh and capacity_wh > 0 and hours_to_sunrise
            and household_load_w is not None and solar_w is not None
            and solar_w <= household_load_w):
        coast_drain_pp = (household_load_w * float(hours_to_sunrise)
                          / float(capacity_wh) * 100.0)
        reserve_at_sunrise = current_soc_pct - coast_drain_pp
        if reserve_at_sunrise < safe_sunrise_floor:
            return _plan(
                "off",
                f"overnight-reserve guard: no solar surplus and coasting "
                f"{household_load_w:.0f}W house load for {float(hours_to_sunrise):.1f}h "
                f"would reach {reserve_at_sunrise:.0f}% by sunrise < "
                f"target{target_sunrise_soc_pct:.0f}%+margin = {safe_sunrise_floor:.0f}%; "
                f"diverting would eat the overnight reserve",
            )

    # Cloudy-tomorrow guard. The sunrise gate only protects the trough
    # BEFORE the next solar ramp — it approved the 2026-07-12 evening car
    # charge (sunrise landed at 36%, above target) and then the overcast
    # 07-13 kept draining the pack to 20% by midday because the refill
    # never came. Guard on the BASELINE forecast's MINIMUM over the
    # extended horizon (through the next day's recharge): if the natural
    # household trajectory dips below comfort_low, diverting now spends
    # reserve the cloudy tomorrow needs. Same hysteresis pattern as the
    # sunrise gate: hard OFF below comfort_low, a skip band above it so
    # an in-flight session isn't flapped by forecast wobble.
    if predicted_min_soc_pct is not None:
        min_on_threshold = comfort_low + on_hysteresis_pp
        if predicted_min_soc_pct < comfort_low:
            return _plan(
                "off",
                f"cloudy-tomorrow guard: predicted {predicted_min_soc_pct:.1f}% "
                f"trough within {guard_horizon_h:.0f}h < comfort_low "
                f"{comfort_low:.0f}%; reserving for the next day's recharge",
            )
        if predicted_min_soc_pct < min_on_threshold:
            return _plan(
                "skip",
                f"in cloudy-tomorrow hysteresis band: predicted "
                f"{predicted_min_soc_pct:.1f}% trough within "
                f"{guard_horizon_h:.0f}h in "
                f"[{comfort_low:.0f}%, {min_on_threshold:.0f}%); holding state",
            )

    # Forecast-driven decision, gated by asymmetric SOC bands:
    #   - START (OFF → ON) requires SOC >= comfort_high.  Without this,
    #     immediately after the hard floor (comfort_low) catches a runaway
    #     drain, the controller would resume at SOC ≈ comfort_low + ε,
    #     putting the battery at the edge of the floor on every tick.
    #     Requiring SOC to recover to comfort_high before re-engaging
    #     gives the battery a meaningful buffer between "controller
    #     wakes back up" and "controller's last-resort hard stop."
    #   - CONTINUE (already ON) only needs forecast OK; the hard floor
    #     is the stopper.  So in the band [comfort_low, comfort_high]:
    #       - if currently ON → keep going (skip = hold state)
    #       - if currently OFF → wait for SOC to climb past comfort_high
    #     gate_min_hold (which sees plug_state_before) resolves the
    #     skip into the correct hold.
    #
    # Forecast gate (within the SOC bands):
    #   - ON when projected sunrise is comfortably above floor (i.e.
    #     above the safety floor + a hysteresis band). Hysteresis
    #     prevents toggling around the boundary as the forecast
    #     wobbles tick-to-tick.
    #   - OFF when projected sunrise drops to or below the safety floor.
    pred = predicted_sunrise_soc_with_diversion
    if pred < safe_sunrise_floor:
        return _plan(
            "off",
            f"predicted sunrise {pred:.1f}% < target {target_sunrise_soc_pct:.0f}%"
            f"+margin{safety_pp:.0f} = {safe_sunrise_floor:.0f}%; diversion would "
            f"risk overnight floor",
        )
    if pred >= on_threshold and current_soc_pct >= comfort_high:
        return _plan(
            "on",
            f"predicted sunrise {pred:.1f}% ≥ target {target_sunrise_soc_pct:.0f}%"
            f"+margin{safety_pp:.0f}+hyst{on_hysteresis_pp:.0f} = {on_threshold:.0f}% "
            f"AND SOC {current_soc_pct:.0f}% ≥ comfort_high {comfort_high:.0f}%; "
            f"battery has headroom to divert",
        )
    # In one of the hold bands — skip means "maintain current plug state."
    # gate_min_hold downgrades a flip to skip when the desired state
    # already matches the current plug state, so this is the safe default.
    if current_soc_pct < comfort_high:
        return _plan(
            "skip",
            f"SOC {current_soc_pct:.0f}% < comfort_high {comfort_high:.0f}%; "
            f"waiting for battery to recover before starting a new session "
            f"(if already on, holding through forecast band)",
        )
    return _plan(
        "skip",
        f"in forecast hysteresis band: predicted sunrise {pred:.1f}% in "
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


# ---------- Inverter overload protection (shared between bridge + server) ----------
_overload_lock = threading.Lock()


def read_overload_state() -> dict[str, dict[str, Any]]:
    """Return the per-device overload-state map.

    Shape: {device_sn: {"last_overload_ts": float, "load_w": float}}.
    Missing file or unreadable contents → empty dict (fail-open is fine;
    the gate is a safety belt, not a primary control loop). Atomic via
    POSIX rename in the writer."""
    with _overload_lock:
        try:
            with open(OVERLOAD_STATE_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("solar_charge overload state unreadable (%s); "
                        "treating as empty", e)
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sn, entry in data.items():
        if isinstance(entry, dict):
            out[str(sn)] = entry
    return out


def stamp_overload(device_sn: str, load_w: float,
                   now_ts: float | None = None) -> None:
    """Record an inverter-overload event for `device_sn`. Called from the
    bridge's MQTT push handler the instant output_power_w crosses the
    threshold. The server's eval loop reads this to gate re-engagement."""
    ts = float(now_ts if now_ts is not None else time.time())
    with _overload_lock:
        try:
            with open(OVERLOAD_STATE_PATH) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except Exception as e:
            log.warning("solar_charge overload state unreadable on stamp "
                        "(%s); overwriting", e)
            data = {}
        data[str(device_sn)] = {"last_overload_ts": ts, "load_w": float(load_w)}
        os.makedirs(os.path.dirname(OVERLOAD_STATE_PATH) or ".", exist_ok=True)
        tmp = OVERLOAD_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, OVERLOAD_STATE_PATH)


def clear_overload_state() -> None:
    """Wipe the overload-state file. Called at server startup (hydrate)
    to enforce the 'restart resets cooldown' invariant the user picked.
    Best-effort: missing file is fine."""
    with _overload_lock:
        try:
            os.unlink(OVERLOAD_STATE_PATH)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("solar_charge clear_overload_state failed (%s)", e)


def gate_load_ceiling(plan: Plan, load_w: float | None,
                      plug_state_before: str,
                      max_system_load_w: float) -> Plan:
    """Refuse OFF→ON plug flips when current system load is already
    at/above the ceiling. Prevents the controller from engaging right
    as a heavy appliance fires and immediately tripping inverter-protect.

    No-op when:
      - plan.action != "on" (nothing to gate)
      - plug_state_before == "on" (already engaged; load_w includes the
        diversion's own draw, blocking would be wrong)
      - load_w is None (no fresh telemetry; let other gates handle)
      - load_w < max_system_load_w (room to engage safely)

    Pure function. Caller passes the threshold from config so this
    stays test-friendly without reading global state."""
    if plan.action != "on":
        return plan
    if plug_state_before == "on":
        return plan
    if load_w is None:
        return plan
    if load_w < max_system_load_w:
        return plan
    car_w = plan.car_load_w if plan.car_load_w is not None else 0.0
    plan.action = "skip"
    plan.reason = (
        f"system load {load_w:.0f}W ≥ ceiling {max_system_load_w:.0f}W — "
        f"engaging diversion (~{car_w:.0f}W) would risk pushing total "
        f"toward the inverter-protect threshold; deferring: {plan.reason}"
    )
    return plan


def gate_inverter_protect(plan: Plan, last_overload_ts: float,
                          cooldown_s: float,
                          now_ts: float | None = None) -> Plan:
    """Refuse `on` actions while the inverter-protect cooldown is active.

    The bridge fires the kasa OFF directly on the MQTT push tick that
    detected the overload; this gate's job is to prevent the eval loop
    from turning it back on for `cooldown_s` after the LAST overload
    sample. Pass `off`/`skip` plans through unchanged — we never want
    to OVERRIDE a controller decision to stay off.

    Pure function. last_overload_ts=0 (file missing or never stamped)
    is a no-op pass-through."""
    if plan.action != "on":
        return plan
    if not last_overload_ts:
        return plan
    now = float(now_ts if now_ts is not None else time.time())
    elapsed = now - float(last_overload_ts)
    if elapsed < cooldown_s:
        remaining = cooldown_s - elapsed
        plan.action = "skip"
        plan.reason = (
            f"inverter-protect cooldown active "
            f"({remaining/60:.0f} min remaining of {cooldown_s/60:.0f} min) "
            f"— load exceeded threshold {elapsed/60:.0f} min ago; "
            f"holding plug OFF: {plan.reason}"
        )
    return plan
