#!/usr/bin/env bash
# deploy-synology.sh — first-time install AND update script for Synology / Linux.
#
# What it does:
#   1. git pull (if this is already a clone)
#   2. ensure .env exists  (copies from .env.example on first run, then aborts
#      so you can fill in your credentials)
#   3. docker compose pull && up -d   (default: pull pre-built image from ghcr.io)
#                              -- or --
#      docker compose -f docker-compose.build.yml up -d --build
#      if BUILD_LOCAL=1 is set or ghcr.io is unreachable.
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
    echo " First run: .env was created from .env.example."
    echo " Defaults are fine -- you'll sign in with your Jackery account"
    echo " through the dashboard UI on first load (credentials are encrypted"
    echo " and saved to the jackery-data Docker volume)."
    echo "================================================================"
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

# ----- 4. (re)start -----
if [ "${BUILD_LOCAL:-0}" = "1" ]; then
    COMPOSE_FILE=docker-compose.build.yml
    echo "==> BUILD_LOCAL=1: building image from local Dockerfile"
    echo "==> ${DC[*]} -f ${COMPOSE_FILE} up -d --build"
    "${DC[@]}" -f "${COMPOSE_FILE}" up -d --build
else
    echo "==> docker compose pull (latest image from ghcr.io)"
    "${DC[@]}" pull
    echo "==> ${DC[*]} up -d"
    "${DC[@]}" up -d
fi

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
