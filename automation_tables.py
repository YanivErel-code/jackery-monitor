"""Smart-charge decision log + automation rule firings.

Mixed into EnergyDB. Owns two tables:
  - smart_charge_decisions: one row per controller tick. Each tick's
    plan + executed flag + narration are persisted so we can compute
    predicted-vs-actual sunrise SOC after the night completes.
  - automation_firings: one row per edge-triggered rule firing.
    Pairs of on/off rows give the ON-time history for each Kasa plug.

This is a mixin, not a standalone class — it relies on the host
(EnergyDB) providing `_conn()` and `_packs_in_target_range()`.
Splitting it out shrinks energy_db.py without changing the API.
"""

from __future__ import annotations

import time


class AutomationTablesMixin:
    """Smart-charge + automation-firing storage methods for EnergyDB."""

    # ---------- smart-charge decisions ----------
    def record_smart_charge_decision(self, device_sn: str, plan: dict,
                                     executed: bool,
                                     narration: str = "") -> None:
        """Persist one tick's worth of smart-charge decision. Idempotent on
        (decided_at, device_sn) — safe if the periodic tick fires twice
        in the same second."""
        if not device_sn or not plan:
            return
        row = (
            int(plan.get("decided_at") or time.time()),
            device_sn,
            str(plan.get("mode") or "off")[:16],
            str(plan.get("action") or "skip")[:16],
            1 if executed else 0,
            (plan.get("reason") or "")[:256],
            plan.get("current_soc_pct"),
            plan.get("predicted_sunrise_soc_pct"),
            plan.get("target_sunrise_soc_pct"),
            plan.get("deficit_kwh"),
            plan.get("window_start"),
            plan.get("window_end"),
            plan.get("sunrise_ts"),
            plan.get("cheapest_rate"),
            (narration or "")[:512],
            plan.get("baseline_predicted_sunrise_soc_pct"),
        )
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO smart_charge_decisions
                       (decided_at, device_sn, mode, action, executed, reason,
                        current_soc_pct, predicted_sunrise_soc_pct,
                        target_sunrise_soc_pct, deficit_kwh,
                        window_start, window_end, sunrise_ts, cheapest_rate,
                        narration, baseline_predicted_sunrise_soc_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )

    def list_smart_charge_decisions(self, device_sn: str | None = None,
                                    limit: int = 100,
                                    since_ts: int = 0) -> list[dict]:
        """Most-recent-first decision log. since_ts=0 returns everything."""
        params: list = []
        sql = """SELECT decided_at, device_sn, mode, action, executed, reason,
                        current_soc_pct, predicted_sunrise_soc_pct,
                        target_sunrise_soc_pct, deficit_kwh,
                        window_start, window_end, sunrise_ts, cheapest_rate,
                        narration, baseline_predicted_sunrise_soc_pct
                 FROM smart_charge_decisions"""
        clauses = []
        if device_sn:
            clauses.append("device_sn = ?")
            params.append(device_sn)
        if since_ts:
            clauses.append("decided_at >= ?")
            params.append(int(since_ts))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY decided_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            {"decided_at": r[0], "device_sn": r[1], "mode": r[2],
             "action": r[3], "executed": bool(r[4]), "reason": r[5],
             "current_soc_pct": r[6], "predicted_sunrise_soc_pct": r[7],
             "target_sunrise_soc_pct": r[8], "deficit_kwh": r[9],
             "window_start": r[10], "window_end": r[11],
             "sunrise_ts": r[12], "cheapest_rate": r[13],
             "narration": r[14],
             "baseline_predicted_sunrise_soc_pct": r[15]}
            for r in rows
        ]

    def smart_charge_analytics(self, device_sn: str,
                               days: int = 14,
                               main_capacity_wh: int | None = None,
                               pack_capacity_wh: int | None = None,
                               ) -> list[dict]:
        """For each `decided_at` row whose `sunrise_ts` is in the past,
        return predicted-vs-actual SOC pairs. Used by the Automation tab.

        `actual_sunrise_soc_pct` is capacity-weighted system SOC when
        capacity hints are passed (matching the predicted, which is
        seeded with system SOC); main-only otherwise."""
        # Late import to avoid a circular dep with the host module.
        from energy_db import _capacity_weighted_soc
        cutoff = int(time.time()) - days * 86400
        now = int(time.time())
        with self._conn() as c:
            rows = c.execute(
                """SELECT d.decided_at, d.action, d.executed,
                          d.predicted_sunrise_soc_pct, d.target_sunrise_soc_pct,
                          d.sunrise_ts, d.mode, d.reason,
                          (SELECT AVG(s.last_battery_pct)
                             FROM samples s
                            WHERE s.device_sn = d.device_sn
                              AND s.bucket >= d.sunrise_ts - 1800
                              AND s.bucket <  d.sunrise_ts + 1800
                              AND s.last_battery_pct IS NOT NULL) AS main_soc,
                          d.baseline_predicted_sunrise_soc_pct
                     FROM smart_charge_decisions d
                    WHERE d.device_sn = ?
                      AND d.decided_at >= ?
                      AND d.sunrise_ts IS NOT NULL
                      AND d.sunrise_ts <= ?
                    ORDER BY d.decided_at DESC""",
                (device_sn, cutoff, now),
            ).fetchall()
            # Reuse the same bulk-pack helper as prediction_accuracy:
            # masquerade rows as (made_at, target, predicted, main_soc).
            shaped = [(r[0], r[5], r[3], r[8]) for r in rows]
            packs_by_ts, ts_sorted = self._packs_in_target_range(
                c, device_sn, shaped, main_capacity_wh, pack_capacity_wh,
            )
        out: list[dict] = []
        for r in rows:
            main_soc = r[8]
            if main_soc is None:
                continue
            actual = _capacity_weighted_soc(
                float(main_soc), int(r[5]),
                packs_by_ts, ts_sorted,
                main_capacity_wh, pack_capacity_wh,
            )
            out.append({
                "decided_at": r[0], "action": r[1], "executed": bool(r[2]),
                "predicted_sunrise_soc_pct": r[3],
                "target_sunrise_soc_pct": r[4],
                "sunrise_ts": r[5],
                "mode": r[6],
                "reason": r[7],
                "actual_sunrise_soc_pct": actual,
                "prediction_error_pp": round(actual - float(r[3]), 1)
                                        if r[3] is not None else None,
                "target_hit": (actual >= float(r[4]))
                              if r[4] is not None else None,
                "baseline_predicted_sunrise_soc_pct": r[9],
            })
        return out

    # ---------- automation rule firings ----------
    def record_automation_fire(self, *, rule_id: str, rule_name: str | None,
                               action: str, kasa_host: str,
                               jackery_sn: str | None,
                               soc_at_fire: float | None,
                               trigger: str | None = "soc",
                               operator: str | None = None,
                               threshold: float | None = None,
                               fired_at: int | None = None) -> int | None:
        """Append one row to automation_firings. Called from the rule
        engine on every successful edge-triggered firing. Returns the
        row id, or None if invalid input."""
        if not rule_id or not action or not kasa_host:
            return None
        ts = int(fired_at if fired_at is not None else time.time())
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO automation_firings
                       (fired_at, rule_id, rule_name, action, kasa_host,
                        jackery_sn, soc_at_fire, trigger, operator, threshold)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, rule_id, rule_name, action, kasa_host, jackery_sn,
                 soc_at_fire, trigger, operator, threshold),
            )
            return cur.lastrowid

    def list_automation_firings(self, *,
                                rule_id: str | None = None,
                                kasa_host: str | None = None,
                                days: int = 30,
                                limit: int = 500) -> list[dict]:
        """Return firings, newest first. Filter by rule_id (history per
        rule) or kasa_host (firings on a specific plug, used for duration
        pairing across rules that share a target)."""
        cutoff = int(time.time()) - days * 86400
        clauses = ["fired_at >= ?"]
        params: list = [cutoff]
        if rule_id:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if kasa_host:
            clauses.append("kasa_host = ?")
            params.append(kasa_host)
        where = " AND ".join(clauses)
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, fired_at, rule_id, rule_name, action,
                          kasa_host, jackery_sn, soc_at_fire, trigger,
                          operator, threshold
                     FROM automation_firings
                    WHERE {where}
                    ORDER BY fired_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        return [
            {"id": r[0], "fired_at": r[1], "rule_id": r[2],
             "rule_name": r[3], "action": r[4], "kasa_host": r[5],
             "jackery_sn": r[6], "soc_at_fire": r[7],
             "trigger": r[8], "operator": r[9], "threshold": r[10]}
            for r in rows
        ]

    def automation_on_intervals(self, kasa_host: str, *,
                                days: int = 30) -> list[dict]:
        """Pair consecutive firings on a Kasa plug into ON intervals.
        Walks firings chronologically: each `on` opens an interval,
        each `off` closes it. Returns the list of intervals plus a
        running total of ON-time. An open interval (last action was
        `on`, no closing `off` yet) is closed at `now` so the user
        can see "currently ON for Xh."

        Robust to:
          - Repeated `on` firings without a closing `off` (consolidates
            to a single interval starting at the first one).
          - Repeated `off` firings without a preceding `on` (drops them
            silently — nothing was on to close).
          - The first event in the window being `off` (drops it; we
            don't know how long it had been on before our window).
        """
        if not kasa_host:
            return []
        firings = list(reversed(
            self.list_automation_firings(
                kasa_host=kasa_host, days=days, limit=10000,
            )
        ))  # oldest first for state-machine pairing
        intervals: list[dict] = []
        open_at: int | None = None
        open_rule: dict | None = None
        for f in firings:
            if f["action"] == "on" and open_at is None:
                open_at = f["fired_at"]
                open_rule = f
            elif f["action"] == "off" and open_at is not None:
                intervals.append({
                    "on_at": open_at,
                    "off_at": f["fired_at"],
                    "duration_s": f["fired_at"] - open_at,
                    "opened_by_rule_id": open_rule["rule_id"] if open_rule else None,
                    "opened_by_rule_name": open_rule["rule_name"] if open_rule else None,
                    "closed_by_rule_id": f["rule_id"],
                    "closed_by_rule_name": f["rule_name"],
                    "open": False,
                })
                open_at = None
                open_rule = None
            # repeated on/off without a complement → ignore (state stays as-is)
        if open_at is not None:
            now = int(time.time())
            intervals.append({
                "on_at": open_at,
                "off_at": None,
                "duration_s": now - open_at,
                "opened_by_rule_id": open_rule["rule_id"] if open_rule else None,
                "opened_by_rule_name": open_rule["rule_name"] if open_rule else None,
                "closed_by_rule_id": None,
                "closed_by_rule_name": None,
                "open": True,
            })
        return intervals
