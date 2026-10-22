# Non-blocking LG monitoring failures with visible warnings

Date: 2026-10-22

Status: Accepted — explicitly requested by the owner

## Context

LG appliances can disconnect when powered off, and LG cloud or token failures
can leave their actual state unknown. The previous LG policy held charging OFF
unless every selected appliance was freshly confirmed OFF or LG-disconnected.
The owner requested that monitoring problems be visible in Automation without
preventing grid charging, prioritizing charging availability during LG outages.

## Decision

Only a fresh active reading from a mapped appliance or an enabled manual 240 V
reservation blocks the charging plug. A fresh LG status error `1222` remains
assumed OFF. Other failures remain unknown rather than being relabelled OFF:
missing/expired credentials, request failures, unrecognized states, stale data,
missing selected devices and empty selections warn without imposing a hold.

Every ON attempt still performs the LG refresh and rechecks active/manual holds.
A refresh failure records a sanitized monitoring error but does not reject the
ON command. Planning ignores unavailable devices' timing constraints, while
preserving constraints from other confirmed active devices. Manual holds remain
effective even with missing credentials or failed requests.

Automation displays a persistent, non-dismissed warning across all its sub-tabs;
an independent red Automation navigation dot makes it discoverable from other
tabs. The dashboard checks the cached monitoring status on startup and every ten
seconds while visible; this does not add LG cloud requests. Both clear when
polling recovers. The warning distinguishes monitoring failures
from measured OFF states. Decision inputs include warnings and a degraded flag.

The live dryer test showed `INITIAL` when the owner powered it on. Recognize
that specific state as active, not as an unknown-state monitoring failure.

## Consequences and alternatives

This explicitly replaces the former LG **fail-closed-on-unavailable-state**
operational rule. An appliance Wi-Fi or monitoring outage can permit charging
while a mapped appliance is running; fresh active holds also expire when their
status becomes stale. Users requiring uninterrupted 240 V availability must
use manual reservation or independent hardware protection. This software guard
cannot guarantee it.

Keeping the previous fail-closed policy was rejected because an LG outage could
prevent needed charging. Treating errors as measured OFF was also rejected:
unknown status and a visible warning preserve the distinction honestly.

Application authentication, LG credential validation, token encryption and
redaction, per-Jackery mappings and other inverter safeguards are unchanged.
No security/CIA-floor control is waived by this operational fallback.
