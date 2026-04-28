# Jackery 5000 Plus Monitor

A small local web app that shows live battery status for your Jackery Explorer 5000 Plus, charts the last few hours of activity, persists energy history per device, and pops a desktop notification when battery drops below 20 %.

It connects through the **Jackery cloud account** (the same one the official app uses). Credentials live only on your host in macOS Keychain.

---

## Pick a deployment mode

| Mode | When to use | Command |
|---|---|---|
| **Docker on macOS (bridge)** | Recommended. Container runs the web app, host bridge handles cloud auth. | `./run-bridge.sh` + `docker compose --profile mac up` |
| **Mock (no hardware)** | UI development / demo. | `docker compose --profile mock up` |

> **Why a bridge?** Cloud credentials stay on the host (in macOS Keychain) instead of being baked into the container. The bridge is a tiny host-native process (`bridge.py`) that owns the cloud session and exposes it to the container over a local TCP socket.

---

## Quick start — Docker on macOS

Terminal 1 — host cloud bridge (stays on the Mac):

```bash
./run-bridge.sh
# listens on 127.0.0.1:8766
```

Terminal 2 — containerized web app:

```bash
docker compose --profile mac up
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
docker compose --profile mock up
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
├── docker-compose.yml        profiles: mac / mock
├── .dockerignore
├── requirements.txt
└── web/                      vanilla HTML/CSS/JS dashboard
```

## Credits

- Cloud-API teardown: [Hsky16's Qiita writeup](https://qiita.com/Hsky16/items/c163137265a87186ac39)

This app is unaffiliated with Jackery Inc.
