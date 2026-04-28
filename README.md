# Jackery Monitor

Self-hosted dashboard for Jackery power stations. Live battery + power, 6-hour
history chart, output control over MQTT, energy aggregation in SQLite, and
SOC-driven automations that flip Kasa smart plugs.

Designed to run on a Synology NAS in Docker. Works anywhere with Docker
Compose. Multi-device — handles multiple Jackery devices on the same account
(e.g. Explorer 5000 Plus + HomePower 3000) and per-device automation rules.

![Dashboard preview — Live tab with smooth gradient charts and accent-colored KPI cards.](web/icon.svg)

---

## What's in it

- **Live tab** — hero battery card with mood-aware glow (green when charging,
  blue when discharging, red when low), KPI cards for output / input / today's
  energy, smooth-bezier 6-hour chart with gradient area fills, dual-axis
  battery %, hover tooltip.
- **Output control** — click an AC/DC/USB/Car card to toggle. Goes over
  Jackery cloud MQTT (`emqx.jackeryapp.com`), confirms before turning AC off.
  Optimistic UI holds during the device's apply window so you don't see
  flicker.
- **Energy tab** — today / 7d / 30d / lifetime kWh totals, time-bucketed
  history chart with 6h / 24h / 7d / 30d ranges, per-device totals.
- **Device tab** — model, serial, cloud connection state, last update time.
  "Pause polling" with duration picker so you can hand the cloud session to
  the phone app without the bridge stealing it back.
- **Automation tab** — Kasa smart-plug rules driven by battery SOC. Each
  rule targets a specific Jackery device, fires once per threshold crossing,
  retries automatically on transient failures.
- **Logs tab** — ring buffer of bridge events (login, MQTT pushes, contested
  sessions, automation fires, errors). Filter by level or category.
- **Settings tab** — runtime-tunable poll cadences, low-battery threshold,
  session-contested cooldown.
- **PWA** — install on your phone home screen via Safari → Share → Add to
  Home Screen. Service worker caches the UI shell.
- **Auth** — single-user username/password login on first boot. Optional;
  pair with Cloudflare Access for internet-exposed deployments.
- **Real-time updates** — MQTT subscribe to the device push topic gives you
  ~500ms-fresh telemetry instead of HTTP polling rate.
- **Auto-deploy** — `git push` triggers a Watchtower-driven container update
  on the NAS within a minute or two. No manual restarts.

---

## Architecture

```
   Browser
     │
     ├──HTTPS── /, /api/*, /static, /login    (FastAPI server)
     └──WS──── /ws  (telemetry broadcast)
                                              ▲
                                              │ JSON over TCP (localhost:8766)
                                              │
   FastAPI server (server.py) ◀──────────────▶ Bridge (bridge.py)
        │                                              │
        ├─── SQLite (/data/energy.db)                  ├──HTTPS── iot.jackeryapp.com   (HTTP API: login, properties)
        ├─── /data/settings.json                       │
        ├─── /data/automation.json                     └──MQTT/TLS── emqx.jackeryapp.com  (push + output commands)
        ├─── /data/kasa_devices.json
        ├─── /data/kasa-creds.json (encrypted)
        ├─── /data/auth.json (encrypted)
        │
        └──HTTP── Kasa smart plugs on the LAN  (python-kasa, KLAP/SMART/IOT)
```

Both server and bridge run as the same image (`ghcr.io/yaniverel-code/jackery-monitor`),
just different `command:`s in compose. The shared `/data` volume holds energy
history, credentials (encrypted), automation rules, and settings.

---

## Quick start — Synology / Linux

The default `docker-compose.yml` pulls a pre-built image from GitHub
Container Registry — no local build step on the NAS.

