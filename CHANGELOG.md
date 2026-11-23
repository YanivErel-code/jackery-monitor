# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project tries to follow [Semantic Versioning](https://semver.org/).
Versions before 1.0 allowed breaking changes in minor releases. Operational
limitations still apply; a major version is not a hardware safety guarantee.

## [Unreleased]

### Added
- Alerts as push notifications to the Jackery iOS app: battery low, on
  course to fall below the reserve, plug on but not charging, AC output
  tripped, charging held by laundry for a long time, lost contact with the
  battery, and backup failed. Each alert is sent once when its condition
  starts and re-arms when it clears; one that could not be delivered is
  tried again on the next pass. Set up under Settings → Notifications: the Apple
  push key (stored encrypted), the registered phones, per-alert toggles
  and thresholds, and a test button.
- A Live Activity on the phone's lock screen while the battery charges
  from the grid or charging is on hold, started, updated and ended by the
  monitor. It is redrawn when the level, status or end time changes, or
  when solar, grid or home power starts, stops or moves by 50 W and a tenth
  (power changes at most every two minutes, to spare the phone's battery).
- The iOS app's widgets are asked to refresh by push when the charging
  status changes, and for a change of battery level at most every 15
  minutes.
- `/api/app/summary`: compact battery, live power and headline for the iOS
  app's widgets.
- `/app/handoff` for the iOS app: signs in through Cloudflare Access in
  the system browser, where Google sign-in is allowed, and hands the
  result back to the app. Answers only requests that carry a valid Access
  assertion.
- V2 dashboard at `/v2`, alongside the classic view. Live opens with a
  plain-language status line ("Not charging. Next charge starts at 12 AM.",
  "Charging is on hold while the dryer runs.", "No grid charging needed."),
  then battery, live power, today's grid cost by rate tier, and one chart of
  measured and projected battery level over the rate bands. Charging shows the
  next charge, protections, a 24-hour rate strip, the last day's changes and
  the settings in everyday wording. Light and dark themes; phone layout with a
  bottom bar. History, Rules & plugs and Settings still open in the classic view.
- V2 is the default at `/`. Settings → Default view (`default_ui_version`,
  saved on the server) switches the default to the classic view; `/v2` and
  `/classic` always open theirs, and each links to the other.
- V2's History, Rules & plugs and Settings pages keep the V2 menu: they frame
  the classic screens (`/classic?embed=1&tabs=…`) without the classic top bar,
  restyled with V2's colours and following its light/dark choice.
- V2 History page, built natively: totals for today / 7 days / 30 days /
  lifetime, energy by day (solar, grid, used) with 14/30/90-day ranges and
  records, a day-by-day table, the 5-day no-charge battery forecast, saved
  sunrise forecasts against what happened, and a per-device table.

- V2 Rules & plugs page, built natively: grid charging and excess-solar
  settings in everyday wording with explicit Save buttons, smart plugs (status,
  switch, add, find, remove, Kasa account), battery rules, and AI insights.
  Switching a controller's plug by hand warns that it ignores the laundry hold.

- V2 Forecast page, between Charging and History: solar expected today
  (measured so far plus still to come), tomorrow, the best day ahead and the
  5-day total; hourly solar power for the next 5 days with a sky summary per
  day; measured solar for the last week beside the forecast days. The battery
  forecast and the sunrise-forecast comparison moved here from History.
- V2 Live page shows the animated power flow again: solar and grid into the
  battery, battery to the home, with a dot travelling along each line that is
  carrying power (faster for more watts; still when reduced motion is set).
- V2 Settings page, built natively, in seven sections: General (default view,
  location, runtime numbers), Rates (flat or time-of-day editor with presets),
  AI (provider, keys, models), Laundry (LG status and account), Backup (status,
  history, test, back up now), Device (info, battery size, AC auto recovery,
  learned values, accounts) and Logs. Changing the backup destination and
  restoring remain on the classic backup screen.
- The V2 shell and the classic shell stamp every script and stylesheet URL with
  a build id, and an open V2 page reloads itself once when the server reports a
  different build.

### Fixed
- The lock-screen battery level could read one point below the dashboard
  (26.5% showed as 26% on one and 27% on the other).
- V2 chart axis labels were cut off at the left edge for values of 10 kWh and
  up, and axis steps could be awkward fractions.
- A V2 page left open across a deploy kept running old code against the new
  server (History opened Live; tabs did nothing). See the build id above.
- `/?ui=v1`, the classic-tab link used by the first V2 release, opens the
  classic view again.
- Service worker offline fallback no longer hands the default view's shell to
  the classic frame or to script requests.

### Added
- `GET /api/v2/overview` builds that page's data from the latest saved
  smart-charge decision. The projected battery curve re-integrates the saved
  no-charge forecast plus planned charging and is labelled approximate.

### Fixed
- Summarise forecast accuracy only over prediction pairs with no grid charging
  between the forecast and its target. The stored forecast is the no-charge
  baseline, so charged pairs measured the charging, not the model. Each pair
  now carries `grid_charge_wh` and `charge_free`; excluded pairs are counted.
  The advisor summary scores about a week of pairs instead of the 500 newest
  (roughly four hours of targets) and reports the window and signed bias.
- Hold a started smart-charge run for at least 45 minutes while the current
  rate is the cheapest remaining tier and the target is not already covered.
  Off-peak top-ups no longer end with their 15-minute nowcast slot and restart
  minutes later. Holds, the duration budget and dearer-rate hours are unchanged.
- Verify that a commanded charge produces AC input. Record the plug read-back,
  mark contradicted commands as not executed, and warn once per run when an ON
  command yields no charging.
- Add a two-hour SOC/power reconciliation that catches slow drift the short
  check allows, and automatically exclude intervals where SOC fell more than
  modeled from drain learning without overriding restored exclusions.
- Project short-horizon demand from fresh metered five/fifteen-minute averages
  while an appliance is running, including an explicit planning allowance and
  exact expiry boundary. Warn when reserve requires pausing/deferring loads;
  preserve LG holds, charging settings and hourly prediction-accuracy targets.
- Recognize repeated severe same-day solar shortfalls rather than rejecting
  their yield ratios as outliers. Apply a temporary, bounded site-yield correction
  using consecutive qualified hours, retain curtailment/freshness gates, and
  replace old shortfalls when new evidence shows recovery. Do not retune the
  global solar coefficient or double-attenuate clouds.
- Expose bounded SOC/power energy-reconciliation diagnostics using fresh,
  source-matched, pack-identified endpoints and complete uncharged meter minutes.
  Preserve raw SOC and existing low-SOC drain-learning exclusions; label an
  unexplained residual as unresolved rather than inventing physical losses.
- Correct confirmed battery-side AC metering: fit complete AC hours without
  subtracting grid-bypassed output, and simulate charging without battery drain
  from bypassed loads. Cap battery charging at the configured shared outlet
  limit minus household output; include total wall power in schedule cost ranking.
  Keep unconfirmed devices and solar-only accounting unchanged, preserve metering
  settings across dashboard saves, and snapshot the boundary for advisor/replay.
- Exclude unresolved energy-reconciliation intervals from SOC-based efficiency
  and drain learning without deleting raw energy, SOC, forecasts or control audit.
  Persist reversible per-device exclusions and retain rows to prevent fits from
  bridging an excluded interval.
- Prefer overnight smart charging from local midnight to sunrise, with cheaper
  overnight hours first and earliest starts at equal rates. Keep earlier charging
  available for reserve/target needs and retry cost-first if the preferred plan
  is infeasible. Replan old-policy commitments while retaining bounded stability
  for new validated intervals; preserve holds, learned power and duration limits.
- Prefer lower-cost useful smart-charge intervals over the optional one-hour
  pre-sunrise finish buffer. Keep the early-finish preference for equal marginal
  cost, while preserving reserve/target validation, holds, duration limits and
  committed-interval stability. Expensive charging remains available when needed.
- Replace LG cycle-phase allowlists with generic fresh ON/OFF evidence. Show any
  valid reported phase, including `END` and future values, without a false
  monitoring warning; mapped appliances remain reserved until OFF/disconnected.
  Use recent countdown progress in every powered-on compartment for provisional
  finish estimates, never as authority to release a hold. Real monitoring errors
  remain visible and non-blocking; manual reservations still apply.
- Recognize the washer's LG-reported rinse, spin, refresh, drain, preparation and
  hold phases instead of flagging normal running states as monitoring failures.
  Recalculate reported cycle finish times; preparation and hold phases retain
  uncertain timing. Only explicitly mapped appliances affect charging.
- Stop short smart-charge ON/OFF cycling during rolling replans. Optimize with
  full-precision SOC rather than rounded display gains, and retain a recent
  successful ON interval through its validated original end. Recheck the full
  path and honor remaining duration, updated settings, data validity and LG
  protection; no targets, power assumptions or duration limits are increased.
- Validate the complete smart-charge SOC path before deferring to a later tariff
  window. Account for unmet demand after a no-grid forecast reaches 0%, rather
  than sizing charging from its clipped sunrise endpoint alone.
- Add a configurable whole-system reserve (default 10%), exact partial charging
  intervals, separate scheduled/minimum SOC projections and explicit infeasible-
  plan warnings without changing the user's sunrise target or charging power.
- Preserve the configured duration budget across replans/restarts using persisted
  successful active commands; wake at interval boundaries. LG/manual holds remain
  enforced, and incomplete/unvalidated trajectories cannot execute live commands.

### Changed
- Auto-save the entire Automation → Controllers tab: Smart charge, LG appliance
  protection/mappings/manual hold and Excess diversion. Remove those two Save
  buttons; keep Evaluate/Backtest and other tabs' explicit actions. Validate
  edits, debounce numeric typing, serialize per-device writes, preserve newer
  drafts during reloads and show saved/error feedback with explicit retry.
- Replace editable Max charge W with an automatic per-device sustained-input
  estimate: 1,000 W until six consecutive reliable charging minutes are available,
  then adapt to the latest stable window. Legacy manual values no longer override
  learning. Show its source read-only and clarify the editable reserve control.
- Clarify that the duration budget estimates commanded ON time in the 24h cycle
  preceding forecast sunrise, not metered energy or independent/manual charging.
  Planning is best effort, not a hardware interlock or a guarantee of SOC accuracy.

## [1.0.0] - 2026-10-22

First major release, rolling up 92 non-merge commits since the tree represented
by v0.3.0. See the [release and upgrade guide](docs/releases/v1.0.0.md).

### Added
- Generic, encrypted LG ThinQ monitoring, explicit per-Jackery appliance mappings,
  manual 240 V reservations and finish-time-aware smart-charge planning.
- Persistent Automation warnings and a separate red navigation dot for LG issues.
- Frozen-measurement detection, bounded cloud recovery, durable evidence and
  reversible historical quarantine with guarded selective restoration.
- Read-only Jackery Home EU discovery, Explorer 1000 v2 catalog support,
  per-device AC auto recovery, rescue watchdogs and pack-balance windows.
- Anthropic/OpenAI advisor selection, exact decision-input snapshots, richer
  control/pack history queries, redesigned Energy reporting and CSV export.
- Container builds publish SBOM and maximum-detail build provenance attestations.

### Changed
- Remove blanket GHCR version pruning so release image pins and associated
  attestations remain available. Registry storage can grow until a release-aware
  retention policy is implemented.
- Fresh mapped active states (including the dryer's powered-on `INITIAL`) and
  manual reservations block charging. Fresh LG disconnected reports are assumed
  OFF. LG token/API failures and unknown/stale/missing states warn but do not
  block charging. Monitoring failure can therefore allow charging while an
  appliance is running; use manual reservation when required.
- Sunrise accuracy uses original, immutable sunset forecasts rather than later
  rolling revisions. Uncaptured originals and excluded data remain unknown.
- Drain/load learning uses quality-gated multi-night evidence and preserves
  calibration provenance and range limits instead of fitting discontinuities.

### Fixed
- Source/receipt freshness, delayed HTTP versus MQTT races, stale expansion packs,
  frozen-history ingestion and forecast/actual contamination after data repair.
- Smart-charge input validation, final-hour top-ups and conflicting-rule UI sync.
- Forecast charge-ceiling accounting, partial-hour/weather alignment, diversion
  accounting, reserve protection, overload handling and manual recovery overrides.
- Redundant dashboard requests, blocking historical aggregates, cached-shell
  startup failures, device-switch races, HTML injection and secret handling.

## [0.x — accumulated development notes]

Preserved historical development log. Some foundational entries below predate
v0.3.0; the comparative v1.0.0 summary above is the release change list.

### Added
- Add encrypted, UI-configurable, read-only LG ThinQ connections for generic LG
  appliances. Map selected appliances to each Jackery's 220/240 V charging
  interlock; fresh active states and manual reservations hold grid charging OFF. Fresh LG
  device-disconnected responses are treated as assumed OFF as described below.
  Recheck all Kasa ON paths, enforce an independent OFF watchdog and preserve
  a manual 240 V reservation. Protection is opt-in; unavailable states warn without
  blocking charging, as described below.
  Use the latest selected appliance's estimated finish plus a five-minute margin
  to constrain charging windows, estimate SOC at finish and audit the timing
  inputs; a timer alone never overrides status-based charging eligibility.

### Fixed
- Show persistent LG monitoring warnings across the Automation tab for expired
  or missing tokens, API/network failures and stale/unknown/missing appliances.
  Add a separate red Automation-tab dot, updated from other tabs and cleared
  with the warning on recovery; leave the existing AI-insights indicator alone.
  Per the requested availability policy, these failures no longer block charging;
  fresh active states and manual reservations still block. Record degraded LG
  monitoring separately in decision inputs and clear the banner after recovery.
  Recognize the dryer's observed powered-on `INITIAL` state as active.
- Refresh every discovered LG appliance, not just mapped ones, so offline-as-OFF
  readings remain current across the appliance list. Charging protection and
  recalculation still depend only on each Jackery's selected appliances.
- Treat a fresh, explicit LG device-disconnected response as assumed OFF for
  appliances whose Wi-Fi powers off with them. Label and audit the assumption;
  keep authentication/API failures, missing devices and expired data unknown,
  and recheck before charging. These failures warn rather than block as described
  above. An appliance Wi-Fi outage can also permit charging.
- Keep selective restores from reintroducing forecast/actual windows covered by
  another incident, including adjacent intervals; validate archive device/key bindings.
- Persist frozen-measurement evidence across bridge restarts, stop pack-history
  persistence and independent SOC rules from using flagged readings, and
  reversibly quarantine the detection lead-up. Preserve original rows and
  controller audit events; prevent late samples, forecasts and actual backfills
  from reintroducing excluded data. Keep missing energy and sunrise SOC unknown.
- Detect per-device frozen discharge measurements even when cached HTTP
  responses keep arriving. Reconnect the cloud session with bounded backoff,
  keep unresolved readings visibly suspect, and exclude them from new energy
  samples, forecasts, and smart/solar charge decisions.
- Avoid losing consistent discharge evidence at arbitrary SOC-bin edges by
  adding a five-night, observed-range-bounded pooled fallback. Preserve
  specific-band, source-quality, freshness, and higher-drain-regime guards;
  allow validated net discharge in twilight without calibrating solar charging.
- Stop learned charge ceilings from discarding already measured SOC, and
  account for ceiling curtailment in diversion lookahead. Report the actual
  modeled baseline per interval and expose when the legacy pack prior remains.
- Preserve an immutable sunset-to-sunrise forecast, its capture time and
  target separately from rolling estimates. Keep overnight updates on the
  sunset date across midnight and measure sunrise accuracy against the
  original target's actual system SOC. Capture without smart charge or an
  open dashboard; mark historical/missed originals unknown instead of
  presenting later revisions as accurate overnight forecasts.
- Learn a quality-gated, SOC-range-specific system drain correction without
  relaxing the legacy 85% physical-fit safeguard or forcing the fixed pack
  baseline onto validated low-SOC discharge data. Require three independent
  nights, synchronized fresh receipts, complete energy coverage, and a
  conservative quantization allowance; reject charging and SOC discontinuities.
- Follow sustained stable household-load changes while retaining spike
  rejection. Exclude future/incomplete training hours, preserve diversion
  safety when SOC leaves a calibrated range, and save calibration provenance.
- Refresh automation-rule switches after smart-charge's "Disable them"
  action, confirm success, and restore the button for future conflicts.
- Prevent smart-charge startup forecasts from using 50% SOC or a generic
  capacity when real readings or model metadata are unavailable. Preserve
  valid zero SOC, require fresh main/pack receipts, and resolve inputs after
  weather fetching using the same device's saved model when needed.
- Continue partial smart-charge top-ups during the final hour when predicted
  sunrise SOC remains below target, including when current SOC is above target.

- Keep dashboard startup self-contained when cached HTML omits the request
  helper or that helper fails to load. Preserve compatibility with older
  cached scripts and refresh the service-worker shell.
- Speed up Live, Energy and Forecast by refreshing dashboard-only historical
  aggregates on read-only background connections, with a 30-second cache.
  SOC and power remain live; cold snapshots no longer wait for yearly scans.
  Forecasts query today's solar directly instead of scanning lifetime totals.
- Avoid duplicate status polling while WebSocket updates are fresh, share
  simultaneous per-device forecast requests, and defer hidden/undiscovered
  device loads. Keep stale-WebSocket recovery nonoverlapping and discard
  delayed readings/forecast work after an account or device switch.
- Pending inverter recovery cycles respect a manual AC-off command or
  disabled recovery setting instead of turning AC back on after the
  two-second cycle pause.
- Expire expansion-pack telemetry caches after two minutes, refresh through
  HTTP with bounded retries, and prevent delayed HTTP responses from replacing
  newer MQTT readings.
- Preserve main SOC and pack receipt timestamps and HTTP/MQTT sources in
  SQLite and diagnostic APIs. Cached pack values no longer become new history
  snapshots; historical provenance remains unknown.

### Added
- Advisor control-history queries and a bounded 48-hour preview covering
  smart-charge execution, solar-charge balance holds, and independent
  automation/rescue firings. Add simultaneous pack history with preserved
  HTTP/MQTT receipt provenance and explicit truncation. Guide reviews to
  distinguish measurement discontinuities, test decisions and actual AC input.
- Exact smart-charge input snapshots with SOC/source timestamps, capacity,
  weather through sunrise, model parameters, and baseline forecasts. Bound
  each snapshot to 128 KiB, expire snapshots after 30 days, and expose them in
  decision details and the advisor's `query_decision_inputs` tool.
- Explicit Jackery Home EU sign-in and read-only system discovery for
  HomePower 2000 Ultra accounts (issue #2). Show aggregate SOC, reported total
  capacity and distinct Home power fields; keep missing readings unknown.
  Portable output commands, energy integration, charging automation and
  recovery are disabled for Home systems. Hardware validation is pending.
- App-level username/password login (`auth.py`) — first-visit setup, PBKDF2
  password hash, HMAC-signed session cookies, sign-out button.
- `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, issue + PR templates,
  this `CHANGELOG.md`.
- Unit tests for `auth`, `crypto_util`, `automation`, `settings` (pytest).
- CI workflow (`ci.yml`) that runs ruff lint + pytest on every push and PR
  across Python 3.11 and 3.12.
- `pyproject.toml` with pytest + ruff config; `requirements-dev.txt`.

### Changed
- Static assets now send `Cache-Control: no-cache` so CDNs (Cloudflare in
  front of a Tunnel) revalidate every request instead of serving 4-hour-stale
  CSS after a deploy.
- Service worker drops `style.css` from the precache shell; bumped cache
  version to `v3` so existing PWAs evict stale shells on next nav.
- `[hidden]` HTML attribute now wins over flex layouts (was being defeated
  by `.field { display: flex }`).

## [0.x — pre-tag history]

The project's first ~50 commits don't have proper semver tags yet. Highlights
of major behaviour the dashboard ships today:

- **Live tab**: smooth-bezier 6h chart with gradient area fills + dual-axis
  battery %; clickable AC/DC/USB/Car cards (commands over Jackery cloud
  MQTT); mood-aware battery glow.
- **Real-time updates**: MQTT subscribe to `hb/app/{userId}/device` so the
  dashboard sees device-pushed deltas at ~500ms instead of HTTP polling rate.
- **Multi-device**: bridge polls every Jackery device on the account each
  cycle. Per-device telemetry exposed through `merged_poll().cloud
  .devices_telemetry`.
- **Energy tab**: per-device kWh totals (today / 7d / 30d / lifetime),
  time-bucketed history chart with range picker.
- **Device tab**: model / serial / cloud state; "Pause polling" button
  with duration picker so the phone app can hold the cloud session.
- **Automation tab**: SOC-driven Kasa smart-plug rules. Each rule targets
  a specific Jackery device, edge-triggered (fires once per crossing),
  retry-on-failure (transient errors don't burn the trigger). Saved-Kasa-
  device registry; encrypted Kasa cloud creds for KLAP/SMART devices.
- **Logs tab**: in-bridge ring buffer of notable events.
- **Settings tab**: runtime-tunable poll cadence + low-battery threshold
  + contested-cooldown.
- **Auto-deploy**: GHA → GHCR → Watchtower on the NAS.
- **PWA**: installable on iPhone home screen; service worker caches the
  shell.
