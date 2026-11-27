"""Closed-loop check that a commanded smart-charge ON produced AC charging.

Smart charge actuates a plain Kasa smart plug (EP10: no emeter) on the
Jackery's grid input. A plug that acknowledged ``turn_on`` proves nothing
about the battery: the plug relay, the outlet/breaker, the cable, or the
Jackery's own AC charger can each leave AC input at 0 W. The only
independent evidence is the Jackery's reported AC input (``acip``), stored
per one-minute bucket in ``samples.ac_input_wh`` / ``last_ac_input_w``.

Observed start latency on this rig is 0-60 s (Oct-Nov 2026: 80 of 85
commanded intervals showed AC input in the first or second bucket), so a
three-minute grace period is long enough to avoid false alarms and short
enough to catch the failure on the next five-minute controller tick.

The exception is weak light at dawn and dusk. With the panels producing a
trickle the Jackery often holds AC charging off until solar picks up or
stops: of 132 commanded runs to 2026-11-27, 123 started within 75 s, and six
of the nine slow or failed ones began with 2-30 W of solar (delays of 17, 26,
34 and 71 minutes). Nothing is broken in that state, so the verdict stays
"pending" while the panels are in that band, for up to WEAK_SOLAR_GRACE_S.

Pure functions only; callers fetch rows and decide what to do with the
verdict.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

GRACE_S = 180            # allow the charger to negotiate after plug ON
RECENT_WINDOW_S = 600    # "still charging?" looks at the last 10 minutes
MIN_AC_W = 100           # below this the charger is not meaningfully running
MAX_TELEMETRY_AGE_S = 180
FULL_SOC_PCT = 99.0      # a full battery legitimately stops drawing AC
WEAK_SOLAR_MIN_W = 1     # panels awake ...
WEAK_SOLAR_MAX_W = 150   # ... but not yet producing in earnest
WEAK_SOLAR_GRACE_S = 90 * 60


def commanded_on_since(decisions: Iterable[Mapping]) -> float | None:
    """Start of the current, still-open run of executed active ON commands.

    ``decisions`` are smart_charge_decisions rows (any order). Only executed
    active on/off rows count, mirroring ``smart_charge.commanded_on_seconds``.
    Returns None when the latest executed command is OFF (or there is none).
    """
    since: float | None = None
    rows = sorted(decisions, key=lambda row: float(row["decided_at"]))
    for row in rows:
        if row.get("mode") != "active" or not row.get("executed"):
            continue
        action = row.get("action")
        if action == "on":
            if since is None:
                since = float(row["decided_at"])
        elif action == "off":
            since = None
    return since


def _bucket_ac_w(row: Mapping) -> float:
    """Best-evidence AC input watts for one 1-minute samples bucket."""
    wh = float(row.get("ac_input_wh") or 0)
    last = float(row.get("last_ac_input_w") or 0)
    # 1-minute bucket: Wh * 60 = mean W. Partial buckets under-read, so
    # also accept the last instantaneous reading.
    return max(wh * 60.0, last)


def assess(*, on_since: float | None, now: float, buckets: Iterable[Mapping],
           soc_pct: float | None = None, grace_s: float = GRACE_S,
           recent_window_s: float = RECENT_WINDOW_S, min_ac_w: float = MIN_AC_W,
           max_telemetry_age_s: float = MAX_TELEMETRY_AGE_S) -> dict:
    """Classify whether commanded charging is physically happening.

    ``buckets`` are samples rows for the device covering at least
    ``[on_since, now]`` with keys bucket, ac_input_wh, last_ac_input_w,
    last_source_ts.

    status:
      idle                   no open ON command
      pending                ON for less than ``grace_s``, or the panels are
                             in weak light (``waiting_on`` = "weak_sunlight")
      charging               AC input >= min_ac_w within the recent window
      full                   no AC input but SOC is at/above FULL_SOC_PCT
      telemetry_unavailable  cannot judge: no fresh device telemetry
      not_charging           ON past grace, fresh telemetry, AC input ~0 W
    """
    result = {
        "status": "idle", "on_since": on_since, "checked_at": now,
        "on_for_s": None, "last_ac_ts": None, "max_recent_ac_w": None,
        "newest_source_ts": None, "minutes_without_ac": None, "waiting_on": None,
    }
    if on_since is None:
        return result
    on_for = max(0.0, now - on_since)
    result["on_for_s"] = round(on_for)
    rows = [row for row in buckets if float(row["bucket"]) + 60 > on_since]
    newest_source = max((float(row.get("last_source_ts") or 0) for row in rows), default=0.0)
    result["newest_source_ts"] = newest_source or None
    last_ac_ts = None
    for row in rows:
        if _bucket_ac_w(row) >= min_ac_w:
            stamp = float(row["bucket"])
            last_ac_ts = stamp if last_ac_ts is None else max(last_ac_ts, stamp)
    result["last_ac_ts"] = last_ac_ts
    window_start = max(on_since, now - recent_window_s)
    recent = [_bucket_ac_w(row) for row in rows if float(row["bucket"]) + 60 > window_start]
    result["max_recent_ac_w"] = round(max(recent, default=0.0), 1)
    if last_ac_ts is not None and last_ac_ts + 60 > window_start:
        result["status"] = "charging"
        return result
    if on_for < grace_s:
        result["status"] = "pending"
        return result
    if not newest_source or now - newest_source > max_telemetry_age_s:
        result["status"] = "telemetry_unavailable"
        return result
    if soc_pct is not None and soc_pct >= FULL_SOC_PCT:
        result["status"] = "full"
        return result
    recent_solar = max((float(row.get("last_solar_w") or 0) for row in rows
                        if float(row["bucket"]) + 60 > window_start), default=0.0)
    if WEAK_SOLAR_MIN_W <= recent_solar <= WEAK_SOLAR_MAX_W and on_for < WEAK_SOLAR_GRACE_S:
        result.update(status="pending", waiting_on="weak_sunlight")
        return result
    reference = last_ac_ts + 60 if last_ac_ts is not None else on_since
    result["minutes_without_ac"] = round(max(0.0, now - reference) / 60, 1)
    result["status"] = "not_charging"
    return result


def warning_text(verdict: Mapping, plug_is_on: bool | None) -> str | None:
    """User-facing warning for a not_charging verdict (None otherwise)."""
    if verdict.get("status") != "not_charging":
        return None
    minutes = verdict.get("minutes_without_ac")
    if plug_is_on is False:
        cause = ("the charging plug reports OFF, so it did not switch or something "
                 "else turned it off (LG interlock, rescue/automation rule, manual)")
    elif plug_is_on is True:
        cause = ("the charging plug reports ON, so the outlet/breaker, cable or the "
                 "Jackery AC charger is not accepting power")
    else:
        cause = "the charging plug state could not be read"
    return (f"Charging commanded ON but the Jackery reports 0 W AC input for "
            f"{minutes:.0f} min: {cause}. Check the grid-charging path.")
