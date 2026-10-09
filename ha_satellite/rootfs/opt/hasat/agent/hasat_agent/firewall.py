"""nftables rules: only the HA host may reach the agent and serial-bridge ports.

The rules live in their own table (inet hasat) and only match our ports, so SSH and
anything else on the Pi is never affected.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess
from typing import Iterable, List, Optional

_LOGGER = logging.getLogger(__name__)


def _run_nft(script: str) -> Optional[str]:
    nft = shutil.which("nft")
    if not nft:
        return "nft not installed"
    proc = subprocess.run([nft, "-f", "-"], input=script, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        return proc.stderr.strip() or "nft failed"
    return None


def apply(enabled: bool, trusted: Iterable[str], ports: Iterable[int]) -> Optional[str]:
    """Install or remove the rule set. Returns an error string or None."""
    ports = sorted({int(p) for p in ports})
    if not enabled or not ports:
        return remove()
    v4: List[str] = []
    v6: List[str] = []
    for item in trusted:
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        (v4 if net.version == 4 else v6).append(str(net))
    if not v4 and not v6:
        # Never lock the manager out because of an empty trust list.
        _LOGGER.warning("No trusted controller IPs configured; firewall disabled")
        return remove()
    port_set = "{ " + ", ".join(str(p) for p in ports) + " }"
    lines = [
        "table inet hasat",
        "delete table inet hasat",
        "table inet hasat {",
        "  chain input {",
        "    type filter hook input priority -5; policy accept;",
        '    iif "lo" accept',
    ]
    if v4:
        lines.append(f"    tcp dport {port_set} ip saddr {{ {', '.join(v4)} }} accept")
    if v6:
        lines.append(f"    tcp dport {port_set} ip6 saddr {{ {', '.join(v6)} }} accept")
    lines += [f"    tcp dport {port_set} counter drop", "  }", "}"]
    err = _run_nft("\n".join(lines) + "\n")
    if err:
        _LOGGER.error("Firewall update failed: %s", err)
    return err


def remove() -> Optional[str]:
    return _run_nft("table inet hasat\ndelete table inet hasat\n")
