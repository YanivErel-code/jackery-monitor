# Controller settings auto-save

Date: 2026-10-22

## Context

The Controllers tab required Save separately for Smart charge and Excess
diversion. Users expect edited settings, including LG mappings and manual
reservations, to persist without a second action. Moving writes onto input
events introduces incomplete numeric values, overlapping requests and device
switches while a write or configuration reload is pending.

## Decision

Use the existing authenticated configuration endpoints. Each controller keeps
an in-memory, serial-number-scoped queue, with at most one write in flight per
device. Toggle/selection changes queue immediately; numeric input coalesces for
500 ms, and committing the field flushes immediately. Validate the complete
form, including the diversion floor/restart relationship. Never submit backtest
parameters as settings. Do not change targets, limits or charging policies.

Capture the device and configuration when editing, not when sending. Ignore
late reloads after a newer edit or device switch. Acknowledge only the latest
revision and check the server retained the submitted values. Keep failed drafts
visible and offer an explicit Retry instead of silently retrying control-related
changes indefinitely. A 20-second request timeout reports an unconfirmed save:
the server may already have accepted it. Warn before leaving with pending edits;
the warning is best effort and browser memory is not durable draft storage.

## Consequences

No explicit Save remains in these two forms. Manual holds are still best-effort
software protection: saving immediately does not promise an instantaneous plug
command. Controller checks and LG policy are unchanged. Evaluate and Backtest
stay disabled until the current form has loaded and edited settings are confirmed
saved, so they cannot silently run an older configuration. Credential entry and
other tabs retain their explicit actions. Concurrent edits
from separate browser sessions remain last-writer-wins as with the existing API.

Posting every keystroke was rejected because intermediate numbers can be valid
but unintended. Re-fetching the entire form after each successful write was
rejected because it can overwrite a newer unsaved edit.
