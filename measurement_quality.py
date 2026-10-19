"""Persistent, reversible exclusion of confirmed measurement-discontinuity windows."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, TypedDict

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurement_incidents (
    device_sn TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'quarantined',
    PRIMARY KEY (device_sn, start_ts),
    CHECK (end_ts > start_ts),
    CHECK (status IN ('quarantined', 'restored'))
);
CREATE TABLE IF NOT EXISTS measurement_quarantine (
    device_sn TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    row_json TEXT NOT NULL,
    patch_json TEXT,
    PRIMARY KEY (device_sn, start_ts, table_name, row_key)
);
"""

TABLE_KEYS = {
    "samples": ("device_sn", "bucket"),
    "battery_packs": ("ts", "parent_sn", "pack_sn"),
    "forecast_predictions": ("device_sn", "made_at", "target"),
    "daily_solar_summary": ("date", "device_sn"),
}
SUMMARY_FIELDS = frozenset({
    "actual_sunset_soc_pct", "actual_sunrise_soc_pct", "sunset_forecast_actual_soc_pct",
    "predicted_sunset_soc_pct", "predicted_sunrise_soc_pct", "sunset_forecast_soc_pct",
    "sunset_forecast_made_at",
})
ACTUAL_TARGETS = {
    "actual_sunset_soc_pct": "sunset_ts",
    "actual_sunrise_soc_pct": "sunrise_ts",
    "sunset_forecast_actual_soc_pct": "sunset_forecast_sunrise_ts",
}
QUARANTINE_QUERIES = {
    "samples": (
        "SELECT * FROM samples WHERE device_sn=? AND bucket>=? AND bucket<?",
        "DELETE FROM samples WHERE device_sn=? AND bucket>=? AND bucket<?",
    ),
    "battery_packs": (
        "SELECT * FROM battery_packs WHERE parent_sn=? AND ts>=? AND ts<?",
        "DELETE FROM battery_packs WHERE parent_sn=? AND ts>=? AND ts<?",
    ),
    "forecast_predictions": (
        "SELECT * FROM forecast_predictions WHERE device_sn=? AND "
        "((made_at+3600>? AND made_at<?) OR (target+1800>? AND target-1800<?))",
        "DELETE FROM forecast_predictions WHERE device_sn=? AND "
        "((made_at+3600>? AND made_at<?) OR (target+1800>? AND target-1800<?))",
    ),
}
RESTORE_QUERIES = {
    "samples": "SELECT * FROM samples WHERE device_sn=? AND bucket=?",
    "battery_packs": "SELECT * FROM battery_packs WHERE ts=? AND parent_sn=? AND pack_sn=?",
    "forecast_predictions": "SELECT * FROM forecast_predictions WHERE device_sn=? AND made_at=? AND target=?",
    "daily_solar_summary": "SELECT * FROM daily_solar_summary WHERE date=? AND device_sn=?",
}
SUMMARY_UPDATES = {
    "actual_sunset_soc_pct": "UPDATE daily_solar_summary SET actual_sunset_soc_pct=? WHERE device_sn=? AND date=?",
    "actual_sunrise_soc_pct": "UPDATE daily_solar_summary SET actual_sunrise_soc_pct=? WHERE device_sn=? AND date=?",
    "sunset_forecast_actual_soc_pct": "UPDATE daily_solar_summary SET sunset_forecast_actual_soc_pct=? WHERE device_sn=? AND date=?",
    "predicted_sunset_soc_pct": "UPDATE daily_solar_summary SET predicted_sunset_soc_pct=? WHERE device_sn=? AND date=?",
    "predicted_sunrise_soc_pct": "UPDATE daily_solar_summary SET predicted_sunrise_soc_pct=? WHERE device_sn=? AND date=?",
    "sunset_forecast_soc_pct": "UPDATE daily_solar_summary SET sunset_forecast_soc_pct=? WHERE device_sn=? AND date=?",
    "sunset_forecast_made_at": "UPDATE daily_solar_summary SET sunset_forecast_made_at=? WHERE device_sn=? AND date=?",
}


class QualityResult(TypedDict):
    archived_rows: int
    restored_rows: int
    start_ts: int
    end_ts: int


class QualityHost(Protocol):
    _conn: Callable[[], AbstractContextManager[sqlite3.Connection]]
    _last: dict[str, tuple[float, ...]]


