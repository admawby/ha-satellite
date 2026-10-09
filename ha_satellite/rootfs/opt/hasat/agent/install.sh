#!/usr/bin/env bash
# HA Satellite agent installer.
#
# This script is generated per enrollment by the HA Satellite add-on. It contains a
# one-time token (valid 30 minutes) and the SHA-256 pin of the add-on's enrollment
# certificate, so every download below is authenticated against your HA instance.
#
#   * installs dependencies (python3-aiohttp, python3-psutil, ser2net, nftables)
#   * installs the agent to /opt/hasat-agent and a systemd unit
#   * generates the satellite's private key locally (it never leaves this machine)
#   * obtains a certificate from the add-on's private CA
set -euo pipefail

CONTROLLER_HOST="@@CONTROLLER_HOST@@"
CONTROLLER_PORT="@@CONTROLLER_PORT@@"
TOKEN="@@TOKEN@@"
PIN="@@PIN@@"
AGENT_PORT="@@AGENT_PORT@@"

BASE_URL="https://${CONTROLLER_HOST}:${CONTROLLER_PORT}/enroll/${TOKEN}"
INSTALL_DIR=/opt/hasat-agent
ETC_DIR=/etc/hasat-agent
STATE_DIR=/var/lib/hasat-agent

info() { printf '\033[1;34m[hasat]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[hasat] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

fetch() {
  curl -fsS --insecure --pinnedpubkey "sha256//${PIN}" --retry 3 --connect-timeout 10 "$@"
}

[[ "$(id -u)" -eq 0 ]] || fail "run as root (pipe into 'sudo bash')"
command -v apt-get >/dev/null || fail "only Debian-based systems (Raspberry Pi OS, Debian, Ubuntu) are supported"
command -v systemctl >/dev/null || fail "systemd is required"

MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || uname -m)"
info "Installing HA Satellite agent on $(hostname) (${MODEL})"

info "Installing dependencies ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  python3 python3-aiohttp python3-psutil ser2net nftables curl openssl ca-certificates >/dev/null

if systemctl is-active --quiet hasat-agent 2>/dev/null; then
  info "Existing agent found - re-enrolling"
  systemctl stop hasat-agent
fi

info "Downloading agent from ${CONTROLLER_HOST} ..."
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
fetch -o "${TMP}/agent.tar.gz" "${BASE_URL}/agent.tar.gz"
rm -rf "${INSTALL_DIR}.new"
mkdir -p "${INSTALL_DIR}.new"
tar -xzf "${TMP}/agent.tar.gz" -C "${INSTALL_DIR}.new"
[[ -f "${INSTALL_DIR}.new/hasat_agent/__init__.py" ]] || fail "downloaded agent is incomplete"
rm -rf "${INSTALL_DIR}"
mv "${INSTALL_DIR}.new" "${INSTALL_DIR}"

info "Generating satellite key and requesting certificate ..."
umask 077
mkdir -p "${ETC_DIR}" "${STATE_DIR}"
chmod 700 "${ETC_DIR}" "${STATE_DIR}"
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout "${ETC_DIR}/agent.key" -subj "/CN=hasat-pending" -out "${TMP}/agent.csr" 2>/dev/null
# Report the address this Pi uses towards HA (HA may only see a NAT address).
CTRL_IP="$(getent ahostsv4 "${CONTROLLER_HOST}" 2>/dev/null | awk 'NR==1{print $1}')"
SRC_IP="$(ip -4 route get "${CTRL_IP:-${CONTROLLER_HOST}}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
fetch -X POST -H "Content-Type: application/pkcs10" --data-binary "@${TMP}/agent.csr" \
  -o "${TMP}/enroll.json" "${BASE_URL}/csr?hostname=$(hostname)&ip=${SRC_IP}"

python3 - "${TMP}/enroll.json" "${ETC_DIR}" <<'PY'
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
PY

info "Installing service ..."
install -m 0644 "${INSTALL_DIR}/hasat-agent.service" /etc/systemd/system/hasat-agent.service
systemctl daemon-reload
systemctl enable --now hasat-agent >/dev/null 2>&1

for _ in $(seq 1 20); do
  if systemctl is-active --quiet hasat-agent; then break; fi
  sleep 0.5
done
systemctl is-active --quiet hasat-agent || { journalctl -u hasat-agent -n 30 --no-pager; fail "agent did not start"; }

info "Done. The agent listens on port ${AGENT_PORT} (mutual TLS, HA host only)."
info "This Pi now appears under Satellites in Home Assistant."
