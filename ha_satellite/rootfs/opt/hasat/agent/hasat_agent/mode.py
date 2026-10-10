"""Agent mode and the features it offers.

* host mode (default): installed directly on Raspberry Pi OS / Debian / Ubuntu.
* container mode: runs as a Docker container on appliances such as Synology DSM,
  where the OS cannot be modified. Only health stats, the terminal (entered into the
  host's namespaces with nsenter) and Docker management are offered; every other
  endpoint is refused by the agent itself.
"""

from __future__ import annotations

import os
from typing import List, Optional

ALL_FEATURES = (
    "health", "terminal", "docker", "docker_install", "radios", "files",
    "updates", "logs", "power", "services", "firewall",
)
CONTAINER_FEATURES = ("health", "terminal", "docker")

# Longest prefixes first; endpoints not listed here are always available
# (status, settings, agent update/uninstall).
ROUTE_FEATURES = (
    ("/api/docker/install", "docker_install"),
    ("/api/docker", "docker"),
    ("/api/usb", "radios"),
    ("/api/serial", "radios"),
    ("/api/storage", "files"),
    ("/api/files", "files"),
    ("/api/updates", "updates"),
    ("/api/logs", "logs"),
    ("/api/system/", "power"),
    ("/api/services/", "services"),
    ("/api/exec", "terminal"),
    ("/api/terminal", "terminal"),
)


def container_mode() -> bool:
    return os.environ.get("HASAT_MODE") == "container"


def mode_name() -> str:
    return "container" if container_mode() else "host"


def features() -> List[str]:
    return list(CONTAINER_FEATURES if container_mode() else ALL_FEATURES)


def feature_for(path: str) -> Optional[str]:
    for prefix, feature in ROUTE_FEATURES:
        base = prefix.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return feature
    return None


def own_container() -> Optional[str]:
    """Name of the agent's own container in container mode."""
    return os.environ.get("HASAT_CONTAINER_NAME") if container_mode() else None


def host_cmd(argv: List[str]) -> List[str]:
    """Run a command in the host's namespaces when the agent lives in a container."""
    if not container_mode():
        return argv
    return ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"] + argv
