#!/usr/bin/env bash
# Run the host-side cloud bridge on macOS/Linux.
# This is what the container talks to when BACKEND=bridge.
#
# Usage:   ./run-bridge.sh
# Env:     BRIDGE_HOST (default 127.0.0.1), BRIDGE_PORT (default 8766)

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install -q --upgrade pip
pip install -q -r requirements.txt

export BRIDGE_HOST="${BRIDGE_HOST:-127.0.0.1}"
export BRIDGE_PORT="${BRIDGE_PORT:-8766}"

echo "Starting Jackery cloud bridge on ${BRIDGE_HOST}:${BRIDGE_PORT}"
echo "(Container should set BRIDGE_URL=host.docker.internal:${BRIDGE_PORT})"
exec python bridge.py
