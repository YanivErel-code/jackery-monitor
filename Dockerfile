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

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy every Python module + the web assets. We use a glob (*.py) instead of
# enumerating files so adding a new module doesn't silently fail to land in
# the image (we got bitten once by forgetting to add settings.py here).
COPY *.py ./
COPY web ./web

# Persistent data lives here (energy.db, jackery-creds.json on Linux hosts).
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000 8766

# Default command runs the dashboard. The bridge service in compose
# overrides this with `command: ["python", "bridge.py"]`.
CMD ["python", "server.py"]
