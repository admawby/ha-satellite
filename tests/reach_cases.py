"""Ping fallback: "device up, agent not responding" vs "device offline"."""

from __future__ import annotations

import time
from typing import Callable

import aiohttp


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, free_port) -> None:
    from hasat import ha_bridge, reachability
    from hasat.store import Satellite

    # Device answers, agent port closed (agent not installed / service stopped).
    up = Satellite(id="sat-a9e17d00", name="Agentless Pi", host="127.0.0.1", port=free_port())
    # Nothing answers at all (TEST-NET-1 address, never routed).
    gone = Satellite(id="sat-0ff0ff00", name="Unplugged Pi", host="192.0.2.1", port=8765)
    mgr.store.satellites[up.id] = up
    mgr.store.satellites[gone.id] = gone
    try:
        await mgr.poll_one(up.id)
        check(up.status == "agent_down" and up.reach["up"] and "not running" in up.reach["reason"],
              f"reach: device up + agent not running -> 'agent_down' ({up.reach['method']}, {up.reach['rtt_ms']} ms)")
        checked = up.reach["checked"]
        await mgr.poll_one(up.id)
        check(up.reach["checked"] == checked, "reach: device not pinged again within 10 s")
        t0 = time.monotonic()
        await mgr.poll_one(gone.id)
        check(gone.status == "offline" and gone.reach and gone.reach["up"] is False,
              f"reach: no ping, no TCP -> 'offline' ({time.monotonic() - t0:.1f}s)")

        async with http.get(f"{ui}/api/state") as r:
            sats = {s["id"]: s for s in (await r.json())["satellites"]}
        check(sats[up.id]["status"] == "agent_down" and sats[up.id]["reach"]["reason"]
              and sats[gone.id]["status"] == "offline", "reach: UI receives status and reason")

        published: dict = {}

        class FakeMqtt:
            def publish(self, topic, payload, retain=False):
                published[topic] = payload

        b = ha_bridge.HABridge(lambda *_: None)
        b._mqtt, b._mqtt_connected = FakeMqtt(), True
        b._publish_mqtt(up)
        check(published[f"hasat/{up.id}/connection"] == "agent_down", "reach: HA Connection sensor says 'agent_down'")
    finally:
        mgr.store.satellites.pop(up.id, None)
        mgr.store.satellites.pop(gone.id, None)

    check("certificate" in reachability.explain("certificate rejected: unable to get local issuer")
          and "not running" in reachability.explain("Cannot connect to host 1.2.3.4:8765 [Connect call failed]")
          and "firewall" in reachability.explain("TimeoutError"),
          "reach: errors explained in plain English")
