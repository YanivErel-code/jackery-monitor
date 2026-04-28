#!/usr/bin/env bash
# apply-cloud-patch.sh
#
# Applies the WiFi/Cloud backend update to an existing jackery-monitor install.
# Run this on your Mac after extracting the new zip into ~/jackery-monitor.
#
# What it does:
#   1. Verifies the install layout (preserves your existing .venv)
#   2. Installs new Python deps:  httpx, pycryptodomex
#   3. Prompts for Jackery cloud credentials and stores them in macOS Keychain
#   4. Restarts the bridge (launchd agent)
#   5. Rebuilds + restarts the Docker container
#
# Safe to re-run.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/jackery-monitor}"
VENV_DIR="$INSTALL_DIR/.venv"
PLIST_LABEL="com.jackery.bridge"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }

bold "Jackery Monitor — Cloud Backend Patch"
echo "Install dir: $INSTALL_DIR"
echo

if [[ ! -f "$INSTALL_DIR/bridge.py" ]]; then
  red "ERROR: $INSTALL_DIR/bridge.py not found. Did you extract the new zip there?"
  echo "Tip: cd ~ && unzip -o ~/Downloads/jackery-monitor.zip"
  exit 1
fi

# Detect existing venv
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  red "ERROR: $VENV_DIR/bin/python missing. Re-run install.sh to recreate the venv."
  exit 1
fi
green "✓ found venv: $VENV_DIR"

# 1. Install new deps
bold "[1/4] Installing new Python deps (httpx, pycryptodomex)..."
"$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
green "✓ deps installed"

# 2. Prompt for cloud creds via the helper, unless already set
bold "[2/4] Checking macOS Keychain for cloud credentials..."
if security find-generic-password -s jackery-monitor -a cloud-email -w >/dev/null 2>&1 \
&& security find-generic-password -s jackery-monitor -a cloud-password -w >/dev/null 2>&1; then
  green "✓ cloud credentials already in keychain"
  read -r -p "Update them anyway? [y/N] " yn
  if [[ "${yn:-N}" =~ ^[Yy]$ ]]; then
    bash "$INSTALL_DIR/set-credentials.sh"
  fi
else
  yellow "No cloud credentials found — running set-credentials.sh now."
  bash "$INSTALL_DIR/set-credentials.sh"
fi

# 3. Restart the launchd bridge agent
bold "[3/4] Restarting bridge launchd agent..."
UID_NUM=$(id -u)
if launchctl print "gui/$UID_NUM/$PLIST_LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$UID_NUM/$PLIST_LABEL"
  green "✓ bridge restarted"
else
  yellow "launchd agent $PLIST_LABEL not loaded; loading from plist..."
  launchctl load -w "$HOME/Library/LaunchAgents/$PLIST_LABEL.plist" 2>/dev/null || true
  launchctl kickstart -k "gui/$UID_NUM/$PLIST_LABEL" 2>/dev/null || true
fi

# Quick health check
sleep 2
if (echo '{"method":"ping"}' | nc -G 2 127.0.0.1 8765 2>/dev/null) | grep -q '"ok": *true'; then
  green "✓ bridge ping OK on 127.0.0.1:8765"
else
  yellow "Bridge ping did not respond yet — check ~/jackery-monitor/bridge.log in a few seconds."
fi

# 4. Rebuild + restart the container
bold "[4/4] Rebuilding Docker container..."
cd "$INSTALL_DIR"
if docker compose --profile mac up -d --build; then
  green "✓ container rebuilt and started"
else
  red "Docker build failed — check the output above."
  exit 1
fi

echo
bold "DONE"
echo "Web UI: http://localhost:8000"
echo
echo "Tail logs:"
echo "  Bridge: tail -f $INSTALL_DIR/bridge.log"
echo "  App:    docker logs -f jackery-monitor"
echo
echo "Notes:"
echo "  • The first time the bridge runs BLE, macOS may prompt for"
echo "    Bluetooth permission for python3.12 — click Allow."
echo "  • Cloud poller refreshes every 60s; BLE every 30s."
echo "  • If BLE is unavailable, you'll see 'via Cloud' on the dashboard."
echo "  • To toggle outputs (AC/DC/USB/Car) you must be in BLE range."
