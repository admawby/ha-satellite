"""Network throughput per physical interface.

Rates are computed between two reads of the kernel byte counters (like CPU usage),
so they reflect the period since the previous poll. Only physical interfaces are
counted (Ethernet, Wi-Fi: they have /sys/class/net/<if>/device); Docker bridges, veth
pairs, VPN tunnels, bond/OVS masters and loopback are skipped so traffic is not
counted twice. Counter resets (reboot, driver reload) are handled.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import psutil

SYS_NET = os.environ.get("HASAT_SYS_NET", "/sys/class/net")
_LAST: Dict[str, Tuple[float, int, int]] = {}  # iface -> (monotonic, rx bytes, tx bytes)


def _physical(iface: str) -> bool:
    return os.path.exists(f"{SYS_NET}/{iface}/device")


def _wireless(iface: str) -> bool:
    return os.path.exists(f"{SYS_NET}/{iface}/wireless") or os.path.exists(f"{SYS_NET}/{iface}/phy80211")


def _rate(prev: Optional[Tuple[float, int, int]], now: float, rx: int, tx: int) -> Tuple[Optional[float], Optional[float]]:
    if not prev or now <= prev[0]:
        return None, None
    drx, dtx = rx - prev[1], tx - prev[2]
    if drx < 0 or dtx < 0:  # counters reset
        return None, None
    span = now - prev[0]
    return drx / span, dtx / span


def collect() -> Dict[str, Any]:
    """{"interfaces": [...], "rx_bps", "tx_bps" (bytes/s), "rx_total", "tx_total" (bytes)}"""
    now = time.monotonic()
    try:
        counters = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return {"interfaces": []}
    addrs = psutil.net_if_addrs()
    interfaces: List[Dict[str, Any]] = []
    for iface, c in sorted(counters.items()):
        if not _physical(iface):
            continue
        st = stats.get(iface)
        rx_bps, tx_bps = _rate(_LAST.get(iface), now, c.bytes_recv, c.bytes_sent)
        _LAST[iface] = (now, c.bytes_recv, c.bytes_sent)
        ipv4 = next((a.address for a in addrs.get(iface, []) if a.family == 2), None)  # AF_INET
        interfaces.append({
            "name": iface,
            "up": bool(st and st.isup),
            "wireless": _wireless(iface),
            "speed_mbps": st.speed if st and st.speed > 0 else None,
            "ipv4": ipv4,
            "rx_bps": round(rx_bps, 1) if rx_bps is not None else None,
            "tx_bps": round(tx_bps, 1) if tx_bps is not None else None,
            "rx_total": c.bytes_recv,
            "tx_total": c.bytes_sent,
            "errors": c.errin + c.errout,
            "drops": c.dropin + c.dropout,
        })
    rates = [i for i in interfaces if i["rx_bps"] is not None]
    return {
        "interfaces": interfaces,
        "rx_bps": round(sum(i["rx_bps"] for i in rates), 1) if rates else None,
        "tx_bps": round(sum(i["tx_bps"] for i in rates), 1) if rates else None,
        "rx_total": sum(i["rx_total"] for i in interfaces),
        "tx_total": sum(i["tx_total"] for i in interfaces),
    }
