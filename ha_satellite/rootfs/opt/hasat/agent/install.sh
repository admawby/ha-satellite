#!/usr/bin/env bash
# HA Satellite agent installer.
#
# This script is generated per enrollment by the HA Satellite add-on. It contains a
# one-time token (valid 30 minutes) and the SHA-256 pin of the add-on's enrollment
# certificate, so every download below is authenticated against your HA instance.
#
# Raspberry Pi OS / Debian / Ubuntu ("host mode"):
#   * installs dependencies (python3-aiohttp, python3-psutil, ser2net, nftables)
#   * installs the agent to /opt/hasat-agent as a systemd service
# Synology DSM and other systems without apt but with Docker ("container mode"):
#   * runs the agent in the container "hasat-agent" on the stock python:3.12-slim image
#   * only health stats, the terminal and Docker management are enabled
# Both:
#   * generate the satellite's private key locally (it never leaves this machine)
#   * obtain a certificate from the add-on's private CA
set -euo pipefail

CONTROLLER_HOST="@@CONTROLLER_HOST@@"
CONTROLLER_PORT="@@CONTROLLER_PORT@@"
TOKEN="@@TOKEN@@"
PIN="@@PIN@@"
AGENT_PORT="@@AGENT_PORT@@"

BASE_URL="https://${CONTROLLER_HOST}:${CONTROLLER_PORT}/enroll/${TOKEN}"
# DSM keeps docker and its admin tools outside sudo's default PATH.
export PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/syno/sbin:/usr/syno/bin"

info() { printf '\033[1;34m[hasat]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[hasat] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

fetch() {
  curl -fsS --insecure --pinnedpubkey "sha256//${PIN}" --retry 3 --connect-timeout 10 "$@"
}

is_synology() {
  [[ -f /etc.defaults/VERSION ]] && grep -q '^productversion=' /etc.defaults/VERSION
}

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# download_agent <install-dir>: fetch and unpack the agent bundle atomically.
download_agent() {
  local dir="$1"
  info "Downloading agent from ${CONTROLLER_HOST} ..."
  fetch -o "${TMP}/agent.tar.gz" "${BASE_URL}/agent.tar.gz"
  rm -rf "${dir}.new"
  mkdir -p "${dir}.new"
  tar -xzf "${TMP}/agent.tar.gz" -C "${dir}.new"
  [[ -f "${dir}.new/hasat_agent/__init__.py" ]] || fail "downloaded agent is incomplete"
  rm -rf "${dir}"
  mv "${dir}.new" "${dir}"
}

# request_certificate <etc-dir>: create the key + CSR and submit it with the token.
request_certificate() {
  local etc="$1"
  info "Generating satellite key and requesting certificate ..."
  umask 077
  mkdir -p "${etc}"
  chmod 700 "${etc}"
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "${etc}/agent.key" -subj "/CN=hasat-pending" -out "${TMP}/agent.csr" 2>/dev/null
  # Report the address this machine uses towards HA (HA may only see a NAT address).
  local ctrl_ip src_ip
  ctrl_ip="$(getent ahostsv4 "${CONTROLLER_HOST}" 2>/dev/null | awk 'NR==1{print $1}' || true)"
  src_ip="$(ip -4 route get "${ctrl_ip:-${CONTROLLER_HOST}}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)"
  fetch -X POST -H "Content-Type: application/pkcs10" --data-binary "@${TMP}/agent.csr" \
    -o "${TMP}/enroll.json" "${BASE_URL}/csr?hostname=$(hostname)&ip=${src_ip}"
}

# The enrollment response is JSON; this small script writes the certificate files.
# It runs with the host's python3 (host mode) or inside the agent image (container mode).
WRITE_FILES_PY='
import json, os, sys
resp = json.load(open(sys.argv[1]))
etc = sys.argv[2]
def write(name, data):
    path = os.path.join(etc, name)
    with open(path, "w") as fh:
        fh.write(data)
    os.chmod(path, 0o600)
write("agent.crt", resp["cert"])
write("ca.crt", resp["ca"])
write("config.json", json.dumps({
    "id": resp["id"],
    "name": resp["name"],
    "controller_cn": resp["controller_cn"],
    "trusted_ips": resp["trusted_ips"],
    "agent_port": resp["agent_port"],
}, indent=2))
print("[hasat] Enrolled as %s (%s)" % (resp["name"], resp["id"]))
'

# Everything this installer adds is written to install-record.json so that removing
# the satellite can undo exactly that, and nothing that was on the machine before.
RECORD_PY='
import json, os, sys
path = sys.argv[1]
rec = {}
if os.path.exists(path):
    try:
        rec = json.load(open(path))
    except ValueError:
        rec = {}
for key, value in json.loads(sys.argv[2]).items():
    if isinstance(value, list):
        rec[key] = sorted(set(rec.get(key, [])) | set(value))
    elif key not in rec:  # keep facts from the first install
        rec[key] = value
if len(sys.argv) > 3:
    added = [p for p in sys.argv[3].split() if p]
    rec["packages_added"] = sorted(set(rec.get("packages_added", [])) | set(added))
with open(path, "w") as fh:
    json.dump(rec, fh, indent=2)
os.chmod(path, 0o600)
'

installed_packages() {
  dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 2>/dev/null | awk '$1 == "ii" {print $2}' | sort -u
}

