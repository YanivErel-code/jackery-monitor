#!/usr/bin/env bash
# Stores Jackery cloud credentials in macOS Keychain.
# The bridge reads them at startup; nothing is written to disk.
#
# Usage:
#   ./set-credentials.sh
#
# Stored items:
#   service: jackery-monitor
#   account: cloud-email   (value: your Jackery account email)
#   account: cloud-password (value: your Jackery account password)
#
# To remove later:
#   security delete-generic-password -s jackery-monitor -a cloud-email
#   security delete-generic-password -s jackery-monitor -a cloud-password

set -euo pipefail

SERVICE="jackery-monitor"

if ! command -v security >/dev/null; then
    echo "Error: 'security' command not found. This script only runs on macOS."
    exit 1
fi

read -rp "Jackery account email: " EMAIL
if [ -z "$EMAIL" ]; then
    echo "Email is required."
    exit 1
fi

# -s flag suppresses input echo so the password isn't visible
read -rsp "Jackery account password: " PASSWORD
echo
if [ -z "$PASSWORD" ]; then
    echo "Password is required."
    exit 1
fi

# Replace existing entries (-U)
security add-generic-password -U -s "$SERVICE" -a "cloud-email" -w "$EMAIL"
security add-generic-password -U -s "$SERVICE" -a "cloud-password" -w "$PASSWORD"

# Region (default US)
read -rp "Region [US]: " REGION
REGION="${REGION:-US}"
security add-generic-password -U -s "$SERVICE" -a "cloud-region" -w "$REGION"

echo
echo "Stored in macOS Keychain (service=$SERVICE):"
echo "  cloud-email    = $EMAIL"
echo "  cloud-password = (hidden)"
echo "  cloud-region   = $REGION"
echo
echo "Restart the bridge to pick them up:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.jackery.bridge"
