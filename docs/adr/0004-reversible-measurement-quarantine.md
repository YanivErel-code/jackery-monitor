# Reversible frozen-measurement quarantine and durable evidence

Status: Accepted

## Context

The October 19 historical audit reproduced three gaps in ADR 0003: pack
snapshots were persisted independently of the frozen flag, legacy SOC rules
still received flagged measurements, and the detection lead-up remained in
analytics. Recreating the bridge also erased in-memory evidence and backoff.

Four strong older Explorer freeze intervals span 31 hours 18 minutes. There
are also weaker single-unit SOC plateaus and SOC recalibration events. Those
are not equivalent evidence and must not be indiscriminately deleted.

## Decision

1. Apply the same device-scoped frozen flag to pack ingestion and independent
   SOC-rule inputs, including the active-device fallback. Healthy other devices
   continue operating. Pausing decisions does not blindly toggle existing plugs.
2. Atomically checkpoint detector state to a private file adjacent to the cloud
   credential file. Save suspicion/recovery transitions immediately and ordinary
   evidence at most once per minute, with a final graceful-shutdown checkpoint.
   Validate restored state and retain confirmed suspicion across observation
   gaps. Unconfirmed evidence still requires continuous observations. An invalid
   checkpoint fails closed until measurement progress; account reset clears it.
3. On confirmation, archive and exclude the device's unchanged interval from
   active samples, pack history, and overlapping forecast issuance/actual windows.
   Round to minute boundaries conservatively; forecast issuance is only hourly,
   so boundary-hour exclusion can include valid forecasts with uncertain provenance.
4. Preserve original rows, valid earlier sunset forecasts, settings, learned/user
   configuration and controller audit events. Clear only affected derived summary
   fields. Missing observations remain unknown; never interpolate or insert zeros.
5. Reject late writes and actual backfills into a persistent incident. Reset the
   energy integrator and invalidate affected dashboard/history/fit caches after
   exclusion so recovery does not integrate across the unknown gap.
6. Provide selective, transactional restoration with collision and overlapping-
   incident guards. Never restore an entire old database over newer observations.

This explicitly supersedes ADR 0003's **“Do not retrospectively rewrite historical
telemetry”** decision: active analytics now exclude confirmed freeze lead-up,
but original evidence is preserved reversibly, not rewritten into invented truth.
It also supersedes the requirement to reaccumulate confirmed suspicion after a
restart. No security or authentication control is relaxed.

## Data dictionary

| Table | Key | Purpose |
|---|---|---|
| `measurement_incidents` | device + minute-aligned start | Exclusive end, detection time, reason, quarantined/restored status |
| `measurement_quarantine` | incident + source table + original key | Exact original JSON row; summary-only original field patch |

`samples`, `battery_packs`, `forecast_predictions` and `daily_solar_summary` are
the only allowed archive/restore targets. Controller decisions and automation
firings are deliberately outside that allowlist. The tables are created through
the existing idempotent SQLite initialization path.

## Consequences and limits

Detection remains conservative: at least 30 minutes and 2% of catalog system
capacity as reported net discharge, about 66 minutes for 551 W / 30,240 Wh.
Data can appear provisionally before confirmation, but is then excluded from
analytics with its originals preserved. Normal power-changing plateaus, charging
and BMS recalibration are separate quality classes; no claim is made that this
guard diagnoses all bad telemetry. SOC-only anomalies do not justify deleting
otherwise credible energy measurements. A cloud reconnect cannot restore the
device's disconnected Wi-Fi.

Quarantine increases storage modestly and makes affected totals partial. JSON
archives retain original column values; a later incompatible schema or changed
record stops automatic restoration for review. Persistent state contains device
measurement metadata, never cloud credentials or a copied authentication token.

## Alternatives rejected

- Deleting originals or setting energy to zero: destroys evidence or invents usage.
- A flag ignored by existing SQL sums: leaves contaminated analytics unchanged.
- A whole-database rollback: discards newer, valid measurements.
- Merely restarting or accepting HTTP success: loses evidence or repeats false freshness.
- Quarantining every SOC step: erases credible power during BMS recalibration.

## Verification

Regression tests reproduce the pack and independent-rule bypasses before the
fix, exercise transactional archive/restore conflicts and other-device isolation,
check late-write/backfill rejection, integrator recovery, cache invalidation and
durable state. Before historical production cleanup, use SQLite online backup,
rehearse against a disposable copy, verify every original table after selective
rollback, then reapply and check live telemetry and unchanged configuration.
