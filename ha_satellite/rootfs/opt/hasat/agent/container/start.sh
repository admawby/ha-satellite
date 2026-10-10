#!/bin/sh
# Entry point of the agent container (container mode, e.g. Synology DSM).
#
# Runs on the stock python image: the agent's Python dependencies are installed
# once into the agent's own data folder, so nothing is built on the host (no
# custom image, no build cache) and removing the folder removes everything.
set -eu

DATA=/opt/hasat
DEPS="${DATA}/pydeps"
REQS="aiohttp>=3.9,<4 psutil>=5.9"
STAMP="${DEPS}/.installed-$(printf '%s' "${REQS}" | md5sum | cut -c1-12)"

if [ ! -f "${STAMP}" ]; then
  echo "Installing agent dependencies into ${DEPS} ..."
  rm -rf "${DEPS}"
  # shellcheck disable=SC2086  # REQS is a list of requirement specifiers
  pip install --quiet --no-cache-dir --disable-pip-version-check --target "${DEPS}" ${REQS}
  touch "${STAMP}"
fi

export PYTHONPATH="${DEPS}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HASAT_MODE=container
cd "${DATA}/hasat-agent"
exec python3 -m hasat_agent