def overlaps(connection: sqlite3.Connection, device_sn: str, start: float, end: float,
             *, exclude_start: int | None = None) -> bool:
    return connection.execute(
        "SELECT 1 FROM measurement_incidents WHERE device_sn=? AND status='quarantined' "
        "AND start_ts<? AND end_ts>? AND (? IS NULL OR start_ts<>?) LIMIT 1",
        (device_sn, end, start, exclude_start, exclude_start),
    ).fetchone() is not None


def _archive(connection: sqlite3.Connection, device_sn: str, start: int,
             table: str, row: sqlite3.Row, patch: dict[str, object] | None = None) -> None:
    if table not in TABLE_KEYS or (patch is not None and not patch.keys() <= SUMMARY_FIELDS):
        raise ValueError("invalid quarantine table or summary patch")
    key = json.dumps([row[column] for column in TABLE_KEYS[table]], separators=(",", ":"))
    original = json.dumps(dict(row), sort_keys=True, allow_nan=False)
    previous = connection.execute(
        "SELECT row_json,patch_json FROM measurement_quarantine "
        "WHERE device_sn=? AND start_ts=? AND table_name=? AND row_key=?",
        (device_sn, start, table, key),
    ).fetchone()
    if previous:
        if patch is None and previous[0] != original:
            raise ValueError("quarantined history changed; refusing to discard new values")
        if patch is not None:
            merged = {**patch, **json.loads(previous[1] or "{}")}
            connection.execute(
                "UPDATE measurement_quarantine SET patch_json=? "
                "WHERE device_sn=? AND start_ts=? AND table_name=? AND row_key=?",
                (json.dumps(merged), device_sn, start, table, key),
            )
        return
    connection.execute(
        "INSERT INTO measurement_quarantine(device_sn,start_ts,table_name,row_key,row_json,patch_json) "
        "VALUES(?,?,?,?,?,?)",
        (device_sn, start, table, key, original, json.dumps(patch) if patch is not None else None),
    )


def _summary_patch(row: sqlite3.Row, start: int, end: int) -> dict[str, object]:
    patch: dict[str, object] = {}
    for field, target in ACTUAL_TARGETS.items():
        timestamp = row[target]
        if timestamp is not None and timestamp + 1800 > start and timestamp - 1800 < end:
            if row[field] is not None:
                patch[field] = row[field]
    made_at = row["predictions_made_at"]
    if made_at is not None and start <= made_at < end:
        for field, target in (("predicted_sunset_soc_pct", "sunset_ts"),
                              ("predicted_sunrise_soc_pct", "sunrise_ts")):
            if row[target] is not None and row[target] >= start and row[field] is not None:
                patch[field] = row[field]
    snapshot = row["sunset_forecast_made_at"]
    if snapshot is not None and start <= snapshot < end:
        for field in ("sunset_forecast_soc_pct", "sunset_forecast_made_at"):
            patch[field] = row[field]
    return patch


