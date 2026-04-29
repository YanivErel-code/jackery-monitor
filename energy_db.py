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

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

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
    last_seen   INTEGER
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
"""


class EnergyDB:
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
               ac_input_w: float = 0.0) -> None:
        """Integrate (input_w, output_w, solar_w, ac_input_w) since last
        reading for this device. ac_input_w is the AC/grid charging power
        (the device's `acip` field), tracked separately so cost accounting
        knows what was paid-for vs free."""
        if not device_sn:
            return
        prev = self._last.get(device_sn)
        self._last[device_sn] = (ts, float(input_w), float(output_w),
                                  float(solar_w), float(ac_input_w))
        if prev is None:
            return  # need two samples to integrate
        prev_ts, prev_in, prev_out, prev_solar, prev_ac = prev
        dt = ts - prev_ts
        if dt <= 0 or dt > MAX_GAP_S:
            return
        # Trapezoidal: avg power times dt, in seconds
        in_wh = ((prev_in + input_w) / 2.0) * (dt / 3600.0)
        out_wh = ((prev_out + output_w) / 2.0) * (dt / 3600.0)
        solar_wh = ((prev_solar + solar_w) / 2.0) * (dt / 3600.0)
        ac_wh = ((prev_ac + ac_input_w) / 2.0) * (dt / 3600.0)
        bucket = int(ts // BUCKET_S) * BUCKET_S

        with self._conn() as c:
            c.execute(
                """INSERT INTO samples
                       (device_sn, bucket, input_wh, output_wh, solar_wh, ac_input_wh,
                        last_input_w, last_output_w, last_solar_w, last_ac_input_w,
                        last_battery_pct, sample_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(device_sn, bucket) DO UPDATE SET
                     input_wh = input_wh + excluded.input_wh,
                     output_wh = output_wh + excluded.output_wh,
                     solar_wh = solar_wh + excluded.solar_wh,
                     ac_input_wh = ac_input_wh + excluded.ac_input_wh,
                     last_input_w = excluded.last_input_w,
                     last_output_w = excluded.last_output_w,
                     last_solar_w = excluded.last_solar_w,
                     last_ac_input_w = excluded.last_ac_input_w,
                     last_battery_pct = COALESCE(excluded.last_battery_pct,
                                                 last_battery_pct),
                     sample_count = sample_count + 1
                """,
                (device_sn, bucket, in_wh, out_wh, solar_wh, ac_wh,
                 int(input_w), int(output_w), int(solar_w), int(ac_input_w),
                 battery_pct),
            )

    # ---------- queries ----------
    def list_devices(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT device_sn, name, model_code, model_name,
                          first_seen, last_seen
                   FROM devices ORDER BY last_seen DESC"""
            ).fetchall()
        return [
            {"device_sn": r[0], "name": r[1], "model_code": r[2],
             "model_name": r[3], "first_seen": r[4], "last_seen": r[5]}
            for r in rows
        ]

    def totals(self, device_sn: str) -> dict:
        """Lifetime + windowed totals for a single device."""
        now = int(time.time())
        windows = {
            "today": _start_of_day(now),
            "last_7d": now - 7 * 86400,
            "last_30d": now - 30 * 86400,
        }
        with self._conn() as c:
            out: dict = {"device_sn": device_sn}
            # Lifetime
            r = c.execute(
                "SELECT COALESCE(SUM(input_wh),0), COALESCE(SUM(output_wh),0) "
                "FROM samples WHERE device_sn = ?", (device_sn,)
            ).fetchone()
            out["lifetime"] = {"input_wh": r[0], "output_wh": r[1]}
            # Windows
            for label, since in windows.items():
                r = c.execute(
                    "SELECT COALESCE(SUM(input_wh),0), COALESCE(SUM(output_wh),0) "
                    "FROM samples WHERE device_sn = ? AND bucket >= ?",
                    (device_sn, since),
                ).fetchone()
                out[label] = {"input_wh": r[0], "output_wh": r[1], "since": since}
        return out

    def all_totals(self) -> list[dict]:
        return [self.totals(d["device_sn"]) | {"name": d["name"]}
                for d in self.list_devices()]

    # ---------- forecast predictions vs actuals ----------
    def record_forecast(self, device_sn: str, made_at: float,
                        predictions: list[dict]) -> int:
        """Persist a forecast snapshot. `predictions` is a list of
        {ts, predicted_soc} entries. INSERT OR REPLACE on the (device,
        made_at, target) primary key so multiple calls within the same
        hour collapse to one row per target. Returns rows written."""
        if not device_sn or not predictions:
            return 0
        made_at_hour = int(made_at // 3600) * 3600
        rows = []
        for p in predictions:
            ts = p.get("ts")
            soc = p.get("predicted_soc")
            if ts is None or soc is None:
                continue
            target = int(int(ts) // 3600) * 3600
            rows.append((device_sn, made_at_hour, target, float(soc)))
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT OR REPLACE INTO forecast_predictions
                       (device_sn, made_at, target, predicted_soc)
                   VALUES (?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def prediction_accuracy(self, device_sn: str,
                            max_age_days: int = 14,
                            limit: int = 500) -> list[dict]:
        """Return predicted-vs-actual pairs for predictions whose target is
        in the past. Each entry has {made_at, target, predicted_soc,
        actual_soc, lead_time_h, error}. Joins each prediction to the
        average last_battery_pct in the ±30 min window around the target."""
        now = int(time.time())
        cutoff_low = now - max_age_days * 86400
        with self._conn() as c:
            rows = c.execute(
                """SELECT p.made_at, p.target, p.predicted_soc,
                          (SELECT AVG(s.last_battery_pct)
                             FROM samples s
                            WHERE s.device_sn = p.device_sn
                              AND s.bucket >= p.target - 1800
                              AND s.bucket <  p.target + 1800
                              AND s.last_battery_pct IS NOT NULL) AS actual_soc
                     FROM forecast_predictions p
                    WHERE p.device_sn = ?
                      AND p.target <= ?
                      AND p.made_at >= ?
                    ORDER BY p.target DESC
                    LIMIT ?""",
                (device_sn, now, cutoff_low, int(limit)),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            actual = r[3]
            if actual is None:
                continue
            out.append({
                "made_at": r[0],
                "target": r[1],
                "predicted_soc": float(r[2]),
                "actual_soc": float(actual),
                "lead_time_h": round((r[1] - r[0]) / 3600, 1),
                "error": round(abs(float(actual) - float(r[2])), 1),
            })
        return out

    def history(self, device_sn: str, hours: int = 24,
                bucket_s: int = 600) -> list[dict]:
        """Time-bucketed history. bucket_s controls aggregation granularity:
           600 (10min) for 24h view, 3600 (1h) for 7d, 86400 (1d) for 30d."""
        since = int(time.time()) - hours * 3600
        bucket_s = max(BUCKET_S, int(bucket_s))
        with self._conn() as c:
            rows = c.execute(
                """SELECT (bucket / ?) * ? AS b,
                           SUM(input_wh) AS in_wh,
                           SUM(output_wh) AS out_wh,
                           SUM(solar_wh) AS sol_wh,
                           SUM(ac_input_wh) AS ac_wh,
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
        return [
            {"ts": r[0], "input_wh": r[1] or 0, "output_wh": r[2] or 0,
             "solar_wh": r[3] or 0, "ac_input_wh": r[4] or 0,
             "input_w": int(r[5] or 0), "output_w": int(r[6] or 0),
             "solar_w": int(r[7] or 0), "ac_input_w": int(r[8] or 0),
             "battery_pct": int(r[9]) if r[9] is not None else None}
            for r in rows
        ]


def _start_of_day(now_ts: int) -> int:
    """User-local midnight as unix seconds.

    Reads the UTC offset from /data/location.json (populated by the weather
    client from Open-Meteo). Falls back to the container's TZ env var (which
    is normally UTC in slim images, so the "today" boundary would land at the
    wrong time without the offset)."""
    try:
        import location as device_location
        loc = device_location.get()
        if loc and "utc_offset_seconds" in loc:
            offset = int(loc["utc_offset_seconds"])
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
