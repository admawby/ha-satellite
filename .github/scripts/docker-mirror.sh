#!/usr/bin/env bash
# Route Docker Hub pulls on the CI runner through Google's public mirror, keeping any
# existing daemon settings. Shared runners often hit Docker Hub's anonymous rate limit.
set -euo pipefail
cfg=/etc/docker/daemon.json
current='{}'
if [[ -s "${cfg}" ]]; then
  current="$(sudo cat "${cfg}")"
fi
echo "${current}" | jq '. + {"registry-mirrors": ["https://mirror.gcr.io"]}' | sudo tee "${cfg}" >/dev/null
sudo systemctl restart docker
docker info --format '{{.RegistryConfig.Mirrors}}'
