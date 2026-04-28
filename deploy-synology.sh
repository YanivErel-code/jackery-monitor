#!/usr/bin/env bash
# deploy-synology.sh — first-time install AND update script for Synology / Linux.
#
# What it does:
#   1. git pull (if this is already a clone)
#   2. ensure .env exists  (copies from .env.example on first run, then aborts
#      so you can fill in your credentials)
#   3. docker compose --profile synology up -d --build
#
# Usage on the NAS (after SSH'ing in):
#   cd /volume1/docker/jackery-monitor   # or wherever you cloned it
#   ./deploy-synology.sh
#
# Re-run this same script any time you want to pull the latest code AND
# redeploy. It's idempotent.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# ----- 1. pull latest code -----
if [ -d .git ]; then
    echo "==> git pull"
    git pull --ff-only
else
    echo "==> not a git checkout; skipping pull"
fi

# ----- 2. ensure .env exists -----
if [ ! -f .env ]; then
    if [ ! -f .env.example ]; then
        echo "ERROR: neither .env nor .env.example exists. Re-clone the repo." >&2
        exit 1
    fi
    cp .env.example .env
    chmod 600 .env
    echo
    echo "================================================================"
    echo " First run detected: .env was just created from .env.example."
    echo " Edit it now and put your Jackery email/password in, then re-run"
    echo " this script:"
    echo
    echo "     nano .env"
    echo "     ./deploy-synology.sh"
    echo
    echo "================================================================"
    exit 0
fi

# Quick sanity check that the user actually filled .env in.
if grep -qE '^JACKERY_EMAIL=you@example\.com\s*$' .env \
   || grep -qE '^JACKERY_PASSWORD=your-password-here\s*$' .env; then
    echo "ERROR: .env still has placeholder values. Edit it first:" >&2
    echo "    nano .env" >&2
    exit 1
fi

# ----- 3. pick the right docker compose binary -----
# Synology DSM 7.2+ ships `docker compose` (v2 plugin). Older boxes have
# `docker-compose` as a separate binary.
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
    echo "On Synology: install Container Manager from Package Center." >&2
    exit 1
fi

# ----- 4. build + (re)start -----
echo "==> ${DC[*]} --profile synology up -d --build"
"${DC[@]}" --profile synology up -d --build

echo
echo "==> Containers:"
"${DC[@]}" ps

NAS_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<your-nas-ip>')"
PORT="$(grep -E '^JACKERY_HTTP_PORT=' .env | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8000}"

echo
echo "==> Done."
echo "   Dashboard:  http://${NAS_IP}:${PORT}"
echo "   Bridge:     internal only (jackery-bridge:8766 inside the docker network)"
echo
echo "Logs:"
echo "   ${DC[*]} logs -f jackery-monitor"
echo "   ${DC[*]} logs -f jackery-bridge"
