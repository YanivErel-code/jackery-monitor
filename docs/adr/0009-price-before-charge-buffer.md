# Charging price before the optional early-finish buffer

Date: 2026-10-23

## Context

The October 23 14:45 Pacific decision scheduled charging until 15:00, again at
21:00–22:00, then 00:00–06:00. A frozen-input replay showed that 00:00–07:00 could
meet the same sunrise target and reserve without the expensive evening hour.
The one-hour early-finish preference was the first element of the candidate
sort key, so a non-buffer expensive slot beat a cheaper final-hour slot.

The owner requested that cheaper electricity take precedence over this optional
buffer when the reserve and sunrise target can still be met.

## Decision

Keep reserve/path repair and the sunrise target checks unchanged. When ranking
eligible useful candidates, compare marginal modeled price per SOC gain first,
the early-finish preference second, then the existing later-start tie-break.
Retain the existing nine-decimal marginal-cost rounding for deterministic ties.

This supersedes only the ordering of the price and early-finish preferences in
ADR 0002. At equal cost effectiveness the planner still prefers to finish one
hour early. The final pre-sunrise hour is available when it avoids unnecessary
costlier charging. Do not change the target, reserve, learned charging power,
duration budget, LG/manual constraints, trajectory validation or retained active
interval policy from ADR 0007.

## Consequences and limits

There is no hard-coded 15:00 stop or midnight start. The saved time-of-use plan
drives prices, and earlier expensive charging remains available if needed to
protect reserve or reach the sunrise target. Starting at midnight is not
mandatory if fewer or later cheap hours suffice. Plans continue to recalculate.

This intentionally spends less on unnecessary buffer time, but may finish closer
to sunrise and leave less time to recover from an unexpected load, power or
weather change. Model feasibility is not a guarantee of actual SOC. The bounded
greedy search still does not claim globally optimal total cost.

## Verification

Tests reproduce a cheaper final hour losing to an expensive pre-buffer hour,
preserve the early finish at equal marginal cost, require expensive hours for
reserve/target constraints, respect holds and exhausted budgets, and retain an
existing cheap interval only to its original 15:00 boundary. Existing full-path,
partial-hour and ON/OFF stability tests remain unchanged.
