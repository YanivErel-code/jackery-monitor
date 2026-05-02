# Jackery Monitor — container image (cloud-only build).
#
# Same image runs either the web server or the cloud bridge — the compose
# file picks which entrypoint to use per service.
#
#   - server.py  : FastAPI dashboard + WebSocket fan-out (port 8000)
#   - bridge.py  : Cloud poller, JSON-RPC over TCP (port 8766)
#
# Compose profiles:
#   mac        - dashboard only; bridge runs on the macOS host
#   mock       - no hardware/cloud, synthetic telemetry (UI dev)
#   synology   - dashboard + bridge, both in containers (Linux/NAS)

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# smbclient is the only Samba binary we need now: backup.py uses it to
# upload snapshots over SMB (no kernel mount → no CAP_SYS_ADMIN needed),
# and backup_discover.py uses it to enumerate share names for the
# Settings UI's share-name dropdown.
#
# We deliberately do NOT install cifs-utils / mount.cifs anymore. That
# path required CAP_SYS_ADMIN + capset() seccomp permission inside the
# container and tended to fail on Synology with "Unable to apply new
# capability set." smbclient avoids the kernel mount entirely.
RUN apt-get update \
    && apt-get install -y --no-install-recommends smbclient \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy every Python module + the web assets. We use a glob (*.py) instead of
# enumerating files so adding a new module doesn't silently fail to land in
# the image (we got bitten once by forgetting to add settings.py here).
COPY *.py ./
COPY web ./web
# Static reference data shipped with the image — model_code → capacity
# catalog (forecaster.py) and the AI advisor's allowed-tunables list
# (claude_advisor.py). Both loaded at import time. Keep separate from
# /data (which is a runtime volume).
COPY models.json tunables.json ./

# Persistent data lives here (energy.db, jackery-creds.json on Linux hosts).
RUN mkdir -p /data
VOLUME ["/data"]

# Note on non-root: deliberately running as root inside the container.
# Switching to a non-root UID would break existing deployments where the
# /data volume already has files owned as root from prior versions, since
# the new UID couldn't read its own credentials. The container itself
# is isolated, so root-in-container is acceptable for this app's threat
# model. Future migration: ship an entrypoint that chowns /data first
# and then drops via gosu, with a one-time backfill for existing volumes.

EXPOSE 8000 8766

# Default command runs the dashboard. The bridge service in compose
# overrides this with `command: ["python", "bridge.py"]`.
CMD ["python", "server.py"]
