# Jackery Monitor — container image (cloud-only build).
#
# Two backends:
#   - bridge mode  (BACKEND=bridge, talks to host bridge.py over TCP)
#   - mock mode    (BACKEND=mock, synthetic telemetry)
#
# Run via docker-compose for the recommended setups.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py device_client.py energy_db.py cloud_client.py ./
COPY web ./web

EXPOSE 8000

# Default: bridge mode. docker-compose can override.
ENV BACKEND=bridge

CMD ["python", "server.py"]
