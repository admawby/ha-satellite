"""Expose local USB radios over TCP with ser2net so HA integrations can use them remotely.

Z-Wave JS:      tcp://<pi-ip>:<port>
Zigbee2MQTT:    tcp://<pi-ip>:<port>
ZHA:            socket://<pi-ip>:<port>
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

SER2NET_CONF = Path("/etc/ser2net.yaml")
BACKUP = Path("/etc/ser2net.yaml.hasat-orig")
VALID_BAUD = {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9_./:+@-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{0,40}$")


def validate(bridges: Any, agent_port: int) -> List[Dict[str, Any]]:
    if not isinstance(bridges, list):
        raise ValueError("serial must be a list")
    clean: List[Dict[str, Any]] = []
    ports, devices = set(), set()
    for item in bridges:
        device = str(item.get("device", ""))
        port = int(item.get("port", 0))
        baud = int(item.get("baud", 115200))
        name = str(item.get("name", ""))
        if not _DEVICE_RE.match(device) or ".." in device:
            raise ValueError(f"invalid device path: {device!r}")
        if not 1024 <= port <= 65535 or port == agent_port:
            raise ValueError(f"invalid port {port} (1024-65535, not the agent port)")
        if baud not in VALID_BAUD:
            raise ValueError(f"unsupported baud rate {baud}")
        if not _NAME_RE.match(name):
            raise ValueError("invalid name")
        if port in ports or device in devices:
            raise ValueError("each port and device may only be used once")
        ports.add(port)
        devices.add(device)
        clean.append({
            "name": name,
            "device": device,
            "port": port,
            "baud": baud,
            "rtscts": bool(item.get("rtscts", False)),
            "enabled": bool(item.get("enabled", True)),
        })
    return clean


def render(bridges: List[Dict[str, Any]]) -> str:
    out = [
        "%YAML 1.1",
        "---",
        "# Managed by the HA Satellite agent. Manual changes will be overwritten.",
        "",
    ]
    for i, b in enumerate(bridges, 1):
        connector = f"serialdev,{b['device']},{b['baud']}n81,local"
        if b["rtscts"]:
            connector += ",rtscts"
        out += [
            f"connection: &hasat{i}",
            f"  accepter: tcp,{b['port']}",
            f"  enable: {'on' if b['enabled'] else 'off'}",
            "  options:",
            "    kickolduser: true",
            f"  connector: {connector}",
            "",
        ]
    return "\n".join(out)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], capture_output=True, timeout=30)


def apply(bridges: List[Dict[str, Any]]) -> Optional[str]:
    if not shutil.which("ser2net"):
        return "ser2net is not installed" if bridges else None
    if SER2NET_CONF.exists() and not BACKUP.exists():
        shutil.copy2(SER2NET_CONF, BACKUP)
    tmp = SER2NET_CONF.with_suffix(".tmp")
    tmp.write_text(render(bridges))
    os.replace(tmp, SER2NET_CONF)
    if any(b["enabled"] for b in bridges):
        _systemctl("enable", "ser2net")
        _systemctl("restart", "ser2net")
    else:
        _systemctl("disable", "--now", "ser2net")
    return None


def status(bridges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        listening = {
            c.laddr.port for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN
        }
        clients: Dict[int, List[str]] = {}
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_ESTABLISHED and c.laddr and c.raddr:
                clients.setdefault(c.laddr.port, []).append(c.raddr.ip)
    except (psutil.Error, OSError):
        listening, clients = set(), {}
    result = []
    for b in bridges:
        item = dict(b)
        item["present"] = os.path.exists(b["device"])
        item["listening"] = b["port"] in listening
        item["clients"] = clients.get(b["port"], [])
        result.append(item)
    return result


def restore() -> None:
    if BACKUP.exists():
        shutil.move(str(BACKUP), str(SER2NET_CONF))
