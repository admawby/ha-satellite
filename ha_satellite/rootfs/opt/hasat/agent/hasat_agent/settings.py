"""Agent identity (written by the installer) and mutable settings (pushed by the manager)."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict

ETC = Path(os.environ.get("HASAT_ETC", "/etc/hasat-agent"))
STATE = Path(os.environ.get("HASAT_STATE", "/var/lib/hasat-agent"))
CONFIG_FILE = ETC / "config.json"
SETTINGS_FILE = ETC / "settings.json"
CERT_FILE = ETC / "agent.crt"
KEY_FILE = ETC / "agent.key"
CA_FILE = ETC / "ca.crt"
INSTALL_DIR = Path(os.environ.get("HASAT_INSTALL_DIR", "/opt/hasat-agent"))
SYSTEMD_DIR = Path(os.environ.get("HASAT_SYSTEMD_DIR", "/etc/systemd/system"))

DEFAULT_SETTINGS: Dict[str, Any] = {
    "auto_update": {
        "enabled": True,
        "time": "04:00",
        "days": [0, 1, 2, 3, 4, 5, 6],  # Monday = 0
        "full_upgrade": False,
        "autoremove": True,
        "auto_reboot": False,
    },
    # Off by default: recreating containers on a schedule must be an explicit choice.
    "docker_update": {
        "enabled": False,
        "mode": "update",  # "check" = only report new images, "update" = pull + recreate
        "time": "05:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "prune": True,
        "exclude": [],
    },
    "firewall_enabled": True,
    "terminal_user": "root",
    "serial": [],
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text())


def save_config(cfg: Dict[str, Any]) -> None:
    _write_json(CONFIG_FILE, cfg)


def load_settings() -> Dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        stored = json.loads(SETTINGS_FILE.read_text())
        for key, val in stored.items():
            if isinstance(val, dict) and isinstance(settings.get(key), dict):
                settings[key].update(val)
            else:
                settings[key] = val
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    _write_json(SETTINGS_FILE, settings)


def merge_settings(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and merge a partial settings update (serial bridges are handled separately)."""
    new = copy.deepcopy(current)
    au = patch.get("auto_update")
    if isinstance(au, dict):
        _merge_schedule(new["auto_update"], au, ("enabled", "full_upgrade", "autoremove", "auto_reboot"))
    du = patch.get("docker_update")
    if isinstance(du, dict):
        _merge_schedule(new["docker_update"], du, ("enabled", "prune"))
        if "mode" in du:
            if du["mode"] not in ("check", "update"):
                raise ValueError("mode must be 'check' or 'update'")
            new["docker_update"]["mode"] = du["mode"]
        if "exclude" in du:
            names = [str(n) for n in du["exclude"]]
            if not all(CONTAINER_RE.match(n) for n in names):
                raise ValueError("invalid container name in exclude list")
            new["docker_update"]["exclude"] = sorted(set(names))
    if "firewall_enabled" in patch:
        new["firewall_enabled"] = bool(patch["firewall_enabled"])
    if "terminal_user" in patch:
        user = str(patch["terminal_user"])
        if not _USER_RE.match(user):
            raise ValueError("invalid terminal user")
        new["terminal_user"] = user
    return new


def _merge_schedule(target: Dict[str, Any], patch: Dict[str, Any], flags: tuple) -> None:
    """Validate the common schedule fields (boolean flags, HH:MM time, weekdays)."""
    for key in flags:
        if key in patch:
            target[key] = bool(patch[key])
    if "time" in patch:
        if not _TIME_RE.match(str(patch["time"])):
            raise ValueError("time must be HH:MM")
        target["time"] = str(patch["time"])
    if "days" in patch:
        target["days"] = sorted({int(d) for d in patch["days"] if 0 <= int(d) <= 6})