# ----------------------------------------------------------------- host mode
install_debian() {
  local install_dir=/opt/hasat-agent etc_dir=/etc/hasat-agent state_dir=/var/lib/hasat-agent
  command -v systemctl >/dev/null || fail "systemd is required"
  local model
  model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || uname -m)"
  info "Installing HA Satellite agent on $(hostname) (${model})"

  info "Installing dependencies ..."
  local before after added ser2net_conf=false
  [[ -f /etc/ser2net.yaml ]] && ser2net_conf=true
  before="$(installed_packages)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q --no-install-recommends \
    python3 python3-aiohttp python3-psutil ser2net nftables curl openssl ca-certificates >/dev/null
  after="$(installed_packages)"
  added="$(comm -13 <(printf '%s\n' "${before}") <(printf '%s\n' "${after}") | tr '\n' ' ')"

  if systemctl is-active --quiet hasat-agent 2>/dev/null; then
    info "Existing agent found - re-enrolling"
    systemctl stop hasat-agent
  fi

  download_agent "${install_dir}"
  request_certificate "${etc_dir}"
  mkdir -p "${state_dir}"
  chmod 700 "${state_dir}"
  python3 -c "${WRITE_FILES_PY}" "${TMP}/enroll.json" "${etc_dir}"
  python3 -c "${RECORD_PY}" "${state_dir}/install-record.json" \
    "{\"mode\": \"host\", \"ser2net_conf_existed\": ${ser2net_conf}}" "${added}"

  info "Installing service ..."
  install -m 0644 "${install_dir}/hasat-agent.service" /etc/systemd/system/hasat-agent.service
  systemctl daemon-reload
  systemctl enable --now hasat-agent >/dev/null 2>&1

  for _ in $(seq 1 20); do
    if systemctl is-active --quiet hasat-agent; then break; fi
    sleep 0.5
  done
  systemctl is-active --quiet hasat-agent || { journalctl -u hasat-agent -n 30 --no-pager; fail "agent did not start"; }

  info "Done. The agent listens on port ${AGENT_PORT} (mutual TLS, HA host only)."
  info "This device now appears under Satellites in Home Assistant."
}

# ------------------------------------------------------------ container mode
install_container() {
  local platform="$1"
  command -v docker >/dev/null || fail "Docker is required. On Synology, install Container Manager from Package Center first."
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running (start Container Manager)."
  local vol base model
  vol=/
  for candidate in /volume[0-9]*; do
    if [[ -d "${candidate}" ]]; then vol="${candidate}"; break; fi
  done
  if [[ "${vol}" == "/" ]]; then base=/opt/hasat; else base="${vol}/docker/hasat-agent"; fi
  model="$(cat /proc/sys/kernel/syno_hw_version 2>/dev/null || uname -m)"
  info "Installing HA Satellite agent on $(hostname) (${platform} ${model}) in container mode"
  info "Only health stats, the terminal and Docker management will be available."

  if docker inspect hasat-agent >/dev/null 2>&1; then
    info "Existing agent container found - re-enrolling"
    docker rm -f hasat-agent >/dev/null
  fi
  mkdir -p "${base}"
  chmod 700 "${base}"
  download_agent "${base}/hasat-agent"

  # The agent runs on the stock python image (nothing is built on this machine).
  local image=python:3.12-slim images=''
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    info "Downloading ${image} ..."
    docker pull -q "${image}" >/dev/null
    images="\"${image}\""  # removal deletes it again, since we downloaded it
  fi

  request_certificate "${base}/etc"
  mkdir -p "${base}/state"
  chmod 700 "${base}/state"
  docker run --rm -v "${TMP}:/t:ro" -v "${base}/etc:/e" "${image}" \
    python3 -c "${WRITE_FILES_PY}" /t/enroll.json /e
  docker run --rm -v "${base}/state:/s" "${image}" \
    python3 -c "${RECORD_PY}" /s/install-record.json \
    "{\"mode\": \"container\", \"host_base\": \"${base}\", \"images\": [${images}]}"

  info "Starting the agent container (first start installs its Python libraries) ..."
  sh "${base}/hasat-agent/container/run.sh" "${base}" "${vol}" "${image}" >/dev/null

  for _ in $(seq 1 180); do
    if docker logs hasat-agent 2>&1 | grep -q "listening on"; then break; fi
    sleep 1
  done
  if ! docker logs hasat-agent 2>&1 | grep -q "listening on"; then
    docker logs --tail 30 hasat-agent || true
    fail "agent container did not start"
  fi

  info "Done. The agent container listens on port ${AGENT_PORT} (mutual TLS)."
  if [[ "${platform}" == "Synology" ]]; then
    info "If the DSM firewall is enabled, allow TCP ${AGENT_PORT} from your Home Assistant host."
  fi
  info "This device now appears under Satellites in Home Assistant."
}

# ---------------------------------------------------------------------- main
[[ "$(id -u)" -eq 0 ]] || fail "run as root (pipe into 'sudo bash')"

if is_synology; then
  install_container "Synology"
elif command -v apt-get >/dev/null; then
  install_debian
elif command -v docker >/dev/null; then
  install_container "Linux"
else
  fail "unsupported system: needs Raspberry Pi OS / Debian / Ubuntu, or Docker (container mode, e.g. Synology Container Manager)"
fi
