"""Add-on options and filesystem locations."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("HASAT_DATA_DIR", "/data"))
OPTIONS_FILE = DATA_DIR / "options.json"
AGENT_SRC_DIR = Path(os.environ.get("HASAT_AGENT_DIR", "/opt/hasat/agent"))
WEB_DIR = Path(__file__).parent / "web"

SUPERVISOR_URL = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

INGRESS_PORT = 8099
ENROLL_PORT = 8766
# Requests to the UI port are only accepted from the Supervisor's ingress proxy.
INGRESS_PROXY_IPS = {"172.30.32.2", "127.0.0.1", "::1"}


@dataclass
class Options:
    enrollment_host: str = ""
    allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    extra_trusted_ips: list[str] = field(default_factory=list)
    agent_port: int = 8765
    poll_interval: int = 30
    auto_update_agents: bool = True
    publish_to_ha: bool = True
    log_level: str = "info"

    def address_allowed(self, address: str) -> bool:
        """True if an IP address falls inside one of the allowed (local) networks."""
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.allowed_networks)


def load_options() -> Options:
    raw: dict = {}
    if OPTIONS_FILE.exists():
        raw = json.loads(OPTIONS_FILE.read_text())
    nets = []
    for item in raw.get("allowed_networks") or ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
        nets.append(ipaddress.ip_network(item.strip(), strict=False))
    return Options(
        enrollment_host=(raw.get("enrollment_host") or "").strip(),
        allowed_networks=nets,
        extra_trusted_ips=[i.strip() for i in raw.get("extra_trusted_ips") or [] if i.strip()],
        agent_port=int(raw.get("agent_port", 8765)),
        poll_interval=int(raw.get("poll_interval", 30)),
        auto_update_agents=bool(raw.get("auto_update_agents", True)),
        publish_to_ha=bool(raw.get("publish_to_ha", True)),
        log_level=raw.get("log_level", "info"),
    )
