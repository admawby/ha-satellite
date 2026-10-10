"""Container mode (Synology DSM) scenarios, run by test_e2e.py: the agent must refuse
everything except health, terminal and Docker, and HA must only get matching entities."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Callable

import aiohttp

from hasat import ha_bridge

DSM_VERSION = 'majorversion="7"\nminorversion="2"\nproductversion="7.2.1"\nbuildnumber="69057"\nsmallfixnumber="5"\n'


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, sat, fake, tmp: Path) -> None:
    proxy = f"{ui}/api/sat/{sat.id}/proxy"
    version_file = tmp / "VERSION"
    version_file.write_text(DSM_VERSION)
    saved = {k: os.environ.get(k) for k in ("HASAT_MODE", "HASAT_CONTAINER_NAME", "HASAT_HOST_VERSION")}
    os.environ.update(HASAT_MODE="container", HASAT_CONTAINER_NAME="hasat-agent", HASAT_HOST_VERSION=str(version_file))
    img = fake.make_image("hasat-agent:local", "1")
    fake.add_local(img)
    fake.run("hasat-agent", "hasat-agent:local", labels={"hasat.update": "false", "hasat.role": "agent"})
    try:
        await mgr.poll_one(sat.id)
        m = sat.metrics
        check(m["mode"] == "container" and m["features"] == ["health", "terminal", "docker"], "container mode: features reported")
        check(m["os"] == "DSM 7.2.1-69057 Update 5", "container mode: DSM version detected")
        check(m["radios"] == [] and m["serial"] == [] and m["updates"] == {}, "container mode: no radio/package data")
        check(m["cpu_percent"] is not None and m["mem_percent"] is not None and m["docker"]["available"], "container mode: health + docker stats")

        blocked = [
            ("GET", "usb"), ("GET", "serial"), ("GET", "storage"), ("GET", "files/list?path=/"),
            ("GET", "updates"), ("POST", "updates/apply"), ("GET", "logs"), ("POST", "system/reboot"),
            ("POST", "system/shutdown"), ("POST", "services/ssh/restart"), ("POST", "docker/install"),
        ]
        refused = []
        for method, path in blocked:
            async with http.request(method, f"{proxy}/{path}") as r:
                body = await r.json()
                if r.status == 502 and "not available on this satellite" in body.get("error", ""):
                    refused.append(path)
        check(len(refused) == len(blocked), f"container mode: agent refuses {len(refused)}/{len(blocked)} disabled endpoints")
        async with http.post(f"{ui}/api/sat/{sat.id}/files/upload", params={"path": "/tmp/x"}, data=b"x") as r:
            check(r.status == 502 and "not available" in (await r.json())["error"], "container mode: file upload refused")
        async with http.get(f"{ui}/api/sat/{sat.id}/files/download", params={"path": "/etc/hostname"}) as r:
            check(r.status == 502, "container mode: file download refused")

        async with http.get(f"{proxy}/docker") as r:
            d = await r.json()
            names = {c["name"]: c for c in d["containers"]}
            check(r.status == 200 and "hasat-agent" in names and names["hasat-agent"]["auto_update"] is False,
                  "container mode: Docker management works, agent container opted out")
        async with http.post(f"{proxy}/docker/containers/hasat-agent/stop") as r:
            check(r.status == 502 and "own container" in (await r.json())["error"], "container mode: agent cannot stop itself")
        async with http.post(f"{proxy}/docker/containers/hasat-agent/update") as r:
            assert r.status == 200
        for _ in range(50):
            async with http.get(f"{proxy}/docker") as r:
                job = (await r.json())["job"]
            if not job["running"]:
                break
            await asyncio.sleep(0.1)
        check("own container" in "\n".join(job["log"]) and fake.by_name("hasat-agent")["State"]["Running"],
              "container mode: agent refuses to update/recreate itself")
        async with http.get(f"{proxy}/settings") as r:
            check(r.status == 200, "container mode: settings still reachable")

        published: dict = {}

        class FakeMqtt:
            def publish(self, topic, payload, retain=False):
                published[topic] = payload

        bridge = ha_bridge.HABridge(lambda *_: None)
        bridge._mqtt, bridge._mqtt_connected = FakeMqtt(), True
        bridge._publish_mqtt(sat)
        cfg = lambda comp, key: published.get(f"homeassistant/{comp}/hasat_{sat.id}/{key}/config")  # noqa: E731
        check(json.loads(cfg("sensor", "cpu_temp"))["unique_id"].endswith("cpu_temp")
              and json.loads(cfg("sensor", "docker_updates")) and json.loads(cfg("button", "update_containers")),
              "container mode: health + docker entities announced")
        check(cfg("button", "reboot") == "" and cfg("sensor", "updates_available") == ""
              and cfg("binary_sensor", "throttled") == "" and cfg("button", "apply_updates") == "",
              "container mode: reboot/package/throttle entities removed")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        await mgr.poll_one(sat.id)
    check("radios" in sat.metrics["features"], "host mode restored")
