"""
Energy aggregator: integrates instantaneous input/output power readings into
energy (Wh) per device, persists them in SQLite, and serves time-bucketed
history + lifetime totals.

Design:
  - Each call to record(device_sn, ts, input_w, output_w) computes Wh accrued
    since the previous reading for that device (trapezoidal integration), and
    UPSERTs into a per-minute bucket. Idle/restart gaps > 10 minutes are
    treated as zero-power gaps so they don't inflate totals.
  - `samples` table: one row per (device_sn, bucket_minute) with summed Wh in/out
    and last seen instantaneous values. Compact and cheap to query.
  - `devices` table: friendly metadata for the UI.
  - All queries are read-only & wrapped in short transactions; safe to call
    concurrently with the aggregator.

Storage path is configurable via JACKERY_DB env var.
"""
from __future__ import annotations

import bisect
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict

from automation_tables import AutomationTablesMixin
from forecast_tables import ForecastTablesMixin


class EnergyWindow(TypedDict):
    """Energy totals over a single window. `since` is the unix timestamp
    the window starts at (omitted on lifetime since lifetime has no
    start)."""
    input_wh: float
    output_wh: float
    solar_wh: float


class EnergyWindowSince(EnergyWindow):
    since: int


class DeviceTotals(TypedDict):
    """Shape returned by EnergyDB.totals(device_sn). Lifetime + three
    rolling windows. The server may decorate this with `today_savings`,
    `lifetime_savings`, and `cost_plan` before sending it to the UI —
    those are added in server._decorate_totals_with_savings."""
    device_sn: str
    lifetime: EnergyWindow
    today: EnergyWindowSince
    last_7d: EnergyWindowSince
    last_30d: EnergyWindowSince

log = logging.getLogger("energy_db")

# Default DB location: env > Docker /data > workspace
DEFAULT_DB = (
    os.environ.get("JACKERY_DB")
    or ("/data/energy.db" if Path("/data").is_dir() else None)
    or str(Path(__file__).parent / "energy.db")
)

# If two readings are >10 min apart, assume the device was off in between
# (don't extrapolate; just record the new sample as a fresh starting point).
MAX_GAP_S = 600

# Aggregation bucket size in seconds (60 = per-minute)
BUCKET_S = 60


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_sn   TEXT PRIMARY KEY,
    name        TEXT,
    model_code  INTEGER,
    model_name  TEXT,
    first_seen  INTEGER,
    last_seen   INTEGER,
    capacity_wh_override INTEGER  -- user-set total capacity (e.g. with extension batteries); NULL = use model default
);

CREATE TABLE IF NOT EXISTS samples (
    device_sn   TEXT NOT NULL,
    bucket      INTEGER NOT NULL,    -- unix epoch (seconds), floored to BUCKET_S
    input_wh    REAL NOT NULL DEFAULT 0,
    output_wh   REAL NOT NULL DEFAULT 0,
    solar_wh    REAL NOT NULL DEFAULT 0,
    ac_input_wh REAL NOT NULL DEFAULT 0,
    last_input_w   INTEGER,
    last_output_w  INTEGER,
    last_solar_w   INTEGER,
    last_ac_input_w INTEGER,
    last_battery_pct INTEGER,
    sample_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_sn, bucket)
);

CREATE INDEX IF NOT EXISTS idx_samples_bucket ON samples(bucket);
CREATE INDEX IF NOT EXISTS idx_samples_dev_bucket ON samples(device_sn, bucket);

CREATE TABLE IF NOT EXISTS forecast_predictions (
    device_sn     TEXT NOT NULL,
    made_at       INTEGER NOT NULL,   -- hour-aligned unix epoch (when made)
    target        INTEGER NOT NULL,   -- hour-aligned unix epoch (when about)
    predicted_soc REAL NOT NULL,
    PRIMARY KEY (device_sn, made_at, target)
);
CREATE INDEX IF NOT EXISTS idx_pred_target ON forecast_predictions(device_sn, target);

-- Smart-charge controller decision log. Every periodic tick (5 min) writes
-- one row capturing what the controller decided AND why. Used to audit
-- behavior over time and to compute predicted-vs-actual analytics (cf.
-- forecast_predictions joined to samples).
CREATE TABLE IF NOT EXISTS smart_charge_decisions (
    decided_at    INTEGER NOT NULL,
    device_sn     TEXT NOT NULL,
    mode          TEXT NOT NULL,       -- off | test | active
    action        TEXT NOT NULL,       -- on | off | skip
    executed      INTEGER NOT NULL DEFAULT 0,  -- 0 = test/skipped, 1 = Kasa actually toggled
    reason        TEXT,
    current_soc_pct           REAL,
    predicted_sunrise_soc_pct REAL,
    target_sunrise_soc_pct    REAL,
    deficit_kwh               REAL,
    window_start              INTEGER,
    window_end                INTEGER,
    sunrise_ts                INTEGER,
    cheapest_rate             REAL,
    narration                 TEXT,
    baseline_predicted_sunrise_soc_pct REAL,
    PRIMARY KEY (decided_at, device_sn)
);
CREATE INDEX IF NOT EXISTS idx_sc_decided ON smart_charge_decisions(decided_at);
CREATE INDEX IF NOT EXISTS idx_sc_sunrise ON smart_charge_decisions(sunrise_ts);

-- Mirror of smart_charge_decisions for the *solar* charge controller, which
-- toggles a separate Kasa plug to divert surplus solar to an EV/heater/etc.
-- Decision direction is inverted: smart_charge brings grid power IN to the
-- Jackery during cheap TOU windows; solar_charge sends Jackery power OUT to
-- a downstream load when solar is producing more than home demand and the
-- overnight reserve is safe. Schema is intentionally similar so the
-- dashboard analytics can reuse the same query patterns.
CREATE TABLE IF NOT EXISTS solar_charge_decisions (
    decided_at    INTEGER NOT NULL,
    device_sn     TEXT NOT NULL,
    mode          TEXT NOT NULL,       -- off | test | active
    action        TEXT NOT NULL,       -- on | off | skip
    executed      INTEGER NOT NULL DEFAULT 0,
    reason        TEXT,
    current_soc_pct                       REAL,
    -- Predicted sunrise SOC assuming the controller keeps making the same
    -- ON/OFF decisions going forward (the "with diversion" projection).
    predicted_sunrise_soc_pct             REAL,
    -- Counterfactual: predicted sunrise SOC if we leave the plug OFF for
    -- the rest of the day. Used by compute_plan to decide whether
    -- diverting is safe (must satisfy baseline >= target + safety margin).
    baseline_predicted_sunrise_soc_pct    REAL,
    target_sunrise_soc_pct                REAL,
    -- Instantaneous values that drove THIS decision (snapshotted for audit
    -- so a "why did it turn off at 3:47pm" review needs no time-machine).
    solar_w                               REAL,
    load_w                                REAL,
    surplus_w                             REAL,  -- solar_w - load_w
    car_load_w                            REAL,  -- configured assumption
    plug_state_before                     TEXT,  -- "on" | "off"
    PRIMARY KEY (decided_at, device_sn)
);
CREATE INDEX IF NOT EXISTS idx_solar_sc_decided ON solar_charge_decisions(decided_at);

