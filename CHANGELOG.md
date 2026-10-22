# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project tries to follow [Semantic Versioning](https://semver.org/).
Pre-1.0 means breaking changes can land in any minor.

## [Unreleased]

### Added
- Add encrypted, UI-configurable, read-only LG ThinQ connections for generic LG
  appliances. Map selected appliances to each Jackery's 220/240 V charging
  interlock; active, unknown, offline or stale states hold grid charging OFF.
  Recheck all Kasa ON paths, enforce an independent OFF watchdog and preserve
  a manual 240 V reservation. Protection is opt-in; unsupported states fail closed.
  Use the latest selected appliance's estimated finish plus a five-minute margin
  to constrain charging windows, estimate SOC at finish and audit the timing
  inputs; a timer never overrides the requirement for confirmed OFF status.

### Fixed
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
