# Overnight-first smart charging

Date: 2026-10-24

## Context

The October 24 morning decisions charged before 09:00 and again at 06:00–07:00
the next day, despite midnight and morning electricity having the same price.
SOC-dependent drain made earlier charging appear more cost-effective, while the
latest-start tie-break deferred overnight demand until just before sunrise.
Retaining the current interval could then keep the daytime charge on even when
a fresh plan could wait. The owner requested overnight charging with midnight
preferred over daytime or a last-hour start.

## Decision

Supersede ADR 0009's candidate ordering and ADR 0007's reuse of decisions from a
different scheduling policy. Keep the same full-path simulation and constraints.

1. Define the preferred overnight window as local midnight through the target
   sunrise, using the forecast's saved UTC offset. Split a forecast slot at local
   midnight when necessary, including fractional-hour offsets.
2. When repairing reserve or sunrise demand, prefer eligible overnight slots.
   Within that window, rank lower tariff rates first, then earlier starts. At
   equal overnight rates this starts at midnight, or now if already overnight.
   A partial duration budget also starts at the beginning of the selected slot.
3. Allow earlier slots when the computed path needs them. Remove unnecessary
   earlier intervals before pruning overnight intervals; at equal overnight
   rates, prune later intervals first. If the preferred search cannot validate a
   feasible plan, retry the previous cost-first ranking and use that alternative
   only if it is feasible. Report this relaxation in the plan's reason.
4. Save `overnight_first_v1` in the plan and decision snapshot. Recover a retained
   interval only from the same policy. Legacy decisions get a fresh plan rather
   than carrying daytime charging into the new policy. Keep all other retention
   checks: recent successful ON, matching device/settings/sunrise cycle, validated
   trajectory, original endpoint and remaining budget. New-policy earlier fallback
   intervals may finish their bounded original interval to avoid chatter.

Recalculate the full SOC path on every normal five-minute evaluation. Holds,
appliance availability, charge-not-before constraints, remaining commanded-ON
budget, learned gross AC power, concurrent load, efficiency, calibration and
charge ceilings retain their existing meaning. Do not alter the owner's target,
reserve, charging power or tariff settings.

## Consequences and limits

This deliberately prioritizes overnight operation over globally cheapest energy
or the smallest modeled charging duration. Cheaper overnight rates may still
delay the start beyond midnight. No charging is scheduled when the baseline
already covers both goals. Earlier charging remains possible when the model
requires it, and unmet constraints remain visible as warnings.

The search remains bounded and greedy, including its fallback; it does not prove
that every feasible schedule has been found or that earlier charging is globally
unavoidable. Model feasibility does not guarantee actual SOC. The midnight
boundary uses the existing fixed forecast offset, not a new daylight-saving-time
model. Valid same-policy commitments can retain an earlier interval until its
original end, at most one forecast hour; they are not an unconditional minimum
ON time and existing holds or a fully covered no-grid path still release them.

## Verification

Regressions cover midnight starts, cheaper daytime deferral, partial-hour budgets,
current overnight time, fractional-hour time zones, earlier reserve/target repair,
nonlinear-drain fallback, holds and exhausted budgets. Snapshot tests reject
legacy/unknown policies and persist the current marker. Existing tariff, reserve,
concurrent-load and anti-flapping tests exercise the unchanged constraints.