-- Daily denormalized summary: one row per (local-date, device) with the
-- noteworthy moments labelled — sunset SOC + sunrise SOC, predicted vs
-- actual. Filled progressively by the smart-charge tick: predicted values
-- written when the moment is in the future; actual values written once
-- it's in the past and a sample exists. Cheap to query for daily checks
-- and ML-style tuning over time.
-- Hourly weather observations (GHI + cloud cover) from Open-Meteo's
-- past_days response. Persisted so a future offline learning job can
-- correlate weather with our actual solar production WITHOUT re-fetching
-- (Open-Meteo deletes their archive after a while; once we've seen a
-- value we keep it).
CREATE TABLE IF NOT EXISTS weather_observations (
    ts              INTEGER NOT NULL,    -- hour-aligned unix epoch
    ghi_w_m2        REAL NOT NULL,
    cloud_cover_pct REAL,
    PRIMARY KEY (ts)
);

CREATE TABLE IF NOT EXISTS daily_solar_summary (
    date           TEXT NOT NULL,         -- YYYY-MM-DD in user's local TZ
    device_sn      TEXT NOT NULL,
    sunset_ts      INTEGER,
    sunrise_ts     INTEGER,                -- the NEXT sunrise (tomorrow's)
    predicted_sunset_soc_pct  REAL,
    actual_sunset_soc_pct     REAL,
    predicted_sunrise_soc_pct REAL,
    actual_sunrise_soc_pct    REAL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (date, device_sn)
);

CREATE TABLE IF NOT EXISTS battery_packs (
    ts              INTEGER NOT NULL,     -- unix epoch when sampled
    parent_sn       TEXT NOT NULL,        -- main device SN
    pack_sn         TEXT NOT NULL,        -- expansion battery SN
    device_order    INTEGER,              -- position in iOS app (0..N)
    soc_pct         REAL,                 -- rb
    input_w         REAL,                 -- ip
    output_w        REAL,                 -- op
    internal_temp_c REAL,                 -- it
    error_code      INTEGER,              -- ec
    PRIMARY KEY (ts, parent_sn, pack_sn)
);

CREATE INDEX IF NOT EXISTS idx_battery_packs_parent_ts
    ON battery_packs(parent_sn, ts);

CREATE TABLE IF NOT EXISTS algorithm_suggestions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      INTEGER NOT NULL,
    device_sn       TEXT,                  -- NULL = global (forecaster const)
    kind            TEXT NOT NULL,         -- 'config' | 'anomaly'
    target          TEXT,                  -- e.g. 'smart_charge.max_charge_w'
    current_value   TEXT,                  -- JSON-encoded
    proposed_value  TEXT,                  -- JSON-encoded
    reasoning       TEXT,
    confidence      TEXT,                  -- 'high' | 'medium' | 'low'
    severity        TEXT,                  -- 'info' | 'warn' (anomalies only)
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
    decided_at      INTEGER,
    decided_by      TEXT                   -- 'user' | 'auto-expired'
);

CREATE INDEX IF NOT EXISTS idx_alg_sugg_device_status
    ON algorithm_suggestions(device_sn, status, created_at);

CREATE TABLE IF NOT EXISTS algorithm_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id   INTEGER REFERENCES algorithm_suggestions(id),
    applied_at      INTEGER NOT NULL,
    device_sn       TEXT,
    target          TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    reasoning       TEXT
);

CREATE INDEX IF NOT EXISTS idx_alg_changes_device_ts
    ON algorithm_changes(device_sn, applied_at);

-- Automation rule firing history. Append-only — one row per successful
-- edge-trigger firing. Replaces the in-memory `last_fired` overwrite
-- with a real audit log: which rule fired, when, what action, what
-- the SOC was, and which Kasa plug it acted on. Used by the Automation
-- tab's "View history" view + by duration calculations that pair
-- consecutive ON/OFF firings on the same plug.
CREATE TABLE IF NOT EXISTS automation_firings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at        INTEGER NOT NULL,        -- unix epoch
    rule_id         TEXT NOT NULL,
    rule_name       TEXT,                    -- snapshot at fire time so renames don't blank history
    action          TEXT NOT NULL,           -- 'on' | 'off'
    kasa_host       TEXT NOT NULL,           -- target plug (used for duration pairing)
    jackery_sn      TEXT,                    -- which device's SOC drove the firing
    soc_at_fire     REAL,                    -- battery_pct at the moment we fired
    trigger         TEXT,                    -- 'soc' | future expansion
    operator        TEXT,                    -- '<' | '<=' | '=' | '>=' | '>'
    threshold       REAL                     -- the rule's value field
);

