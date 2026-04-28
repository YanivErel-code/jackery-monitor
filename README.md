# Jackery 5000 Plus Monitor

A small local web app that shows live battery status for your Jackery Explorer 5000 Plus, charts the last few hours of activity, persists energy history per device, and pops a desktop notification when battery drops below 20 %.

It connects through the **Jackery cloud account** (the same one the official app uses). Credentials live only on your host in macOS Keychain.

---

## Pick a deployment mode

| Mode | When to use | Command |
|---|---|---|
| **Synology / Linux** | NAS or Linux box. Dashboard + bridge both run as containers. | `./deploy-synology.sh` |
| **Docker on macOS (bridge)** | Mac with the bridge as a host-native process (uses Keychain). | `./run-bridge.sh` + `docker compose -f docker-compose.dev.yml --profile mac up` |
| **Mock (no hardware)** | UI development / demo. | `docker compose -f docker-compose.dev.yml --profile mock up` |

> **Why a bridge?** Cloud credentials stay on the host (macOS Keychain or a `.env` file you control) instead of being baked into the container image. The bridge (`bridge.py`) owns the cloud session and exposes it over a local TCP socket; the dashboard container talks only to the bridge. On Synology both run as containers in the same docker network; on Mac the bridge runs as a launchd agent.

---

## Quick start — Docker on macOS

Terminal 1 — host cloud bridge (stays on the Mac):

```bash
./run-bridge.sh
# listens on 127.0.0.1:8766
```

Terminal 2 — containerized web app:

```bash
docker compose -f docker-compose.dev.yml --profile mac up
# open http://localhost:8000
```

Sign in once via the login modal in the web UI; credentials are stored in macOS Keychain (service `jackery-monitor`). To run the bridge automatically at login:

```bash
# Edit WorkingDirectory in the plist to your absolute jackery-monitor/ path first.
cp com.jackery.bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jackery.bridge.plist
launchctl start com.jackery.bridge

# logs
tail -f /tmp/jackery-bridge.out /tmp/jackery-bridge.err
```

---

## Quick start — Mock (no hardware)

```bash
docker compose -f docker-compose.dev.yml --profile mock up
```

Synthetic telemetry, the dashboard works end-to-end without a real device.

---

## What you'll see

- **Hero battery card**: percentage, time-remaining (or time-to-full when charging), battery temperature.
- **Live KPIs**: output watts, input watts, AC voltage and frequency.
- **Last 6 hours chart**: battery % line + input/output power areas, on a custom canvas (no chart library).
- **Output state**: AC / DC / USB / Car shown read-only (the Jackery cloud API does not expose port toggles).
- **Energy tab**: today / 7-day / 30-day / lifetime kWh totals, history chart with a 6h/24h/7d/30d range picker.
- **Device card**: model name, serial, cloud connection state, last update time, error code.
- **Device dropdown**: switch which Jackery on your account is being monitored.
- **Browser notification** when battery falls under 20 %.

## How it works

```
Browser ──WS──▶ FastAPI (container) ──TCP/JSON-RPC──▶ bridge.py (host) ──HTTPS──▶ Jackery cloud
```

- `server.py` — FastAPI app with `/api/status`, `/api/reconnect`, `/api/auth/*`, `/api/energy/*`, `/api/select_device`, `/ws`.
- `device_client.py` — pluggable backends: `mock`, `bridge` (TCP).
- `bridge.py` — host process polling the Jackery cloud; line-delimited JSON-RPC on TCP 8766.
- `cloud_client.py` — Jackery cloud API client (login, device list, properties).
- `energy_db.py` — SQLite Wh integrator (per-device samples + totals).
- `web/` — vanilla HTML/CSS/JS dashboard.

