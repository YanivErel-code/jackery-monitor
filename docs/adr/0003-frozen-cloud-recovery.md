# Frozen cloud measurements and bounded session recovery

Status: Proposed

## Context

On October 19 the monitor repeatedly received 88% main SOC, 551 W output,
zero input, and identical SOCs for all five expansion packs. Successful HTTP
receipts advanced the existing cloud/SOC timestamps, so the ten-minute cloud
watchdog considered this healthy indefinitely. Another device on the same
account continued changing. Restarting both monitor and bridge did not, by
itself, establish that the battery's measurements had recovered.

Transport liveness is not measurement liveness. Conversely, constant SOC at
idle or during a charging plateau is normal, and a single rounded SOC value
cannot prove a fault. Automatically reconnecting every identical packet would
create a login storm and interfere with the Jackery phone app.

## Decision

Maintain per-device evidence separately from HTTP receipt timestamps. Observe
unchanged main SOC/input/output and complete known expansion-pack identities
and SOCs. Only suspect an anomaly after 30 minutes of continuous observations
whose conservative reported net discharge exceeds 2% of catalog system capacity.
Use the existing model catalog and expansion-pack capacity functions. Do not
apply portable power accounting to Home systems or unknown models.

The existing cloud watchdog closes and recreates its client/task for suspect
devices even when HTTP succeeds. Preserve suspicion and evidence across that
recovery; only changed measurements clear it. Back off subsequent frozen-data
retries from 30 minutes to four hours, honoring pause/contention suppression.
Recovery sends no battery-output command and changes no charging target.

Expose per-device measurement health, display a persistent error, and stop
using suspect values for new energy/chart samples, forecasts, and smart/solar
charge decisions. Do not retrospectively rewrite historical telemetry.

## Consequences

Detection is intentionally delayed by SOC quantization and system capacity.
551 W on a 30,240 Wh rig takes about 66 minutes to exceed the energy guard.
A reset process must accumulate evidence again; restarting is not a durable
substitute for the watchdog. Variable-power faults, net-charging freezes,
and unknown capacity are not diagnosed by this conservative first guard.

The suspicion is an anomaly, not proof of device failure, network failure,
or exact physical energy loss. If a fresh cloud session still returns old
measurements, retain the warning rather than claiming successful recovery.

## Alternatives rejected

- Receipt-age-only monitoring: misses successful cached replies.
- Identical SOC alone: falsely diagnoses idle and full batteries.
- Account-global progress: hides one frozen device behind another healthy one.
- Clearing the warning on login/HTTP success: reproduces false freshness.
- Repeated process/container restarts: disrupts unrelated work and loses evidence.
