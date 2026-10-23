# Stable smart-charge replanning

Date: 2026-10-22

## Context

The reserve-aware planner recomputed all intervals on every tick. It compared
candidate cost against SOC gains rounded to the display's 0.1 percentage points.
Advancing the clock changed the length of the current partial hour and moved
those quantization boundaries. October 22 observed loads reproduced ON/OFF/ON
decisions with otherwise fixed inputs. The live decision history also alternated
ON and OFF, without retaining an already-started charging interval.

## Decision

Keep the existing physics and calibrated inputs. Add an opt-in unrounded SOC
simulation for the scheduler only; display forecasts keep their existing format.
Use full precision for path/goal checks and candidate gains, with a negligible
numeric tolerance and deterministic cost/gain ties. Round only reported SOC.

Extend ADR 0002's every-tick recalculation with a bounded active-interval seed:

1. Read the latest successful active ON/OFF command for this device within ten
   minutes. Only an ON command from the same sunrise cycle can authorize reuse.
2. Require a validated input snapshot, matching capacity, plug, mode, sunrise
   target, reserve and duration setting. Its interval must cover both the saved
   decision and now, and be no longer than an hour. Missing/old-format history
   falls back to a fresh plan; no migration or new state file is required.
3. Keep only the remaining part of that interval, bounded by the current forecast
   hour and remaining commanded-ON budget. Seed it before optimizing other slots,
   without overlap or double-counting. Repeated replans do not slide its endpoint.
4. Recalculate the entire trajectory and later slots every time. Drop retention
   when the no-grid path already covers both reserve and sunrise target. LG/manual
   holds, later availability constraints, invalid/stale inputs and an exhausted
   budget remain authoritative. User setting changes invalidate reuse. Automatic
   learned-power refinements revalidate the interval with the new power instead
   of discarding it merely because the fit changed.

Record plug identity in new decision snapshots and expose the retained endpoint
as `retained_charge_until_ts` in the plan/projection for replay and diagnosis.
One-shot historical backtests without such context still construct fresh plans.

## Consequences

The controller no longer discards an active interval just to reshuffle marginal
costs every few minutes. There is no unconditional minimum-ON delay: safety and
goal checks can stop charging early. Retention is a best-effort commanded state,
not proof of AC energy delivery or a hardware interlock. A plug reporting ON
while Jackery reports zero input remains a separate issue, not fixed by this PR.

The earlier October 22 regression now checks that charging begins before the
first predicted reserve breach rather than demanding ON immediately at 15:15.
With full precision, that snapshot remains above reserve until charging starts
at 18:00, unlike the old 21:00 deferral. Its insufficient duration budget remains
explicitly warned.
No calibration, target, duration setting, charging-power assumption or LG policy
is retuned. Long outages and materially changed forecasts still replan normally;
this does not guarantee zero switching under genuinely changing constraints.
