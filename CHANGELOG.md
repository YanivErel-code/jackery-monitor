# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project tries to follow [Semantic Versioning](https://semver.org/).
Pre-1.0 means breaking changes can land in any minor.

## [Unreleased]

### Added
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