Polling: bridge logs in once → fetches device list → polls `device/property` for the selected device every 15 s. The container polls the bridge every 10 s and broadcasts over WebSocket.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `BACKEND` | `bridge` | `mock` / `bridge` |
| `BRIDGE_URL` | `host.docker.internal:8766` | Bridge endpoint when `BACKEND=bridge` |
| `JACKERY_MOCK` | unset | Shorthand for `BACKEND=mock` |
| `POLL_INTERVAL_S` | `10` | Server → bridge polling cadence |
| `CLOUD_POLL_INTERVAL_S` | `15` | Bridge → Jackery cloud polling cadence |
| `LOW_BATTERY_THRESHOLD` | `20` | Triggers low-battery alert |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | Server bind |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8766` | Bridge bind (used by `bridge.py`) |
| `JACKERY_DB` | `/data/energy.db` (container) | SQLite location |

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Cannot reach BLE bridge at host.docker.internal:8766" | Bridge isn't running. Start `./run-bridge.sh` on the host. |
| Login fails with "login failed: …" | Double-check the email/password in the Jackery app, and try region `EU` if you're outside the US. |
| Dashboard says *Cloud needs-credentials* | Sign in via the login modal, or run `./set-credentials.sh`. |
| Wrong device showing | Use the dropdown at the top of the Live tab to switch. |
| Port 8766 already in use | Set `BRIDGE_PORT=...` before `./run-bridge.sh` and `BRIDGE_URL=host.docker.internal:<port>` in compose. |

## File layout

```
jackery-monitor/
├── run-bridge.sh             host cloud bridge launcher
├── com.jackery.bridge.plist  launchd agent (auto-start the bridge)
├── server.py                 FastAPI backend (uses device_client)
├── device_client.py          mock | bridge backends
├── bridge.py                 host cloud process (JSON-RPC over TCP)
├── cloud_client.py           Jackery cloud API client
├── energy_db.py              SQLite energy integrator
├── Dockerfile
├── docker-compose.yml        Synology / Linux production (no profiles)
├── docker-compose.dev.yml    macOS dev + mock profiles
├── .dockerignore
├── requirements.txt
└── web/                      vanilla HTML/CSS/JS dashboard
```

## Credits

- Cloud-API teardown: [Hsky16's Qiita writeup](https://qiita.com/Hsky16/items/c163137265a87186ac39)

This app is unaffiliated with Jackery Inc.

---

## Quick start — Synology / Linux NAS

Tested on Synology DSM 7.2+ (Container Manager) on x86_64 hardware (RS822+, DS+ models). Should work on any Linux box with Docker + Compose v2.

### One-time setup

1. Install **Container Manager** on the Synology (Package Center → Container Manager). DSM 7.2+ ships with Compose v2.
2. Enable SSH (Control Panel → Terminal & SNMP → Enable SSH). SSH in as your admin user.
3. Pick a project folder on a real volume (not `/root`). The convention is `/volume1/docker/jackery-monitor`:

   ```bash
   sudo mkdir -p /volume1/docker
   cd /volume1/docker
   sudo git clone https://github.com/YanivErel-code/jackery-monitor.git
   sudo chown -R "$USER":users jackery-monitor
   cd jackery-monitor
   ```
4. Run the deploy script. The first run creates `.env` from the template and exits so you can fill it in:

   ```bash
   ./deploy-synology.sh
   nano .env          # set JACKERY_EMAIL, JACKERY_PASSWORD, optionally JACKERY_REGION
   ./deploy-synology.sh
   ```
5. Open the dashboard at **`http://<your-nas-ip>:8000`**. If port 8000 is taken, set `JACKERY_HTTP_PORT=8123` (or any free port) in `.env` and re-run.

### Synology Container Manager (no SSH)

If you don't want to use SSH, deploy via the DSM **Container Manager** UI:

1. Get the project files onto the NAS — File Station, upload+extract the GitHub zip into `/docker/jackery-monitor`.
2. In File Station, copy `.env.example` → `.env` and edit it (enable "Show hidden files" first; install the **Text Editor** package from Package Center if needed). Fill in `JACKERY_EMAIL` and `JACKERY_PASSWORD`.
3. Open **Container Manager** → **Project** → **Create**.
4. Project name: `jackery-monitor`. Path: `/docker/jackery-monitor`.
5. **Source**: "Use existing docker-compose.yml". Container Manager will pick up the project's `docker-compose.yml` automatically — it's profile-free and runs both services. (The Mac/mock variants live in `docker-compose.dev.yml` and are ignored here.)
6. Click **Next** → **Done**. The build takes 2–3 minutes. Two containers (`jackery-bridge` + `jackery-monitor`) will start.

> If an earlier project run failed with `no service selected`, that was caused by the previous compose file using profiles. Delete the old project (Container Manager → Project → the project → **Action → Delete**, leave **"Also remove volumes" UNCHECKED** to preserve your DB), pull the latest code, and recreate the project.
7. Dashboard at `http://<your-nas-ip>:8000`.

### Updating to the latest code

```bash
cd /volume1/docker/jackery-monitor
./deploy-synology.sh
```

Same script — it does `git pull` then rebuilds and restarts. The energy database in the `jackery-data` volume is preserved across rebuilds.

### How credentials are stored on the NAS

Two options, in priority order:

1. **`.env` file** (recommended). Lives only on the NAS, mode `0600`, never committed to git. The bridge reads `JACKERY_EMAIL` / `JACKERY_PASSWORD` / `JACKERY_REGION` from environment at startup.
2. **Sign in via the dashboard**. If `.env` is empty, the bridge starts idle and the dashboard shows a sign-in form. Submitting it persists the credentials to `/data/jackery-creds.json` inside the `jackery-data` volume (mode `0600`). Same place the energy database lives. Survives container restarts.

The `set_credentials` web endpoint refuses to write when `.env` has pinned credentials — that prevents the dashboard from overriding what the operator configured.

### Networking notes

- The bridge container **does not publish** port 8766 to the LAN. Only the dashboard (`jackery-monitor`) on port 8000 is reachable from your network. The two containers talk over the internal docker bridge network using DNS name `jackery-bridge:8766`.
- If you want the dashboard reachable over HTTPS / from outside your LAN, use Synology's reverse-proxy (Control Panel → Login Portal → Advanced → Reverse Proxy) to point a `https://jackery.your-domain` host to `http://localhost:8000`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `./deploy-synology.sh: Permission denied` | `chmod +x deploy-synology.sh` |
| `docker compose` not found | Install Container Manager from Package Center; on older DSM use `docker-compose` (script handles both). |
| Dashboard says "bridge unreachable" | `docker compose logs jackery-bridge` — usually a credentials issue, check `.env`. |
| Login fails with 401 | Confirm the email/password work in the actual Jackery mobile app first. |
| Port 8000 already in use | Set `JACKERY_HTTP_PORT=8123` in `.env`, re-run `./deploy-synology.sh`. |
