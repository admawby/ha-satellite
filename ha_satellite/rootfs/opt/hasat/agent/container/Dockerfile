# HA Satellite agent, container mode (Synology DSM and other appliance systems).
# Built on the satellite by install.sh; the agent code itself is bind-mounted from
# the host so the add-on can update it without rebuilding this image.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

RUN pip install --no-cache-dir "aiohttp>=3.9,<4" "psutil>=5.9" \
    && command -v nsenter

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HASAT_MODE=container

WORKDIR /opt/hasat/hasat-agent
CMD ["python3", "-m", "hasat_agent"]
