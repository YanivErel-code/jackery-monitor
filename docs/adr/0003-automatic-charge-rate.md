# Automatic sustained AC charging rate

Date: 2026-10-22

## Context

The Smart charge form exposed a manual `max_charge_w` while a separate Device
parameter fit used hourly 95th-percentile input. Controller decisions still used
the manual value. Partial charging hours dilute hourly averages, historic peaks
may belong to a different charging mode, and saving unrelated form settings can
accidentally turn a prefilled estimate into a permanent override.

## Decision

Supersede ADR 0002's manual charging-power assumption, not its target, reserve,
duration or LG policies. Use one automatic rate for live plans, status, Device
parameters and the current-policy backtest power assumption. Use 1,000 W until
reliable device-specific measurements are available; ignore legacy manual rate
values and reject new Device-tab overrides. The UI retains an editable reserve
and replaces Max charge W with a read-only estimate and provenance.

Read completed minute buckets from the preceding 14 days. Require receipt
provenance within two minutes of the bucket, valid powers, main SOC below 90%,
and solar at most 50 W. Prefer classified AC; unclassified input additionally
requires a known local clock in the 21:00–06:00 night band. UTC is valid. Known
frozen measurements are already quarantined and excluded from the sample table;
quarantine invalidates the learning cache.

Use the newest six-consecutive-minute window with at least five readings within
15% of its median, taking the median of those consistent values. Gaps and
duplicate minutes cannot manufacture coverage. A new sustained rate can move
the estimate up or down; an isolated spike cannot set it. Cache for at most
60 seconds, retaining the supporting sample timestamps for inspection. Include
rate/source evidence in each new controller input snapshot. A history-read
failure visibly reports the 1,000 W fallback rather than claiming a measured rate.

## Consequences and limits

This estimates input; it never sets the Jackery's hardware power or raises the
duration limit. A six-minute window is a responsiveness/noise trade-off, not a
guarantee of future charging power. Cloud source classification can still be
wrong. Near-zero solar and fresh receipt times are filters, not proof of a
correct physical reading. Undetected freezes remain a measurement limitation.
Cold-start 1,000 W may overestimate a slower unit until it is observed. A learned
window remains usable for up to 14 days; no qualifying window means default.

The existing current-policy backtest uses the current learned rate for its
counterfactual, not a reconstruction of learning at every historical decision.
Historical decisions and their saved input rates remain unchanged.

## Alternatives

- Keep manual power: rejected at the user's request and because it drifts.
- Reuse hourly peak fitting: rejected because mixed ON/OFF hours and old modes
  are not the sustained power available to a new charging interval.
- Learn from one live sample: rejected because startup, taper and cloud spikes
  would immediately change the forecast without sustained evidence.

## Verification

Tests cover cold start, increases/decreases, source/freshness/taper/solar gates,
spikes, duplicate/gapped/future/stale evidence, per-device isolation, cache
expiry, shared UI/planner/snapshot values and rejection of manual overrides.