class MeasurementQualityMixin:
    def measurement_quarantined(self: QualityHost, device_sn: str, start: float,
                               end: float | None = None) -> bool:
        with self._conn() as connection:
            return overlaps(connection, device_sn, start, end if end is not None else start + 1)

    def quarantine_measurements(self: QualityHost, device_sn: str, start_ts: float, end_ts: float,
                                *, reason: str) -> QualityResult:
        """Archive originals and remove only measurement/forecast data, never controller audit."""
        if (not device_sn or not reason or any(isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (start_ts, end_ts)) or end_ts <= start_ts):
            raise ValueError("invalid measurement incident")
        start, end = math.floor(start_ts / 60) * 60, math.ceil(end_ts / 60) * 60
        removed = 0
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT end_ts,status FROM measurement_incidents WHERE device_sn=? AND start_ts=?",
                (device_sn, start),
            ).fetchone()
            if previous:
                end = max(end, previous[0])
                if end == previous[0] and previous[1] == "quarantined":
                    self._last.pop(device_sn, None)
                    return {"archived_rows": 0, "restored_rows": 0, "start_ts": start, "end_ts": end}
            connection.execute(
                "INSERT INTO measurement_incidents(device_sn,start_ts,end_ts,detected_at,reason) "
                "VALUES(?,?,?,?,?) ON CONFLICT(device_sn,start_ts) DO UPDATE "
                "SET end_ts=excluded.end_ts,status='quarantined'",
                (device_sn, start, end, int(time.time()), reason),
            )
            for table, (select_query, delete_query) in QUARANTINE_QUERIES.items():
                params = ((device_sn, start, end, start, end) if table == "forecast_predictions"
                          else (device_sn, start, end))
                rows = connection.execute(select_query, params).fetchall()
                for row in rows:
                    _archive(connection, device_sn, start, table, row)
                connection.execute(delete_query, params)
                removed += len(rows)
            summaries = connection.execute(
                "SELECT * FROM daily_solar_summary WHERE device_sn=?", (device_sn,),
            ).fetchall()
            for row in summaries:
                patch = _summary_patch(row, start, end)
                if not patch:
                    continue
                _archive(connection, device_sn, start, "daily_solar_summary", row, patch)
                for column in patch:
                    connection.execute(SUMMARY_UPDATES[column], (None, device_sn, row["date"]))
                removed += 1
        self._last.pop(device_sn, None)
        return {"archived_rows": removed, "restored_rows": 0, "start_ts": start, "end_ts": end}

    def restore_measurements(self: QualityHost, device_sn: str, start_ts: int) -> QualityResult:
        """Selectively restore archived rows; conflicting/newer values abort the transaction."""
        restored = 0
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            incident = connection.execute(
                "SELECT end_ts,status FROM measurement_incidents WHERE device_sn=? AND start_ts=?",
                (device_sn, start_ts),
            ).fetchone()
            if not incident:
                raise ValueError("unknown measurement incident")
            if incident[1] == "restored":
                return {"archived_rows": 0, "restored_rows": 0, "start_ts": start_ts, "end_ts": incident[0]}
            if connection.execute(
                "SELECT 1 FROM measurement_incidents WHERE device_sn=? AND start_ts<>? "
                "AND status='quarantined' AND start_ts<? AND end_ts>? LIMIT 1",
                (device_sn, start_ts, incident[0], start_ts),
            ).fetchone():
                raise ValueError("overlapping quarantine must be reviewed before restore")
            archives = connection.execute(
                "SELECT table_name,row_key,row_json,patch_json FROM measurement_quarantine "
                "WHERE device_sn=? AND start_ts=? ORDER BY table_name,row_key", (device_sn, start_ts),
            ).fetchall()
            for table, key_json, original_json, patch_json in archives:
                if table not in TABLE_KEYS:
                    raise ValueError("invalid quarantine table")
                original = json.loads(original_json)
                key_values = json.loads(key_json)
                parent_column = "parent_sn" if table == "battery_packs" else "device_sn"
                if (original.get(parent_column) != device_sn
                        or key_values != [original[column] for column in TABLE_KEYS[table]]):
                    raise ValueError("archive key/device mismatch; refusing restore")
                if table == "forecast_predictions" and (
                        overlaps(connection, device_sn, original["made_at"], original["made_at"] + 3600,
                                 exclude_start=start_ts)
                        or overlaps(connection, device_sn, original["target"] - 1800, original["target"] + 1800,
                                    exclude_start=start_ts)):
                    raise ValueError("another quarantine covers this forecast window")
                current = connection.execute(RESTORE_QUERIES[table], key_values).fetchone()
                if patch_json is not None:
                    patch = json.loads(patch_json)
                    if table != "daily_solar_summary" or not patch.keys() <= SUMMARY_FIELDS or current is None:
                        raise ValueError("invalid or missing summary quarantine row")
                    for field, target_column in ACTUAL_TARGETS.items():
                        target = original.get(target_column)
                        if field in patch and target is not None and overlaps(
                                connection, device_sn, target - 1800, target + 1800, exclude_start=start_ts):
                            raise ValueError("another quarantine covers this actual observation window")
                    if any(current[column] is not None and current[column] != value for column, value in patch.items()):
                        raise ValueError("summary changed; refusing restore")
                    for column, value in patch.items():
                        connection.execute(SUMMARY_UPDATES[column], (value, device_sn, original["date"]))
                    restored += 1
                elif current is not None:
                    if dict(current) != original:
                        raise ValueError("history changed; refusing restore")
                else:
                    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                    if set(columns) != original.keys():
                        raise ValueError("table schema changed; review restore before proceeding")
                    placeholders = ",".join("?" for _ in columns)
                    connection.execute(f"INSERT INTO {table} VALUES({placeholders})", [original[column] for column in columns])
                    restored += 1
            connection.execute(
                "UPDATE measurement_incidents SET status='restored' WHERE device_sn=? AND start_ts=?",
                (device_sn, start_ts),
            )
        self._last.pop(device_sn, None)
        return {"archived_rows": 0, "restored_rows": restored, "start_ts": start_ts, "end_ts": incident[0]}