1. Install **Container Manager** (Synology Package Center) or Docker Compose v2.
2. Get the project files onto the NAS via File Station: download the
   [latest zip](https://github.com/YanivErel-code/jackery-monitor/archive/refs/heads/main.zip)
   and extract into `/docker/jackery-monitor/`.
3. **Container Manager → Project → Create**:
   - Project name: `jackery-monitor`
   - Path: `/docker/jackery-monitor`
   - Source: "Use existing docker-compose.yml"
4. Click **Next → Done**. Container Manager pulls the image (~30s) and
   starts the services.
5. Open `http://<nas-ip>:8123`.
6. **First visit:** pick a username + password to lock down the dashboard.
7. **Second:** sign into your Jackery cloud account through the dashboard
   modal. Credentials are encrypted at rest.

The default port is `8123`. Set `JACKERY_HTTP_PORT=8000` in `.env` if you
want it elsewhere.

### Updating

The compose file ships with a Watchtower service that polls GHCR every
60s and recreates the labelled containers when a new `:latest` is
published. So `git push` → image rebuild → ~60-90s → NAS is up to date.

To update the **compose file itself** (env, services, ports), copy the
new `docker-compose.yml` to your NAS via File Station and recreate the
project.

## Quick start — macOS dev

Bridge runs on the Mac (uses macOS Keychain for credentials), dashboard
in Docker:

```bash
./run-bridge.sh      # listens on 127.0.0.1:8766
docker compose -f docker-compose.dev.yml --profile mac up
open http://localhost:8000
```

Or in fully-mock mode (no Jackery cloud, synthetic telemetry):

```bash
docker compose -f docker-compose.dev.yml --profile mock up
```

---

## PWA install on phone

Open the dashboard URL in **Safari** (iOS) or **Chrome** (Android) →
**Share / menu → Add to Home Screen**. The app opens full-screen, no
browser chrome, looks like a native app. Service worker caches the UI
shell so it loads instantly even on slow networks.

For internet-accessible install, point Safari at your Cloudflare Tunnel
URL (see Authentication below) and Add to Home Screen there.

---

## Authentication

Two layers, use either or both:

### Cloudflare Access (recommended for public exposure)

Free for up to 50 users, edge-level, battle-tested OIDC stack.
1. Run a Cloudflare Tunnel on your NAS (synology has a one-click app).
2. Add a public hostname pointing at `localhost:8123`.
3. Cloudflare Zero Trust → Access → Applications → Add a Self-hosted app
   pointing at the same hostname.
4. Add an Allow policy listing your authorized email(s).

Visit the public URL → email-OTP login → you're in.

### In-app username/password (built-in)

First visit auto-redirects to `/setup` where you pick a username and
password. From then on, `/login` is required. Sign-out button in the
top bar. Password is hashed with PBKDF2-SHA256, session is an
HMAC-signed cookie (HttpOnly, SameSite=Lax, 30-day TTL).

The two layers compose: Cloudflare Access at the edge plus app login
gives you defense in depth.

---

## Automation

Drive Kasa smart plugs from your battery state of charge.

1. **Automation tab → Kasa account.** Enter your Kasa cloud email +
   password. Required for newer Kasa SMART devices (KP125M, EP25, KP405)
   that use KLAP authentication. Older devices (KP115, HS103) ignore
   credentials gracefully — saving them is safe.
2. **Devices section → + Add device.** Enter the Kasa plug's IP (find
   it in your router's DHCP table or the official Kasa app). Click
   **Test** to verify reachability and auto-fill the alias. Save.
   Repeat for every plug you want to control.
3. **Battery-driven rules → + New rule.** Pick a Jackery device,
   threshold (`<`, `<=`, `=`, `>=`, `>` and a percentage), action
   (`on` or `off`), and one of your saved Kasa devices. Save.

Rules are **edge-triggered**: a `<20%` rule fires once when SOC drops
through 20%, not every poll while it's below 20%. It won't fire again
until SOC goes back above 20% and drops below it again. Failed actions
retry on the next poll cycle (transient errors don't burn the trigger).

The bridge polls **all** Jackery devices on the account every cycle, so
a rule targeting your HomePower 3000 fires even while the dashboard is
viewing the 5000 Plus.

---

## Configuration

Most knobs live in the **Settings tab**, persisted to
`/data/settings.json`, applied on the next poll cycle (no restart):

| Setting | Default | Range | What it does |
|---|---|---|---|
| Server poll interval | 2 s | 1-300 | Server → bridge → browser cadence. With MQTT push the bridge has ~500ms-fresh data; this is just the WS broadcast rate. |
| Cloud poll interval | 15 s | 5-600 | HTTP poll to the Jackery cloud. Now a backstop since MQTT push handles real-time. |
| Session-contested cooldown | 60 s | 10-600 | After the phone app bumps the bridge off, how long before the bridge tries to reclaim. |
| Low-battery alert threshold | 20 % | 1-99 | Below this, the dashboard shows a low-battery alert banner. |

Env vars (`POLL_INTERVAL_S`, etc.) act as defaults until you save a
value through the UI.

---

## File layout

```
jackery-monitor/
├── server.py                 FastAPI dashboard, WS broadcast, REST API
├── bridge.py                 Cloud + MQTT bridge (separate container)
├── cloud_client.py           Jackery HTTP + MQTT client
├── device_client.py          mock | bridge | (legacy native) backends
├── energy_db.py              SQLite Wh integrator with per-device totals
├── automation.py             Edge-triggered SOC rule engine
├── kasa_client.py            python-kasa wrapper (status / set_state / discover)
├── kasa_devices.py           Saved Kasa-device registry (/data/kasa_devices.json)
├── kasa_creds.py             Encrypted Kasa cloud creds (/data/kasa-creds.json)
├── auth.py                   App-level user auth (PBKDF2 + HMAC sessions)
├── settings.py               Runtime-tunable settings module
├── crypto_util.py            Shared AES-256-GCM helper
│
├── web/
│   ├── index.html            Main dashboard
│   ├── login.html            /login + /setup pages
│   ├── style.css             Single CSS file (no build step)
│   ├── app.js                Single JS file (no build step)
│   ├── manifest.webmanifest  PWA manifest
│   ├── sw.js                 Service worker
│   └── icon.svg              PWA / favicon icon
│
├── docker-compose.yml        Synology / Linux prod (GHCR pull + Watchtower)
├── docker-compose.build.yml  Build locally on the NAS instead of pulling
├── docker-compose.dev.yml    macOS host-bridge + mock profiles
├── Dockerfile
├── requirements.txt
├── .github/workflows/        Builds + publishes ghcr.io image on push
└── deploy-synology.sh        SSH-based one-command updater (optional)
```

Persistent state on the NAS (Docker volume `jackery-data`, mounted at
`/data` in both containers):

```
/data/
├── energy.db                 SQLite — energy aggregation per device
├── settings.json             Runtime settings overrides
├── automation.json           Saved automation rules
├── kasa_devices.json         Saved Kasa-device registry
├── kasa-creds.json           Encrypted Kasa cloud account credentials
├── jackery-creds.json        Encrypted Jackery cloud account credentials
├── auth.json                 Encrypted dashboard-login user
└── .jackery-creds.key        AES-256 at-rest encryption key (mode 0600)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says *bridge unreachable* | `docker compose logs jackery-bridge` — usually a creds issue. Sign in via the dashboard. |
| Phone app keeps signing out when bridge is running | Expected — Jackery allows one session per account. Use the "Pause polling" button on the Device tab to hand the session over for a configurable duration, or wait the 60s contested-cooldown. |
| Output toggle button reverts after click | Normal — the device takes 5-30s to apply. The UI holds the optimistic state during a 30s "pending" window. |
| Kasa device test fails with `Device response did not match our challenge` | Newer Kasa firmware uses KLAP auth. Enter your Kasa cloud email + password in **Automation tab → Kasa account**. Email is case-sensitive. |
| Kasa test fails with `ZoneInfoNotFoundError` | The image needs the `tzdata` Python package. Should be in latest builds — make sure Watchtower has pulled. |
| Live chart shows only 6 minutes after a deploy | Watchtower restarted the container, in-memory chart history was wiped. The chart hydrates from the energy DB on the next poll — give it a minute. |
| Energy DB lost data after a project recreate | Container Manager's "Delete project" can wipe the volume if you don't uncheck "Delete volumes". Always uncheck. |

---

## Limitations

- **Per-input solar (HPV vs LPV) isn't in the Jackery cloud API.** Only the
  total solar input (`ip - acip - cip`) is exposed. Confirmed empirically
  on a real device and across multiple independent reverse-engineering
  projects. The Jackery iOS app itself shows just one solar number.
- **One Jackery cloud session per account** — bridge + phone fight over
  it. We auto-cooldown on contests; the manual pause button gives you a
  longer window.
- **Kasa SMART devices need cloud credentials** for local control;
  older Kasa devices don't.
- **The cloud API is reverse-engineered** and could change at any time.
  We've added tolerance for known field-name variations, but a major API
  rev would need code changes.

---

## Credits

Reverse-engineered protocol references:
- [jlopez/socketry](https://github.com/jlopez/socketry) — most thorough APK
  decompilation, includes
  [docs/protocol.md](https://github.com/jlopez/socketry/blob/main/docs/protocol.md)
  with the MQTT topic structure and action IDs.
- [theak/jackery-homeassistant](https://github.com/theak/jackery-homeassistant) —
  HTTP-only HA integration; original reference for the property keys.
- [turmacar/jackery-homeassistant](https://github.com/turmacar/jackery-homeassistant) —
  fork that adds writable switches over MQTT.
- [Hsky16's Qiita writeup](https://qiita.com/Hsky16/items/c163137265a87186ac39) —
  original auth-flow analysis.

This app is unaffiliated with Jackery Inc. or TP-Link / Kasa.
