# Generic LG power evidence, not cycle-phase allowlists

Date: 2026-10-23

## Context

The washer moved through `RINSING`, `SPINNING`, `END` and `POWER_OFF`.
PR #24 added missing running phases but still reported `END` as an unknown-state
monitoring failure. Maintaining a list of every manufacturer's cycle names is
not a durable way to reserve a mapped appliance's 220/240 V output.

## Decision

Classify the protocol's power/status fields, not a phase vocabulary. Preserve
valid phase text for display. Any valid non-OFF string in a recognized
`runState.currentState`, `operation.*OperationMode`, `power` or `powerState`
field conservatively means ON / not confirmed OFF. Explicit `OFF`, `POWER_OFF`
and boolean false in a power field provide OFF evidence. Nonempty strings are
bounded and control-character-free; malformed/missing fields stay unknown.
Boolean cycle phases are invalid, not OFF evidence.

This does not claim that the motor or heater is running. In particular, `END`
keeps a mapped appliance reserved until it reports OFF or fresh disconnected
status. A valid unfamiliar label is not a monitoring failure. Never infer OFF
from a new phase, a zero timer or an estimated finish passing. A non-OFF
compartment overrides another compartment's OFF reading.

Remove the separate finish-time phase allowlist too. A valid remaining-time
report becomes a provisional finish estimate only after observed countdown
progress in every powered-on compartment. Track each compartment independently;
one moving timer cannot certify a second stalled or reset timer. First readings,
counter increases/resets, gaps beyond the 75-second freshness limit and missing
timers have no confirmed finish. An unchanged countdown after a phase change
waits for new progress, without interpreting the phase. Progress expires after
120 seconds if the counter stops decreasing. Recalculate on subsequent evidence
and carry evidence into saved decision inputs.

This supersedes the phase-name recognition/timing assumptions in ADR 0005 and
PR #24, not their monitoring-availability policy. Authentication failures,
missing/malformed/stale data and API failures still warn without blocking
charging. Manual reservations remain effective. Fresh explicit LG disconnected
responses remain assumed OFF under the owner's existing Wi-Fi-off policy.
No appliance is mapped automatically and no LG control permissions are added.

## Consequences and limits

New phase names no longer need a code update. A connected appliance can remain
reserved while idle or displaying `END`; this conservatism is intentional until
OFF evidence arrives. Monitoring failure can still permit charging while an
appliance is powered, as already documented in ADR 0005; hardware protection or
a manual reservation is required for stronger guarantees.

A moving countdown is evidence, not proof of future completion. A pause may
retain its provisional estimate briefly until progress expires, and LG can
revise the counter. Actual charging remains subject to the live ON/OFF interlock.
After restart or reconnection, the first countdown is displayed but no finish
time is used until fresh progress is observed. No controller targets, charging
power assumptions or duration limits change.

## Verification

Tests replay END-to-OFF, many arbitrary future phases, malformed/empty/oversized
values, boolean power versus cycle fields, mapped/unmapped devices, counter
decrements/increases/stalls, stale gaps and multi-compartment countdowns.
Existing real API-error, stale-state, disconnected, manual-hold and pre-command
recheck tests remain. UI tests preserve phase text safely, distinguish ON from
missing status, and display finish estimates only with current countdown evidence.
