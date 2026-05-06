"""Forecast-prediction storage + accuracy scoring.

Mixed into EnergyDB. Owns the `forecast_predictions` table — every
record_forecast() call snapshots a {target → predicted_soc} list, and
prediction_accuracy() joins each past target back to the actual SOC
observed at that time. The forecaster + the daily advisor both feed
on what comes out of here.

This is a mixin, not a standalone class — it relies on the host class
(EnergyDB) providing `_conn()`, `_packs_in_target_range()`, and the
threading lock the connection factory acquires. Splitting it out makes
energy_db.py easier to navigate without changing any caller's API.
"""

from __future__ import annotations

import time


class ForecastTablesMixin:
    """Forecast-storage and accuracy-scoring methods for EnergyDB."""

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
                            limit: int = 500,
                            since_made_at_ts: int | None = None,
                            main_capacity_wh: int | None = None,
                            pack_capacity_wh: int | None = None) -> list[dict]:
        """Return predicted-vs-actual pairs for predictions whose target is
        in the past. Each entry has {made_at, target, predicted_soc,
        actual_soc, lead_time_h, error}.

        `actual_soc` is capacity-weighted system SOC (main + expansion
        packs) when both `main_capacity_wh` and `pack_capacity_wh` are
        passed AND a pack snapshot exists in the target window. This
        matches the predicted_soc which is always seeded with system
        SOC at made_at. Without capacity hints, falls back to main-only
        last_battery_pct (legacy behavior).

        `since_made_at_ts`: when set, only return predictions whose
        `made_at` is at or after this unix timestamp. Used by the
        dashboard to slice off forecasts produced by pre-fix code so
        the headline accuracy summary reflects current behavior.
        Defaults to the 14-day window when None."""
        # Late import to avoid a circular dep with the host module.
        from energy_db import _capacity_weighted_soc
        now = int(time.time())
        # Effective lower bound: max(default 14d window, caller's cutoff).
        cutoff_low = now - max_age_days * 86400
        if since_made_at_ts is not None:
            cutoff_low = max(cutoff_low, int(since_made_at_ts))
        with self._conn() as c:
            rows = c.execute(
                """SELECT p.made_at, p.target, p.predicted_soc,
                          (SELECT AVG(s.last_battery_pct)
                             FROM samples s
                            WHERE s.device_sn = p.device_sn
                              AND s.bucket >= p.target - 1800
                              AND s.bucket <  p.target + 1800
                              AND s.last_battery_pct IS NOT NULL) AS main_soc
                     FROM forecast_predictions p
                    WHERE p.device_sn = ?
                      AND p.target <= ?
                      AND p.made_at >= ?
                    ORDER BY p.target DESC
                    LIMIT ?""",
                (device_sn, now, cutoff_low, int(limit)),
            ).fetchall()
            packs_by_ts, ts_sorted = self._packs_in_target_range(
                c, device_sn, rows, main_capacity_wh, pack_capacity_wh,
            )
        out: list[dict] = []
        for r in rows:
            main_soc = r[3]
            if main_soc is None:
                continue
            actual = _capacity_weighted_soc(
                float(main_soc), int(r[1]),
                packs_by_ts, ts_sorted,
                main_capacity_wh, pack_capacity_wh,
            )
            out.append({
                "made_at": r[0],
                "target": r[1],
                "predicted_soc": float(r[2]),
                "actual_soc": round(actual, 1),
                "lead_time_h": round((r[1] - r[0]) / 3600, 1),
                "error": round(abs(actual - float(r[2])), 1),
            })
        return out
