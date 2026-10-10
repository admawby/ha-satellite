"""System information and health metrics."""

from __future__ import annotations

import datetime as dt
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

THROTTLE_BITS = {
    0: "under_voltage_now",
    1: "freq_capped_now",
    2: "throttled_now",
    3: "soft_temp_limit_now",
    16: "under_voltage_occurred",
    17: "freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(errors="ignore").strip("\x00\n ")
    except OSError:
        return None


def prime() -> None:
    """psutil's first cpu_percent() call always returns 0; call once at startup."""
    psutil.cpu_percent(interval=None)


def cpu_temp() -> Optional[float]:
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        temps = {}
    for name in ("cpu_thermal", "coretemp", "k10temp", "soc_thermal"):
        if temps.get(name):
            return round(temps[name][0].current, 1)
    for entries in temps.values():
        if entries:
            return round(entries[0].current, 1)
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.lstrip("-").isdigit():
        return round(int(raw) / 1000, 1)
    return None


def throttled() -> Optional[Dict[str, Any]]:
    value: Optional[int] = None
    raw = _read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if raw:
        try:
            value = int(raw, 16)
        except ValueError:
            value = None
    if value is None and shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
            value = int(out.strip().split("=")[1], 16)
        except (subprocess.SubprocessError, IndexError, ValueError, OSError):
            value = None
    if value is None:
        return None
    result: Dict[str, Any] = {"raw": hex(value)}
    for bit, name in THROTTLE_BITS.items():
        result[name] = bool(value & (1 << bit))
    return result


def _dsm_version() -> Optional[str]:
    raw = _read(os.environ.get("HASAT_HOST_VERSION", "/etc.defaults/VERSION"))
    if not raw or "productversion" not in raw:
        return None
    kv = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    kv = {k.strip(): v.strip().strip('"') for k, v in kv.items()}
    text = f"DSM {kv.get('productversion', '?')}-{kv.get('buildnumber', '?')}"
    if kv.get("smallfixnumber", "0") not in ("", "0"):
        text += f" Update {kv['smallfixnumber']}"
    return text


def _os_name() -> str:
    dsm = _dsm_version()
    if dsm:
        return dsm
    if os.environ.get("HASAT_MODE") == "container":
        return f"Linux {platform.release()} (container agent)"
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _synology_model() -> Optional[str]:
    model = _read("/proc/sys/kernel/syno_hw_version")
    return f"Synology {model}" if model else None


def addresses() -> Dict[str, list]:
    result: Dict[str, list] = {}
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo" or iface.startswith(("docker", "veth", "br-")):
            continue
        ips = [a.address.split("%")[0] for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)]
        if ips:
            result[iface] = ips
    return result


def collect() -> Dict[str, Any]:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("HASAT_DISK_PATH", "/"))
    boot = psutil.boot_time()
    return {
        "hostname": socket.gethostname(),
        "model": _read("/proc/device-tree/model") or _synology_model() or platform.machine(),
        "os": _os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": psutil.cpu_count(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_temp": cpu_temp(),
        "cpu_freq_mhz": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
        "load": [round(x, 2) for x in psutil.getloadavg()],
        "mem_percent": mem.percent,
        "mem_total": mem.total,
        "mem_used": mem.total - mem.available,
        "disk_percent": disk.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "uptime": int(dt.datetime.now().timestamp() - boot),
        "boot_time_iso": dt.datetime.fromtimestamp(boot, dt.timezone.utc).isoformat(),
        "throttled": throttled(),
        "addresses": addresses(),
    }
