#!/bin/sh
# Start (or restart) the HA Satellite agent container.
#
#   run.sh <base-dir> <data-volume> [image]
#
# <base-dir>     holds hasat-agent/ (code), etc/ (certificates, config) and state/
# <data-volume>  volume whose usage is reported as "disk" (e.g. /volume1), mounted read-only
#
# Host networking, PID and UTS namespaces plus --privileged are required so the
# agent reports the host's metrics/hostname and the terminal can enter the host
# with nsenter. The Docker socket lets it manage the host's containers.
set -eu

BASE="$1"
VOL="$2"
IMAGE="${3:-hasat-agent:local}"
NAME="${HASAT_CONTAINER_NAME:-hasat-agent}"

set --
if [ -f /etc.defaults/VERSION ]; then
  set -- -v /etc.defaults/VERSION:/host/VERSION:ro
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --network host --pid host --uts host --privileged \
  --label hasat.update=false \
  --label hasat.role=agent \
  -e HASAT_MODE=container \
  -e HASAT_CONTAINER_NAME="$NAME" \
  -e HASAT_INSTALL_DIR=/opt/hasat/hasat-agent \
  -e HASAT_ETC=/opt/hasat/etc \
  -e HASAT_STATE=/opt/hasat/state \
  -e HASAT_DISK_PATH=/host/volume \
  -e HASAT_HOST_VERSION=/host/VERSION \
  -v "$BASE:/opt/hasat" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$VOL:/host/volume:ro" \
  "$@" \
  "$IMAGE"
