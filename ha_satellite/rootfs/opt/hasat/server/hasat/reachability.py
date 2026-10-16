"""Is the device itself up? Used when its satellite agent does not answer, to tell
"device offline" apart from "device up, but the HA Satellite agent is not working".

ICMP ping first (latency included); if ICMP is unavailable or filtered, a TCP probe of a
few common ports: any answer, even "connection refused", proves the host is up.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Optional

TCP_PORTS = (22, 80, 443, 8765, 5000, 5001)  # SSH, web, agent, Synology DSM
_RTT = re.compile(r"time[=<]\s*([\d.]+)\s*ms")


@dataclass
class Reachability:
    up: bool
    method: str               # "ping" | "tcp" | "none"
    rtt_ms: Optional[float] = None
    checked: float = 0.0


async def _icmp(host: str, timeout: float) -> Optional[Reachability]:
    exe = shutil.which("ping")
    if not exe:
        return None
    if os.name == "nt":  # development on Windows
        cmd = [exe, "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = [exe, "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 2)
    except (OSError, asyncio.TimeoutError):
        return None
    text = out.decode(errors="replace")
    if proc.returncode == 0:
        m = _RTT.search(text)
        return Reachability(True, "ping", float(m.group(1)) if m else None, time.time())
    if "permission" in text.lower() or "operation not permitted" in text.lower():
        return None  # no ICMP allowed in this environment: fall back to TCP
    return Reachability(False, "ping", None, time.time())


async def _tcp(host: str, timeout: float) -> Reachability:
    async def probe(port: int) -> bool:
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            w.close()
            return True
        except ConnectionRefusedError:
            return True  # the host itself answered with a reset
        except (OSError, asyncio.TimeoutError):
            return False

    start = time.monotonic()
    results = await asyncio.gather(*(probe(p) for p in TCP_PORTS))
    up = any(results)
    return Reachability(up, "tcp", round((time.monotonic() - start) * 1000, 1) if up else None, time.time())


async def check(host: str, timeout: float = 2.0) -> Reachability:
    icmp = await _icmp(host, timeout)
    if icmp is not None and icmp.up:
        return icmp
    tcp = await _tcp(host, timeout)  # ICMP blocked/filtered or unavailable: try TCP
    if tcp.up or icmp is None:
        return tcp
    return Reachability(False, "ping+tcp", None, time.time())


def explain(error: str) -> str:
    """Plain-English reason the agent is not answering on a reachable device."""
    e = error.lower()
    if "certificate" in e:
        return "the agent's certificate is not trusted (it was set up by another HA Satellite install); launch it again"
    if "refused" in e or "connect call failed" in e:
        return "the agent is not running (not installed, removed, or its service stopped)"
    if "timeout" in e:
        return "the agent does not answer (hung, or a firewall drops the connection)"
    if "403" in e:
        return "the agent refused the request"
    return error or "unknown error"
