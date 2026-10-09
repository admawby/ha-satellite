"""Discover USB / serial radios attached to the Pi."""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Well-known Z-Wave / Zigbee / Thread sticks, keyed by "vid:pid".
KNOWN_RADIOS = {
    "10c4:ea60": "Silicon Labs CP210x (Sonoff ZBDongle-P, Zooz ZST39, SkyConnect, ...)",
    "10c4:8a2a": "Nortek HUSBZB-1 (Z-Wave + Zigbee)",
    "1a86:55d4": "CH9102 (Sonoff ZBDongle-E, SMLight SLZB-06U, ...)",
    "1a86:7523": "CH340 serial (various Zigbee sticks)",
    "0658:0200": "Sigma Designs Z-Wave (Aeotec Z-Stick, Zooz ZST10, ...)",
    "1cf1:0030": "Dresden Elektronik ConBee II",
    "1cf1:0031": "Dresden Elektronik ConBee III",
    "0451:16a8": "Texas Instruments CC2531",
    "0451:bef3": "Texas Instruments CC1352/CC2652 LaunchPad",
    "1915:520f": "Nordic nRF52840 dongle",
    "303a:4001": "Espressif ESP32 (Zigbee/Thread)",
    "0403:6015": "FTDI FT231X (Zooz ZST10 700, ...)",
    "0403:6001": "FTDI FT232R",
}


def _sysattr(start: Path, name: str) -> Optional[str]:
    """Walk up the sysfs tree from a tty device until the USB attribute is found."""
    cur = start
    for _ in range(8):
        f = cur / name
        if f.exists():
            try:
                return f.read_text().strip()
            except OSError:
                return None
        cur = cur.parent
        if str(cur) in ("/", "/sys", "/sys/devices"):
            break
    return None


def _by_id_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        mapping[os.path.realpath(link)] = link
    return mapping


def serial_devices() -> List[Dict[str, Any]]:
    by_id = _by_id_map()
    devices = []
    candidates = sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyAMA*")))
    for dev in candidates:
        name = os.path.basename(dev)
        sys_dev = Path(f"/sys/class/tty/{name}/device")
        if not sys_dev.exists():
            continue
        real = sys_dev.resolve()
        vid = _sysattr(real, "idVendor")
        pid = _sysattr(real, "idProduct")
        key = f"{vid}:{pid}" if vid and pid else None
        devices.append({
            "device": dev,
            "by_id": by_id.get(dev),
            "path": by_id.get(dev) or dev,  # stable path preferred for bridges
            "usb": bool(vid),
            "vid": vid,
            "pid": pid,
            "manufacturer": _sysattr(real, "manufacturer"),
            "product": _sysattr(real, "product"),
            "serial": _sysattr(real, "serial"),
            "hint": KNOWN_RADIOS.get(key or "", None),
        })
    return devices


def usb_tree() -> List[Dict[str, Any]]:
    out = []
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        p = Path(dev)
        vid = p / "idVendor"
        if not vid.exists():
            continue

        def read(n: str) -> Optional[str]:
            try:
                return (p / n).read_text().strip()
            except OSError:
                return None

        out.append({
            "bus_path": p.name,
            "vid": read("idVendor"),
            "pid": read("idProduct"),
            "manufacturer": read("manufacturer"),
            "product": read("product"),
            "speed": read("speed"),
        })
    return out
