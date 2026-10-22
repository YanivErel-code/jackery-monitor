# Reserve-aware smart-charge schedules

Date: 2026-10-22

## Problem

The October 22 controller deferred charging to 21:00/23:00 even though its own
no-grid forecast approached depletion before that window. It sized charging from
`target - baseline_sunrise_soc`, then selected cheap hours. Once the forecast
reached its physical 0% clamp, further unserved load disappeared from that
endpoint calculation. Checking the sunrise endpoint also missed an earlier SOC
trough. Repeating that calculation every five minutes did not repair the plan.

## Decision

Preserve the honest no-grid forecast. Separately simulate proposed ON intervals
from the current, capacity-weighted system SOC through sunrise, using the existing
forecaster's efficiency, drain-band and ceiling behavior.

1. Require continuous, finite interval inputs through sunrise for live control.
2. Protect a configurable `reserve_soc_pct` (default 10%, range 0–95%). When
   already below reserve or physically constrained by a hold, pursue the best
   available recovery and flag the remaining shortfall.
3. Check the sunrise SOC of the proposed schedule, not just its grid-energy sum.
   Preserve unserved-demand evidence when reporting the no-grid energy deficit.
4. Prefer lower-cost feasible windows and later equal-cost slots. Prefer finishing
   one hour early, but allow a final-hour top-up when needed. This bounded greedy
   search does not claim a globally optimal price or prove that no other feasible
   schedule exists when it fails to find one.
5. Use actual interval overlap: a remaining 15 minutes is not a full charging
   hour. Persist exact intervals and conditional SOC/minimum projections.
6. Respect LG finish constraints and manual/active holds. Timers never authorize
   charging while an active-state/manual hold still applies. Monitoring-error
   fail-open policy remains unchanged.
7. Subtract already-commanded ON time from the configured duration budget. Query
   successful active ON/OFF decisions, including the preceding state before the
   cycle boundary, in the 24h period before the forecast sunrise. Repeated ON
   decisions do not reset the counter; test/failed commands do not count.
8. Recalculate on LG/config changes, planned interval boundaries and at least
   every five minutes. Do not silently increase target, power or duration settings.

`plan_feasible` describes this computed schedule under its model assumptions.
Warnings report reserve/target shortfalls rather than disguising them as a safe
wait for cheaper electricity. They remain visible in the persisted decision
reason as well as the detailed plan UI.

## Compatibility and limits

- Legacy start-stamped snapshots remain readable via a compatibility policy.
  Their plans are explicitly unvalidated and cannot execute in the live server.
- The duration counter is a conservative estimate of commanded ON time, not an
  AC meter. Unobserved manual OFF periods may overcount; independent/manual ON
  paths are not included. It is not a physical electrical protection device.
- State changes between a decision and its Kasa command, cloud latency, missing
  data and unsuccessful OFF commands can defeat timing expectations. Existing
  LG checks remain at the command boundary and in the independent watchdog.
- The scheduler respects configured charging power; it does not automatically
  retune power from coarse, partially charged hourly buckets. If actual input
  is lower, forecasts remain optimistic. Users should review the measured-rate
  hint and infeasibility warnings instead of assuming the target is guaranteed.
- The saved October 22 incident is reproduced with an identifier/credential-free
  fixture. With its existing power and duration budget, the corrected planner
  requests earlier charging and reports the remaining infeasibility instead of
  promising that a late window will meet 30%.

## Validation

Regression coverage includes clipped depletion, an intermediate trough despite a
recovered sunrise endpoint, partial-hour accounting, insufficient power/duration,
charge ceilings, reserve recovery, LG/manual constraints, restart-safe commanded
duration, malformed forecasts and independent energy-balance checks. Dashboard
tests distinguish baseline/planned SOC, render partial intervals and escape
warning content. Production is not used for fault injection or controller tests.