CREATE INDEX IF NOT EXISTS idx_automation_firings_rule_ts
    ON automation_firings(rule_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_automation_firings_host_ts
    ON automation_firings(kasa_host, fired_at);

-- Generic per-device parameter store. Keyed by (device_sn, key) with
-- exactly one row per param; the resolution ladder (user override >
-- fitted value > catalog/probe > default) collapses into a single
-- write that records the source. UI / forecaster reads via
-- resolve_device_param(...) so all per-device parameters share the
-- same resolution policy: DB → live fit → catalog/probe → default →
-- ask user. See DEVICE_PARAM_KEYS below for the canonical list.
CREATE TABLE IF NOT EXISTS device_params (
    device_sn   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       REAL,
    source      TEXT NOT NULL,    -- 'user' | 'fit' | 'probe' | 'catalog' | 'default'
    n_samples   INTEGER,          -- fit-source: how many windows; null otherwise
    confidence  TEXT,             -- 'low' | 'medium' | 'high' | NULL
    note        TEXT,             -- optional free-text (e.g. cloud probe key path)
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (device_sn, key)
);
"""

# Canonical list of per-device parameter keys the resolver knows about.
# Adding a new key here gets it into the Device-tab "Learned parameters"
# panel + the AI advisor's per-device context automatically.
DEVICE_PARAM_KEYS: dict[str, dict[str, Any]] = {
    "battery_capacity_wh": {
        "label": "Battery capacity",
        "unit": "Wh",
        "description": "Total system capacity used by the SOC simulator.",
    },
    "max_charge_w": {
        "label": "Max AC charge rate",
        "unit": "W",
        "description": "Peak wattage the device pulls from the wall when smart-charge engages.",
    },
    "inverter_overhead_pct": {
        "label": "Inverter overhead",
        "unit": "ratio",
        "description": "Fraction of throughput lost as heat in DC→AC conversion. 0.10 (10%) is typical for modern LiFePO4 inverters.",
    },
    "parasitic_w": {
        "label": "Parasitic baseline",
        "unit": "W",
        "description": "Constant draw that doesn't scale with AC throughput — BMS, idle inverter, pack-balancing, DC-bus losses. 50W is typical for single-unit setups; multi-pack rigs commonly fit 200-500W.",
    },
    "charge_efficiency": {
        "label": "Charge efficiency",
        "unit": "ratio",
        "description": "Stored Wh per input Wh. 0.85-0.95 is typical.",
    },
    "solar_coefficient": {
        "label": "Solar coefficient",
        "unit": "W per W/m²",
        "description": "Effective panel array size — fitted from observed solar vs irradiance.",
    },
}


class EnergyDB(ForecastTablesMixin, AutomationTablesMixin):
    """Top-level energy DB.

    The bulk of read/write methods are direct members below
    (samples + devices + history + battery_packs + suggestions).
    Two narrowly-scoped table groups are pulled out into mixins to keep
    this file navigable:
      - forecast_tables.ForecastTablesMixin: forecast_predictions table
      - automation_tables.AutomationTablesMixin: smart_charge_decisions
        + automation_firings tables
    Mixin methods are first-class on EnergyDB instances — no caller
    changes when methods move between this class and a mixin."""

    def __init__(self, path: str = DEFAULT_DB) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # per-device last-reading state for trapezoidal integration
        self._last: dict[str, tuple[float, float, float, float, float]] = {}
        # ^ device_sn -> (ts, input_w, output_w, solar_w, ac_input_w)
        self._init()
        log.info("Energy DB at %s", path)

    @contextmanager
    def _conn(self):
        with self._lock:
            con = sqlite3.connect(self.path, timeout=5.0)
            # sqlite3.Row supports BOTH index access (r[0]) and dict-style
            # access (r["device_sn"]), so flipping this on is backwards-
            # compatible with all existing index-based callers and lets
            # new code write `dict(r)` to materialize a row in one step.
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=NORMAL")
                yield con
                con.commit()
            finally:
                con.close()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Backfill columns added after the original schema ship — pre-v0.1.0
            # databases won't have solar_wh / last_solar_w; ALTER TABLE adds them
            # with DEFAULT so existing rows are valid.
            existing = {row[1] for row in c.execute(
                "PRAGMA table_info(samples)").fetchall()}
            if "solar_wh" not in existing:
                c.execute("ALTER TABLE samples ADD COLUMN solar_wh REAL NOT NULL DEFAULT 0")
            if "last_solar_w" not in existing:
                c.execute("ALTER TABLE samples ADD COLUMN last_solar_w INTEGER")
            if "ac_input_wh" not in existing:
                c.execute("ALTER TABLE samples ADD COLUMN ac_input_wh REAL NOT NULL DEFAULT 0")
            if "last_ac_input_w" not in existing:
                c.execute("ALTER TABLE samples ADD COLUMN last_ac_input_w INTEGER")
            # solar_charge_diverted_wh: portion of output_wh in this bucket
            # that was intentionally routed to a downstream load by the
            # solar_charge controller (EV charger, etc.). NOT subtracted
            # from output_wh — output_wh stays raw, this is an annotation.
            # forecaster.fit_load_profile subtracts it before fitting so
            # the learned demand model isn't polluted by intentional
            # diversion. Backfills with 0 on pre-existing buckets, which
            # is correct (no diversion before this feature shipped).
            if "solar_charge_diverted_wh" not in existing:
                c.execute("ALTER TABLE samples ADD COLUMN "
                          "solar_charge_diverted_wh REAL NOT NULL DEFAULT 0")
            # devices table: capacity override (added in v0.2.0+ for users with
            # extension batteries stacked on the 5000 Plus / etc.).
            existing_dev = {row[1] for row in c.execute(
                "PRAGMA table_info(devices)").fetchall()}
            if "capacity_wh_override" not in existing_dev:
                c.execute("ALTER TABLE devices ADD COLUMN capacity_wh_override INTEGER")
            # smart_charge_decisions: baseline_predicted_sunrise_soc_pct added
            # so we can distinguish floor-clamp (predicted == target) from
            # structural pessimism (baseline well under target) on historic
            # rows. Pre-existing rows stay NULL — only new ticks fill it.
            existing_sc = {row[1] for row in c.execute(
                "PRAGMA table_info(smart_charge_decisions)").fetchall()}
            if "baseline_predicted_sunrise_soc_pct" not in existing_sc:
                c.execute(
                    "ALTER TABLE smart_charge_decisions "
                    "ADD COLUMN baseline_predicted_sunrise_soc_pct REAL"
                )
            # daily_solar_summary: predictions_made_at tracks when the row's
            # predicted_* values were last written (NOT bumped by backfill of
            # actual_*). Used by the Forecast tab to filter the headline
            # MAE to predictions made by post-cutoff code, so wild errors
            # from older buggy code don't drag the displayed accuracy down.
            existing_ds = {row[1] for row in c.execute(
                "PRAGMA table_info(daily_solar_summary)").fetchall()}
            if "predictions_made_at" not in existing_ds:
                c.execute(
                    "ALTER TABLE daily_solar_summary "
                    "ADD COLUMN predictions_made_at INTEGER"
                )
                # Backfill existing rows: use sunset_ts as the proxy for
                # when the day's predictions were finalized. By the time
                # sunset arrives, the predicted_sunset_soc_pct has stopped
                # being updated for that day (the next tick targets the
                # next day). Imperfect but better than NULL.
                c.execute(
                    "UPDATE daily_solar_summary "
                    "SET predictions_made_at = COALESCE(sunset_ts, sunrise_ts) "
                    "WHERE predictions_made_at IS NULL"
                )

    # ---------- ingestion ----------
    def upsert_device(self, device_sn: str, name: str | None,
                      model_code: int | None,
                      model_name: str | None) -> None:
        if not device_sn:
            return
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                """INSERT INTO devices (device_sn, name, model_code, model_name,
                                        first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_sn) DO UPDATE SET
                     name = COALESCE(excluded.name, devices.name),
                     model_code = COALESCE(excluded.model_code, devices.model_code),
                     model_name = COALESCE(excluded.model_name, devices.model_name),
                     last_seen = excluded.last_seen
                """,
                (device_sn, name, model_code, model_name, now, now),
            )

    def record(self, device_sn: str, ts: float,
               input_w: float, output_w: float,
               battery_pct: int | None = None,
               solar_w: float = 0.0,
               ac_input_w: float = 0.0,
               solar_charge_diverted_w: float = 0.0) -> None:
        """Integrate (input_w, output_w, solar_w, ac_input_w, diverted_w)
        since last reading for this device.

        `ac_input_w` is the AC/grid charging power (the device's `acip`
        field), tracked separately so cost accounting knows what was
        paid-for vs free.

        `solar_charge_diverted_w` is the instantaneous draw of any
        downstream load currently being driven by the solar_charge
        controller (e.g. EV charger). It's a subset of `output_w`:
        output_w already includes it, this argument annotates how much
        of output is intentional diversion. The trapezoidal integration
        gives the right partial-bucket fraction when the plug toggles
        mid-bucket. forecaster.fit_load_profile subtracts the integrated
        column before learning demand so diversion doesn't pollute the
        load model. Pass 0 (default) when the controller is OFF or the
        plug isn't part of the Jackery output chain."""
        if not device_sn:
            return
        prev = self._last.get(device_sn)
        self._last[device_sn] = (ts, float(input_w), float(output_w),
                                  float(solar_w), float(ac_input_w),
                                  float(solar_charge_diverted_w))
        if prev is None:
            return  # need two samples to integrate
        # Back-compat: pre-feature ticks stored a 5-tuple. Defensively
        # unpack as 6 with a 0 default.
        if len(prev) == 5:
            prev_ts, prev_in, prev_out, prev_solar, prev_ac = prev
            prev_div = 0.0
        else:
            prev_ts, prev_in, prev_out, prev_solar, prev_ac, prev_div = prev
        dt = ts - prev_ts
        if dt <= 0 or dt > MAX_GAP_S:
            return
        # Trapezoidal: avg power times dt, in seconds
        in_wh = ((prev_in + input_w) / 2.0) * (dt / 3600.0)
        out_wh = ((prev_out + output_w) / 2.0) * (dt / 3600.0)
        solar_wh = ((prev_solar + solar_w) / 2.0) * (dt / 3600.0)
        ac_wh = ((prev_ac + ac_input_w) / 2.0) * (dt / 3600.0)
        div_wh = ((prev_div + solar_charge_diverted_w) / 2.0) * (dt / 3600.0)
        # Physical-reality clamp: diverted energy in this interval CAN'T
        # exceed total output energy. If the Jackery delivered 500W
        # during this dt but the solar_charge controller (which doesn't
        # know whether anything's actually plugged in) was reporting
        # 1300W diverted, the smaller value is the truth: you can't
        # divert more than the inverter actually pushed out. Pre-fix,
        # the controller would happily record full car_load_w while
        # the plug was electrically ON but nothing was drawing —
        # leading to "diverted=21Wh, output=7Wh" buckets that
        # over-counted today's diversion by 5+ kWh in a single
        # overnight session.
        div_wh = min(div_wh, out_wh)
        bucket = int(ts // BUCKET_S) * BUCKET_S

        with self._conn() as c:
            c.execute(
                """INSERT INTO samples
                       (device_sn, bucket, input_wh, output_wh, solar_wh, ac_input_wh,
                        solar_charge_diverted_wh,
                        last_input_w, last_output_w, last_solar_w, last_ac_input_w,
                        last_battery_pct, sample_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(device_sn, bucket) DO UPDATE SET
                     input_wh = input_wh + excluded.input_wh,
                     output_wh = output_wh + excluded.output_wh,
                     solar_wh = solar_wh + excluded.solar_wh,
                     ac_input_wh = ac_input_wh + excluded.ac_input_wh,
                     solar_charge_diverted_wh = solar_charge_diverted_wh
                                                + excluded.solar_charge_diverted_wh,
                     last_input_w = excluded.last_input_w,
                     last_output_w = excluded.last_output_w,
                     last_solar_w = excluded.last_solar_w,
                     last_ac_input_w = excluded.last_ac_input_w,
                     last_battery_pct = COALESCE(excluded.last_battery_pct,
                                                 last_battery_pct),
                     sample_count = sample_count + 1
                """,
                (device_sn, bucket, in_wh, out_wh, solar_wh, ac_wh, div_wh,
                 int(input_w), int(output_w), int(solar_w), int(ac_input_w),
                 battery_pct),
            )

    # ---------- queries ----------
    def list_devices(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT device_sn, name, model_code, model_name,
                          first_seen, last_seen, capacity_wh_override
                   FROM devices ORDER BY last_seen DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_capacity_override(self, device_sn: str) -> int | None:
        if not device_sn:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT capacity_wh_override FROM devices WHERE device_sn = ?",
                (device_sn,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def set_capacity_override(self, device_sn: str,
                              capacity_wh: int | None) -> bool:
        """Set or clear the user's manual total-capacity override. Pass None
        to clear (forecast falls back to the model-code default). Sanity
        bounds: 500..200000 Wh — anything outside is rejected."""
        if not device_sn:
            return False
        if capacity_wh is not None:
            try:
                capacity_wh = int(capacity_wh)
            except (TypeError, ValueError):
                return False
            if not 500 <= capacity_wh <= 200_000:
                return False
        with self._conn() as c:
            c.execute(
                "UPDATE devices SET capacity_wh_override = ? WHERE device_sn = ?",
                (capacity_wh, device_sn),
            )
        return True

    def totals(self, device_sn: str) -> DeviceTotals:
        """Lifetime + windowed totals for a single device."""
        now = int(time.time())
        windows = {
            "today": _start_of_day(now),
            "last_7d": now - 7 * 86400,
            "last_30d": now - 30 * 86400,
        }
        with self._conn() as c:
            out: dict[str, Any] = {"device_sn": device_sn}
            # Lifetime
            r = c.execute(
                "SELECT COALESCE(SUM(input_wh),0), COALESCE(SUM(output_wh),0), "
                "COALESCE(SUM(solar_wh),0), "
                "COALESCE(SUM(solar_charge_diverted_wh),0) "
                "FROM samples WHERE device_sn = ?", (device_sn,)
            ).fetchone()
            out["lifetime"] = {"input_wh": r[0], "output_wh": r[1],
                               "solar_wh": r[2],
                               "solar_charge_diverted_wh": r[3]}
            # Windows
            for label, since in windows.items():
                r = c.execute(
                    "SELECT COALESCE(SUM(input_wh),0), COALESCE(SUM(output_wh),0), "
                    "COALESCE(SUM(solar_wh),0), "
                    "COALESCE(SUM(solar_charge_diverted_wh),0) "
                    "FROM samples WHERE device_sn = ? AND bucket >= ?",
                    (device_sn, since),
                ).fetchone()
                out[label] = {"input_wh": r[0], "output_wh": r[1],
                              "solar_wh": r[2],
                              "solar_charge_diverted_wh": r[3],
                              "since": since}
        return out  # type: ignore[return-value]

    def all_totals(self) -> list[dict]:
        return [self.totals(d["device_sn"]) | {"name": d["name"]}
                for d in self.list_devices()]

    def _packs_in_target_range(self, c, device_sn: str,
                               rows: list,
                               main_wh: int | None,
                               pack_wh: int | None,
                               ) -> tuple[dict[int, list[float]], list[int]]:
        """Bulk-fetch every pack snapshot whose ts overlaps any prediction
        target in `rows` (±30min). Returns (packs_by_ts, sorted_ts) for
        bisect-based closest-snapshot lookup. Empty when capacities aren't
        passed or no rows have main SOC available."""
        if not (main_wh and pack_wh and rows):
            return {}, []
        targets = [r[1] for r in rows if r[3] is not None]
        if not targets:
            return {}, []
        t_min = min(targets) - 1800
        t_max = max(targets) + 1800
        pack_rows = c.execute(
            """SELECT ts, soc_pct FROM battery_packs
                WHERE parent_sn = ?
                  AND ts >= ? AND ts < ?
                  AND soc_pct IS NOT NULL""",
            (device_sn, t_min, t_max),
        ).fetchall()
        packs_by_ts: dict[int, list[float]] = {}
        for ts, soc in pack_rows:
            packs_by_ts.setdefault(int(ts), []).append(float(soc))
        return packs_by_ts, sorted(packs_by_ts)

    # ---------- weather observations (GHI + cloud cover history) ----------
    def upsert_weather_observations(self, rows: list[dict]) -> int:
        """Bulk-write weather observations. Each row is {ts, ghi_w_m2,
        cloud_cover_pct}. INSERT OR IGNORE — once we've recorded a value
        for a given hour we don't overwrite (Open-Meteo's historical
        re-analysis can shift a tiny bit and we'd rather have the value
        we saw at observation time)."""
        if not rows:
            return 0
        prepared = [
            (int(r["ts"]),
             float(r.get("ghi_w_m2") or 0),
             float(r["cloud_cover_pct"]) if r.get("cloud_cover_pct") is not None else None)
            for r in rows if r.get("ts")
        ]
        if not prepared:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT OR IGNORE INTO weather_observations
                       (ts, ghi_w_m2, cloud_cover_pct)
                   VALUES (?, ?, ?)""",
                prepared,
            )
        return len(prepared)

    def list_weather_observations(self, since_ts: int = 0,
                                  limit: int = 24 * 30) -> list[dict]:
        """Past hourly weather observations, oldest first. since_ts=0
        returns everything stored."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT ts, ghi_w_m2, cloud_cover_pct
                     FROM weather_observations
                    WHERE ts >= ?
                    ORDER BY ts ASC
                    LIMIT ?""",
                (int(since_ts), int(limit)),
            ).fetchall()
        return [
            {"ts": r[0], "ghi_w_m2": r[1], "cloud_cover_pct": r[2]}
            for r in rows
        ]

    # ---------- per-expansion-battery state ----------
    def record_battery_packs(self, parent_sn: str, packs: list[dict],
                             ts: int | None = None) -> int:
        """Persist a snapshot of every expansion battery for the daily
        learning job. Each pack dict comes from cloud_client.fetch_battery_packs
        (raw cloud field names: rb=SOC, ip=input W, op=output W, it=temp,
        ec=error). Returns count of rows written."""
        if not parent_sn or not packs:
            return 0
        ts = int(ts if ts is not None else time.time())
        prepared: list[tuple] = []
        for p in packs:
            pack_sn = str(p.get("deviceSn") or "").strip()
            if not pack_sn:
                continue
            prepared.append((
                ts, parent_sn, pack_sn,
                int(p.get("deviceOrder") or 0),
                _nullable_float(p.get("rb")),
                _nullable_float(p.get("ip")),
                _nullable_float(p.get("op")),
                _nullable_float(p.get("it")),
                int(p.get("ec") or 0),
            ))
        if not prepared:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT OR REPLACE INTO battery_packs
                       (ts, parent_sn, pack_sn, device_order,
                        soc_pct, input_w, output_w, internal_temp_c, error_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prepared,
            )
        return len(prepared)

    def pack_snapshot_summary(self, parent_sn: str,
                              since_ts: int) -> dict[str, Any]:
        """Cheap health-check on the battery_packs join used to compute
        system_soc. Returns counts since `since_ts`:
          - n_pack_snapshots: number of rows in battery_packs
          - distinct_packs_seen: number of distinct pack_sns
          - distinct_orders_seen: number of distinct device_orders
          - latest_ts: most recent snapshot epoch (None if no rows)
        Used by /api/diagnostics/row_soc to show per-device pack health."""
        if not parent_sn:
            return {"n_pack_snapshots": 0, "distinct_packs_seen": 0,
                    "distinct_orders_seen": 0, "latest_ts": None}
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n,
                          COUNT(DISTINCT pack_sn) AS d_packs,
                          COUNT(DISTINCT device_order) AS d_orders,
                          MAX(ts) AS latest
                     FROM battery_packs
                    WHERE parent_sn = ? AND ts >= ?""",
                (parent_sn, int(since_ts)),
            ).fetchone()
        return {
            "n_pack_snapshots": int(row[0] or 0),
            "distinct_packs_seen": int(row[1] or 0),
            "distinct_orders_seen": int(row[2] or 0),
            "latest_ts": int(row[3]) if row[3] is not None else None,
        }

    def latest_battery_packs(self, parent_sn: str) -> list[dict]:
        """Most recent snapshot of all packs for a given main device. Returns
        rows ordered by device_order so the UI renders them in app order."""
        if not parent_sn:
            return []
        with self._conn() as c:
            latest_ts_row = c.execute(
                "SELECT MAX(ts) FROM battery_packs WHERE parent_sn = ?",
                (parent_sn,),
            ).fetchone()
            if not latest_ts_row or latest_ts_row[0] is None:
                return []
            rows = c.execute(
                """SELECT ts, pack_sn, device_order, soc_pct, input_w,
                          output_w, internal_temp_c, error_code
                     FROM battery_packs
                    WHERE parent_sn = ? AND ts = ?
                    ORDER BY device_order""",
                (parent_sn, int(latest_ts_row[0])),
            ).fetchall()
        return [
            {"ts": r[0], "pack_sn": r[1], "device_order": r[2],
             "soc_pct": r[3], "input_w": r[4], "output_w": r[5],
             "internal_temp_c": r[6], "error_code": r[7]}
            for r in rows
        ]

    # ---------- daily solar summary (sunset/sunrise predicted vs actual) ----------
    def upsert_daily_summary(self, *, device_sn: str, local_date: str,
                             sunset_ts: int | None,
                             sunrise_ts: int | None,
                             predicted_sunset_soc_pct: float | None = None,
                             actual_sunset_soc_pct: float | None = None,
                             predicted_sunrise_soc_pct: float | None = None,
                             actual_sunrise_soc_pct: float | None = None) -> None:
        """Upsert one daily row. Only NON-None values overwrite the existing
        row, so the same call can fill predicted at noon and actual at
        midnight without clobbering anything that's already there."""
        if not device_sn or not local_date:
            return
        now = int(time.time())
        with self._conn() as c:
            existing = c.execute(
                """SELECT sunset_ts, sunrise_ts,
                          predicted_sunset_soc_pct, actual_sunset_soc_pct,
                          predicted_sunrise_soc_pct, actual_sunrise_soc_pct,
                          predictions_made_at
                     FROM daily_solar_summary
                    WHERE date = ? AND device_sn = ?""",
                (local_date, device_sn),
            ).fetchone()
            # Bump predictions_made_at ONLY when at least one predicted_*
            # value is non-None — backfill_daily_actuals passes None for
            # predicted_* and shouldn't make stale rows look fresh.
            writing_predictions = (predicted_sunset_soc_pct is not None
                                   or predicted_sunrise_soc_pct is not None)
            if existing:
                merged = (
                    sunset_ts if sunset_ts is not None else existing[0],
                    sunrise_ts if sunrise_ts is not None else existing[1],
                    predicted_sunset_soc_pct if predicted_sunset_soc_pct is not None else existing[2],
                    actual_sunset_soc_pct if actual_sunset_soc_pct is not None else existing[3],
                    predicted_sunrise_soc_pct if predicted_sunrise_soc_pct is not None else existing[4],
                    actual_sunrise_soc_pct if actual_sunrise_soc_pct is not None else existing[5],
                )
                predictions_made_at = (now if writing_predictions
                                       else existing[6])
                c.execute(
                    """UPDATE daily_solar_summary
                          SET sunset_ts = ?, sunrise_ts = ?,
                              predicted_sunset_soc_pct = ?,
                              actual_sunset_soc_pct = ?,
                              predicted_sunrise_soc_pct = ?,
                              actual_sunrise_soc_pct = ?,
                              updated_at = ?,
                              predictions_made_at = ?
                        WHERE date = ? AND device_sn = ?""",
                    (*merged, now, predictions_made_at,
                     local_date, device_sn),
                )
            else:
                c.execute(
                    """INSERT INTO daily_solar_summary
                           (date, device_sn, sunset_ts, sunrise_ts,
                            predicted_sunset_soc_pct, actual_sunset_soc_pct,
                            predicted_sunrise_soc_pct, actual_sunrise_soc_pct,
                            updated_at, predictions_made_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (local_date, device_sn, sunset_ts, sunrise_ts,
                     predicted_sunset_soc_pct, actual_sunset_soc_pct,
                     predicted_sunrise_soc_pct, actual_sunrise_soc_pct,
                     now, now if writing_predictions else None),
                )

    def backfill_daily_actuals(self, device_sn: str, *,
                               days: int = 14,
                               main_capacity_wh: int | None = None,
                               pack_capacity_wh: int | None = None) -> int:
        """Walk daily_solar_summary rows missing actual_sunset / actual_sunrise
        whose corresponding `*_ts` is in the past, and fill in the actual
        from the samples + battery_packs join.

        The single-shot tick path (`_update_daily_summary`) only ever writes
        to today's row, but the row's `sunrise_ts` typically falls on the
        FOLLOWING calendar day — so today's tick can't back-fill yesterday's
        sunrise. This helper, run from each tick, walks recent rows and
        fills in any actuals that have aged into the past.

        Returns the count of values filled (sunset + sunrise across all
        rows). Idempotent — re-running over already-filled rows is a no-op.
        """
        if not device_sn:
            return 0
        from datetime import datetime, timedelta, timezone
        cutoff_date = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        now = int(time.time())
        with self._conn() as c:
            rows = c.execute(
                """SELECT date, sunset_ts, sunrise_ts,
                          actual_sunset_soc_pct, actual_sunrise_soc_pct
                     FROM daily_solar_summary
                    WHERE device_sn = ?
                      AND date >= ?
                      AND ((actual_sunset_soc_pct IS NULL AND sunset_ts IS NOT NULL
                            AND sunset_ts <= ?)
                        OR (actual_sunrise_soc_pct IS NULL AND sunrise_ts IS NOT NULL
                            AND sunrise_ts <= ?))""",
                (device_sn, cutoff_date, now, now),
            ).fetchall()
        filled = 0
        for date, sunset_ts, sunrise_ts, act_sunset, act_sunrise in rows:
            fill_sunset: float | None = None
            fill_sunrise: float | None = None
            if act_sunset is None and sunset_ts and sunset_ts <= now:
                fill_sunset = self.system_soc_at(
                    device_sn, int(sunset_ts),
                    main_capacity_wh=main_capacity_wh,
                    pack_capacity_wh=pack_capacity_wh,
                )
            if act_sunrise is None and sunrise_ts and sunrise_ts <= now:
                fill_sunrise = self.system_soc_at(
                    device_sn, int(sunrise_ts),
                    main_capacity_wh=main_capacity_wh,
                    pack_capacity_wh=pack_capacity_wh,
                )
            if fill_sunset is None and fill_sunrise is None:
                continue
            self.upsert_daily_summary(
                device_sn=device_sn, local_date=date,
                sunset_ts=None, sunrise_ts=None,
                predicted_sunset_soc_pct=None,
                actual_sunset_soc_pct=fill_sunset,
                predicted_sunrise_soc_pct=None,
                actual_sunrise_soc_pct=fill_sunrise,
            )
            if fill_sunset is not None:
                filled += 1
            if fill_sunrise is not None:
                filled += 1
        return filled

    def list_daily_summary(self, device_sn: str, days: int = 30) -> list[dict]:
        """Most recent N daily rows for a device, newest first.

        Filter is on the row's `date` column (lexicographic on YYYY-MM-DD,
        which sorts correctly), NOT on `updated_at`. Otherwise the
        backfill bumping updated_at on every tick would defeat the
        windowing — all rows would always look 'recent' and the
        Forecast tab's days dropdown would appear stuck."""
        from datetime import datetime, timedelta, timezone
        cutoff_date = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        with self._conn() as c:
            rows = c.execute(
                """SELECT date, sunset_ts, sunrise_ts,
                          predicted_sunset_soc_pct, actual_sunset_soc_pct,
                          predicted_sunrise_soc_pct, actual_sunrise_soc_pct,
                          updated_at, predictions_made_at
                     FROM daily_solar_summary
                    WHERE device_sn = ?
                      AND date >= ?
                    ORDER BY date DESC""",
                (device_sn, cutoff_date),
            ).fetchall()
        return [
            {"date": r[0], "sunset_ts": r[1], "sunrise_ts": r[2],
             "predicted_sunset_soc_pct": r[3], "actual_sunset_soc_pct": r[4],
             "predicted_sunrise_soc_pct": r[5], "actual_sunrise_soc_pct": r[6],
             "sunset_error_pp": (r[4] - r[3])
                                if r[3] is not None and r[4] is not None else None,
             "sunrise_error_pp": (r[6] - r[5])
                                 if r[5] is not None and r[6] is not None else None,
             "updated_at": r[7],
             "predictions_made_at": r[8]}
            for r in rows
        ]

    def actual_soc_at(self, device_sn: str, ts: int,
                      window_s: int = 1800) -> float | None:
        """Main-only average last_battery_pct in the ±window around `ts`.
        Most callers should use `system_soc_at()` instead so the actual
        matches the predicted (which is system-weighted). Kept as a
        primitive: `system_soc_at()` calls it internally."""
        with self._conn() as c:
            row = c.execute(
                """SELECT AVG(last_battery_pct)
                     FROM samples
                    WHERE device_sn = ?
                      AND bucket >= ?
                      AND bucket <  ?
                      AND last_battery_pct IS NOT NULL""",
                (device_sn, ts - window_s, ts + window_s),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def system_soc_at(self, device_sn: str, ts: int, *,
                      main_capacity_wh: int | None = None,
                      pack_capacity_wh: int | None = None,
                      window_s: int = 1800) -> float | None:
        """Capacity-weighted system SOC at `ts` (main + expansion packs).
        Joins `samples.last_battery_pct` with the closest `battery_packs`
        snapshot in the ±window. Returns main-only when capacities aren't
        passed or no pack snapshot is available — single-unit devices and
        pre-pack-recording history degenerate to the main-only behavior."""
        main_soc = self.actual_soc_at(device_sn, ts, window_s=window_s)
        if main_soc is None:
            return None
        if not (main_capacity_wh and pack_capacity_wh):
            return main_soc
        with self._conn() as c:
            pack_rows = c.execute(
                """SELECT ts, soc_pct FROM battery_packs
                    WHERE parent_sn = ?
                      AND ts >= ? AND ts < ?
                      AND soc_pct IS NOT NULL""",
                (device_sn, ts - window_s, ts + window_s),
            ).fetchall()
        if not pack_rows:
            return main_soc
        packs_by_ts: dict[int, list[float]] = {}
        for pack_ts, soc in pack_rows:
            packs_by_ts.setdefault(int(pack_ts), []).append(float(soc))
        return _capacity_weighted_soc(
            main_soc, ts, packs_by_ts, sorted(packs_by_ts),
            main_capacity_wh, pack_capacity_wh, window_s,
        )

    # ---------- algorithm suggestions (Claude advisor) ----------
    def insert_suggestion(self, *, device_sn: str | None, kind: str,
                          target: str | None, current_value: Any,
                          proposed_value: Any, reasoning: str,
                          confidence: str | None,
                          severity: str | None) -> int:
        """Persist a new advisor-generated suggestion. Returns the row id
        so the API can hand it back for apply/dismiss actions. Suggestions
        always start as `pending`; the user (or an expiry sweeper) flips
        them to applied/dismissed."""
        import json as _json
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO algorithm_suggestions
                       (created_at, device_sn, kind, target,
                        current_value, proposed_value, reasoning,
                        confidence, severity, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (int(time.time()), device_sn, kind, target,
                 _json.dumps(current_value) if current_value is not None else None,
                 _json.dumps(proposed_value) if proposed_value is not None else None,
                 reasoning, confidence, severity),
            )
            return int(cur.lastrowid or 0)

    def list_suggestions(self, *, device_sn: str | None = None,
                         status: str | None = None,
                         limit: int = 50) -> list[dict]:
        """Return suggestions ordered newest-first. Filters by device_sn
        if provided (NULL device_sn = global suggestions are always
        included regardless of filter), and by status if provided."""
        import json as _json
        clauses: list[str] = []
        params: list = []
        if device_sn:
            clauses.append("(device_sn = ? OR device_sn IS NULL)")
            params.append(device_sn)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, created_at, device_sn, kind, target,
                          current_value, proposed_value, reasoning,
                          confidence, severity, status, decided_at, decided_by
                     FROM algorithm_suggestions
                     {where}
                     ORDER BY created_at DESC
                     LIMIT ?""",
                params,
            ).fetchall()

        def _decode(v):
            if v is None:
                return None
            try:
                return _json.loads(v)
            except Exception:
                return v
        return [
            {"id": r[0], "created_at": r[1], "device_sn": r[2],
             "kind": r[3], "target": r[4],
             "current_value": _decode(r[5]),
             "proposed_value": _decode(r[6]),
             "reasoning": r[7], "confidence": r[8], "severity": r[9],
             "status": r[10], "decided_at": r[11], "decided_by": r[12]}
            for r in rows
        ]

    def get_suggestion(self, suggestion_id: int) -> dict | None:
        rows = self.list_suggestions(limit=10000)
        return next((r for r in rows if r["id"] == suggestion_id), None)

    def update_suggestion_status(self, suggestion_id: int, status: str,
                                 decided_by: str = "user") -> bool:
        with self._conn() as c:
            cur = c.execute(
                """UPDATE algorithm_suggestions
                      SET status = ?, decided_at = ?, decided_by = ?
                    WHERE id = ?""",
                (status, int(time.time()), decided_by, suggestion_id),
            )
            return cur.rowcount > 0

    def record_change(self, *, suggestion_id: int | None, device_sn: str | None,
                      target: str, old_value: Any, new_value: Any,
                      reasoning: str | None = None) -> int:
        import json as _json
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO algorithm_changes
                       (suggestion_id, applied_at, device_sn, target,
                        old_value, new_value, reasoning)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (suggestion_id, int(time.time()), device_sn, target,
                 _json.dumps(old_value) if old_value is not None else None,
                 _json.dumps(new_value) if new_value is not None else None,
                 reasoning),
            )
            return int(cur.lastrowid or 0)

    def list_changes(self, device_sn: str | None = None,
                     limit: int = 50) -> list[dict]:
        import json as _json
        params: list = []
        where = ""
        if device_sn:
            where = " WHERE (device_sn = ? OR device_sn IS NULL)"
            params.append(device_sn)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, suggestion_id, applied_at, device_sn,
                          target, old_value, new_value, reasoning
                     FROM algorithm_changes{where}
                     ORDER BY applied_at DESC
                     LIMIT ?""",
                params,
            ).fetchall()
        def _decode(v):
            if v is None:
                return None
            try:
                return _json.loads(v)
            except Exception:
                return v
        return [
            {"id": r[0], "suggestion_id": r[1], "applied_at": r[2],
             "device_sn": r[3], "target": r[4],
             "old_value": _decode(r[5]), "new_value": _decode(r[6]),
             "reasoning": r[7]}
            for r in rows
        ]

    # ---------- device_params ----------
    def set_device_param(self, device_sn: str, key: str, value: float | None, *,
                          source: str, n_samples: int | None = None,
                          confidence: str | None = None,
                          note: str | None = None) -> None:
        """Upsert a per-device parameter. `source` records the resolution
        ladder rung that produced the value: 'user' | 'fit' | 'probe' |
        'catalog' | 'default'. The resolver in server.py decides which
        source wins on read."""
        if not device_sn or not key:
            return
        with self._conn() as c:
            c.execute(
                """INSERT INTO device_params
                       (device_sn, key, value, source, n_samples,
                        confidence, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_sn, key) DO UPDATE SET
                     value = excluded.value,
                     source = excluded.source,
                     n_samples = excluded.n_samples,
                     confidence = excluded.confidence,
                     note = excluded.note,
                     updated_at = excluded.updated_at""",
                (device_sn, key,
                 None if value is None else float(value),
                 source, n_samples, confidence, note,
                 int(time.time())),
            )

    def get_device_param(self, device_sn: str, key: str) -> dict | None:
        if not device_sn or not key:
            return None
        with self._conn() as c:
            row = c.execute(
                """SELECT value, source, n_samples, confidence, note, updated_at
                     FROM device_params
                    WHERE device_sn = ? AND key = ?""",
                (device_sn, key),
            ).fetchone()
        if not row:
            return None
        return {"value": row[0], "source": row[1], "n_samples": row[2],
                "confidence": row[3], "note": row[4], "updated_at": row[5]}

    def list_device_params(self, device_sn: str) -> list[dict]:
        if not device_sn:
            return []
        with self._conn() as c:
            rows = c.execute(
                """SELECT key, value, source, n_samples, confidence,
                          note, updated_at
                     FROM device_params
                    WHERE device_sn = ?
                    ORDER BY key""",
                (device_sn,),
            ).fetchall()
        return [{"key": r[0], "value": r[1], "source": r[2],
                 "n_samples": r[3], "confidence": r[4], "note": r[5],
                 "updated_at": r[6]} for r in rows]

    def clear_device_param(self, device_sn: str, key: str) -> None:
        """Remove a stored param so the resolver falls through to the
        next ladder step. Used when the user clicks 'reset to auto-fit'
        after a manual override."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM device_params WHERE device_sn = ? AND key = ?",
                (device_sn, key),
            )

    def expire_old_suggestions(self, max_age_s: int = 7 * 86400) -> int:
        """Auto-dismiss pending suggestions older than max_age_s. Run
        from the daily review job so stale advice doesn't pile up."""
        cutoff = int(time.time()) - max_age_s
        with self._conn() as c:
            cur = c.execute(
                """UPDATE algorithm_suggestions
                      SET status = 'dismissed', decided_at = ?, decided_by = 'auto-expired'
                    WHERE status = 'pending' AND created_at < ?""",
                (int(time.time()), cutoff),
            )
            return cur.rowcount

    def history(self, device_sn: str, hours: int = 24,
                bucket_s: int = 600,
                main_capacity_wh: int | None = None,
                pack_capacity_wh: int | None = None) -> list[dict]:
        """Time-bucketed history. bucket_s controls aggregation granularity:
           600 (10min) for 24h view, 3600 (1h) for 7d, 86400 (1d) for 30d.

        When both `main_capacity_wh` and `pack_capacity_wh` are provided,
        each row also gets a `system_soc` field — the capacity-weighted
        SOC across main + every expansion pack at that bucket's
        timestamp (using the closest battery_packs snapshot within
        ±30 min). Multi-pack rigs need this: `battery_pct` from the
        cloud is the MAIN unit's SOC only, and the main pack drains
        4-6× faster than the system before BMS rebalances. Slope-based
        fits in forecaster (drain model, charge efficiency, inverter
        overhead) that compute drain via `soc_drop × system_capacity`
        will over-attribute drain by the pack ratio if they walk
        `battery_pct`. With `system_soc` available they walk that
        instead. Single-unit devices and back-compat callers (no
        capacity hints) keep the prior behavior; `system_soc` is
        omitted from those rows."""
        since = int(time.time()) - hours * 3600
        bucket_s = max(BUCKET_S, int(bucket_s))
        with self._conn() as c:
            rows = c.execute(
                """SELECT (bucket / ?) * ? AS b,
                           SUM(input_wh) AS in_wh,
                           SUM(output_wh) AS out_wh,
                           SUM(solar_wh) AS sol_wh,
                           SUM(ac_input_wh) AS ac_wh,
                           SUM(solar_charge_diverted_wh) AS div_wh,
                           AVG(last_input_w) AS in_w,
                           AVG(last_output_w) AS out_w,
                           AVG(last_solar_w) AS sol_w,
                           AVG(last_ac_input_w) AS ac_w,
                           AVG(last_battery_pct) AS bat
                    FROM samples
                    WHERE device_sn = ? AND bucket >= ?
                    GROUP BY b
                    ORDER BY b""",
                (bucket_s, bucket_s, device_sn, since),
            ).fetchall()
            # Bulk-fetch pack snapshots covering the bucket range when
            # capacity hints permit a system-SOC computation. The same
            # ±30min window the prediction-accuracy join uses.
            packs_by_ts: dict[int, list[float]] = {}
            ts_sorted: list[int] = []
            if main_capacity_wh and pack_capacity_wh and rows:
                ts_min = rows[0][0] - 1800
                ts_max = rows[-1][0] + 1800
                pack_rows = c.execute(
                    """SELECT ts, soc_pct FROM battery_packs
                        WHERE parent_sn = ?
                          AND ts >= ? AND ts < ?
                          AND soc_pct IS NOT NULL""",
                    (device_sn, ts_min, ts_max),
                ).fetchall()
                for ts, soc in pack_rows:
                    packs_by_ts.setdefault(int(ts), []).append(float(soc))
                ts_sorted = sorted(packs_by_ts)
            # Per-bucket fraction of time the solar-charge plug was ON,
            # from the controller's decision log. This is the RELIABLE
            # diversion signal — emeter-independent — used by the
            # forecaster's load-profile fit to net the EV charge out of
            # learned demand (the configured EP10 is a non-emeter plug
            # that records solar_charge_diverted_wh=0 even while the car
            # draws ~car_load_w, which otherwise poisons the profile and
            # drives the forecast to 0% SOC). plug_state_before is
            # snapshotted each evaluate tick (~30s), so on/total
            # approximates the on-fraction within the bucket. Absent
            # (None) when no decision covered the bucket — fit falls back.
            plug_on_rows = c.execute(
                """SELECT (decided_at / ?) * ? AS b,
                          SUM(CASE WHEN plug_state_before = 'on'
                                   THEN 1 ELSE 0 END) AS on_n,
                          COUNT(*) AS tot_n
                   FROM solar_charge_decisions
                   WHERE device_sn = ? AND decided_at >= ?
                   GROUP BY b""",
                (bucket_s, bucket_s, device_sn, since),
            ).fetchall()
            plug_on_frac = {int(b): on_n / tot_n
                            for b, on_n, tot_n in plug_on_rows if tot_n}
        out = []
        for r in rows:
            # solar_charge_diverted_wh is GROSS output that was intentionally
            # routed to a downstream load (EV charger) by the solar-charge
            # controller. Exposed as a separate field so the forecaster's
            # load-profile fit can subtract it (real demand only) while
            # other fits (drain model, charge efficiency) keep using the
            # gross output_wh — they need total load, not net of diversion,
            # for their slope math to work out.
            row = {"ts": r[0], "input_wh": r[1] or 0, "output_wh": r[2] or 0,
                   "solar_wh": r[3] or 0, "ac_input_wh": r[4] or 0,
                   "solar_charge_diverted_wh": r[5] or 0,
                   "input_w": int(r[6] or 0), "output_w": int(r[7] or 0),
                   "solar_w": int(r[8] or 0), "ac_input_w": int(r[9] or 0),
                   "battery_pct": int(r[10]) if r[10] is not None else None}
            if ts_sorted and row["battery_pct"] is not None:
                row["system_soc"] = _capacity_weighted_soc(
                    float(row["battery_pct"]), int(row["ts"]),
                    packs_by_ts, ts_sorted,
                    main_capacity_wh, pack_capacity_wh,
                )
            if plug_on_frac:
                frac = plug_on_frac.get(int(r[0]))
                if frac is not None:
                    row["solar_charge_plug_on_frac"] = frac
            out.append(row)
        return out


def _capacity_weighted_soc(main_soc: float,
                           target: int,
                           packs_by_ts: dict[int, list[float]],
                           ts_sorted: list[int],
                           main_wh: int | None,
                           pack_wh: int | None,
                           window_s: int = 1800) -> float:
    """Capacity-weighted system SOC using the closest pack snapshot to
    `target` (within ±window_s). Falls back to `main_soc` when:
      - capacity hints aren't passed (single-unit device or back-compat call),
      - no pack snapshot exists in the window (single-unit device, or
        gap in pack history),
      - the snapshot has no valid pack readings.
    Stays in [0, 100] regardless of input."""
    if not (main_wh and pack_wh and ts_sorted):
        return main_soc
    i = bisect.bisect_left(ts_sorted, target)
    candidates = []
    if i < len(ts_sorted):
        candidates.append(ts_sorted[i])
    if i > 0:
        candidates.append(ts_sorted[i - 1])
    in_window = [ts for ts in candidates if target - window_s <= ts < target + window_s]
    if not in_window:
        return main_soc
    best_ts = min(in_window, key=lambda ts: abs(ts - target))
    pack_socs = packs_by_ts.get(best_ts) or []
    if not pack_socs:
        return main_soc
    total_wh = main_wh + len(pack_socs) * pack_wh
    if total_wh <= 0:
        return main_soc
    stored = main_soc * main_wh + sum(p * pack_wh for p in pack_socs)
    return max(0.0, min(100.0, stored / total_wh))


def _nullable_float(v: Any) -> float | None:
    """float(v) but pass None / unparseable through as None so SQLite stores
    NULL rather than 0.0. Used by battery_packs ingestion where the cloud
    sometimes omits a field instead of returning 0."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _start_of_day(now_ts: int) -> int:
    """User-local midnight as unix seconds.

    Reads the UTC offset from /data/location.json — set by the weather
    client (Open-Meteo) when location is configured, or by the server
    poll loop from the device's own `uo` telemetry field. Falls back to
    the container's TZ env var (likely UTC on slim images)."""
    try:
        import location as device_location
        offset = device_location.get_tz_offset()
        if offset is not None:
            local_now = now_ts + offset
            local_midnight_local = (local_now // 86400) * 86400
            return int(local_midnight_local - offset)
    except Exception as e:
        log.debug("location-based start_of_day failed: %s", e)
    # Fallback: container's localtime (likely UTC on slim images).
    lt = time.localtime(now_ts)
    midnight = time.mktime(time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
         lt.tm_wday, lt.tm_yday, lt.tm_isdst)))
    return int(midnight)
