"""Docker-tab scenarios run by test_e2e.py against the fake Docker Engine, through the
real UI proxy -> mTLS -> agent -> Engine API path."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable

import aiohttp

from fake_docker import FakeDocker


def seed(fake: FakeDocker) -> None:
    z1 = fake.make_image("koenkk/zigbee2mqtt:latest", "1.0")
    z2 = fake.make_image("koenkk/zigbee2mqtt:latest", "2.0")
    fake.add_local(z1)
    fake.publish(z2)
    fake.run(
        "zigbee2mqtt", "koenkk/zigbee2mqtt:latest",
        env=["TZ=Europe/London", "ZIGBEE2MQTT_DATA=/app/data"],
        labels={"com.docker.compose.project": "home", "com.docker.compose.service": "z2m"},
        host={"NetworkMode": "home_default", "Binds": ["/opt/z2m:/app/data"],
              "PortBindings": {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]},
              "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
              "Devices": [{"PathOnHost": "/dev/ttyUSB0", "PathInContainer": "/dev/ttyUSB0", "CgroupPermissions": "rwm"}]},
        networks={"home_default": {"Aliases": ["z2m"], "IPAMConfig": None}, "mqtt_net": {"Aliases": ["zigbee"]}},
    )
    # opted out by label
    p1 = fake.make_image("portainer/portainer-ce:latest", "1")
    fake.add_local(p1)
    fake.publish(fake.make_image("portainer/portainer-ce:latest", "2"))
    fake.run("portainer", "portainer/portainer-ce:latest", labels={"hasat.update": "false"})
    # pinned by digest -> never updated
    n1 = fake.make_image("nginx:1.27", "1")
    fake.add_local(n1)
    fake.tags["nginx@sha256:abc"] = n1["Id"]
    fake.run("web", "nginx@sha256:abc")
    # recreate will fail -> must roll back
    b1 = fake.make_image("example/broken:latest", "1")
    fake.add_local(b1)
    fake.publish(fake.make_image("example/broken:latest", "2"))
    fake.run("broken", "example/broken:latest", labels={"test.fail-start": "true"})
    # stopped container, up to date
    m1 = fake.make_image("eclipse-mosquitto:2", "2")
    fake.add_local(m1)
    fake.publish(m1)
    fake.run("mosquitto", "eclipse-mosquitto:2", running=False)


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, sat_id: str,
              agent, fake: FakeDocker) -> None:
    base = f"{ui}/api/sat/{sat_id}/proxy"

    async def get_docker() -> dict:
        async with http.get(f"{base}/docker") as r:
            assert r.status == 200, await r.text()
            return await r.json()

    async def wait_job() -> dict:
        for _ in range(100):
            d = await get_docker()
            if not d["job"]["running"]:
                return d
            await asyncio.sleep(0.1)
        raise AssertionError("docker job did not finish")

    d = await get_docker()
    names = {c["name"]: c for c in d["containers"]}
    check(d["available"] and set(names) == {"zigbee2mqtt", "portainer", "web", "broken", "mosquitto"}, "docker: containers listed")
    check(names["zigbee2mqtt"]["project"] == "home" and names["zigbee2mqtt"]["ports"] == ["8080->8080/tcp"], "docker: stack + ports shown")
    check(names["portainer"]["skip_reason"].startswith("label hasat.update") and names["web"]["skip_reason"].startswith("image pinned"),
          "docker: opt-out label and pinned image detected")
    check(d["settings"]["enabled"] is False, "docker: scheduled updates are off by default")

    async with http.post(f"{base}/docker/check") as r:
        check(r.status == 200, "docker: check started")
    d = await wait_job()
    names = {c["name"]: c for c in d["containers"]}
    check(names["zigbee2mqtt"]["update_available"] and names["broken"]["update_available"], "docker: registry digest check finds updates")
    check(not names["mosquitto"]["update_available"] and names["mosquitto"]["checked"], "docker: up-to-date image reported")
    check(not any(c == "pull" for c, _ in fake.calls), "docker: check does not pull images")

    async with http.get(f"{base}/docker/containers/zigbee2mqtt/logs?lines=50") as r:
        out = (await r.json())["output"]
        check("hello from stdout" in out and "warning on stderr" in out and "\x01" not in out, "docker: logs demultiplexed")

    async with http.post(f"{base}/docker/containers/mosquitto/start") as r:
        check(r.status == 200 and fake.by_name("mosquitto")["State"]["Running"], "docker: start container")
    async with http.post(f"{base}/docker/containers/mosquitto/stop") as r:
        check(r.status == 200 and not fake.by_name("mosquitto")["State"]["Running"], "docker: stop container")
    async with http.post(f"{base}/docker/containers/bad;name/restart") as r:
        check(r.status == 403, "docker: invalid container names blocked at the proxy")

    old = fake.by_name("zigbee2mqtt")
    old_id, old_image = old["Id"], old["Image"]
    async with http.post(f"{base}/docker/containers/zigbee2mqtt/update") as r:
        check(r.status == 200, "docker: single container update started")
    d = await wait_job()
    new = fake.by_name("zigbee2mqtt")
    env = dict(e.split("=", 1) for e in new["Config"]["Env"])
    check(new["Id"] != old_id and old_id not in fake.containers, "docker: container recreated, old one removed")
    check(new["Image"] == fake.remote["koenkk/zigbee2mqtt:latest"]["Id"] and new["State"]["Running"], "docker: new image running")
    check(env.get("TZ") == "Europe/London" and env.get("IMAGE_VERSION") == "2.0", "docker: user env kept, image env from new image")
    check(new["Config"]["Labels"].get("com.docker.compose.project") == "home"
          and new["Config"]["Labels"].get("org.opencontainers.image.version") == "2.0", "docker: compose labels kept, image labels refreshed")
    hc = new["HostConfig"]
    check(hc["Binds"] == ["/opt/z2m:/app/data"] and hc["PortBindings"]["8080/tcp"][0]["HostPort"] == "8080"
          and hc["RestartPolicy"]["Name"] == "unless-stopped" and hc["Devices"][0]["PathOnHost"] == "/dev/ttyUSB0",
          "docker: volumes, ports, restart policy, USB device kept")
    nets = new["NetworkSettings"]["Networks"]
    check(set(nets) == {"home_default", "mqtt_net"} and "z2m" in nets["home_default"]["Aliases"]
          and "zigbee" in nets["mqtt_net"]["Aliases"] and old_id[:12] not in nets["home_default"]["Aliases"],
          "docker: all networks and aliases restored")
    check(new["Config"]["Hostname"] == new["Id"][:12], "docker: auto hostname not copied from old container")
    check(("rmi", old_image) in fake.calls, "docker: old image pruned")
    check(not {c["name"]: c for c in d["containers"]}["zigbee2mqtt"]["update_available"], "docker: no longer flagged after update")

    broken = fake.by_name("broken")
    async with http.post(f"{base}/docker/containers/broken/update") as r:
        check(r.status == 200, "docker: update of failing container started")
    d = await wait_job()
    restored = fake.by_name("broken")
    check(restored["Id"] == broken["Id"] and restored["State"]["Running"] and fake.find("broken-hasat-old") is None,
          "docker: failed recreate rolls back to the original container")
    check(any("restoring the original container" in line for line in d["job"]["log"]) and "failed 1" in d["job"]["last_result"],
          "docker: failure reported in job log")

    async with http.put(f"{base}/settings", json={"docker_update": {"enabled": True, "mode": "bogus"}}) as r:
        check(r.status == 502, "docker: invalid schedule rejected")
    now = dt.datetime.now()
    sched = {"docker_update": {"enabled": True, "mode": "update", "time": now.strftime("%H:%M"),
                               "days": [now.weekday()], "prune": False, "exclude": ["broken"]}}
    async with http.put(f"{base}/settings", json=sched) as r:
        s = await r.json()
        check(s["docker_update"]["exclude"] == ["broken"] and s["docker_update"]["enabled"], "docker: schedule saved")

    # A new zigbee2mqtt release appears; the scheduled window should update it and skip the excluded one.
    fake.publish(fake.make_image("koenkk/zigbee2mqtt:latest", "3.0"))
    broken_id = fake.by_name("broken")["Id"]
    portainer_id = fake.by_name("portainer")["Id"]
    pulls_before = sum(1 for c, _ in fake.calls if c == "pull")
    await agent.docker._tick()
    z = fake.by_name("zigbee2mqtt")
    check(dict(e.split("=", 1) for e in z["Config"]["Env"])["IMAGE_VERSION"] == "3.0", "docker: scheduled window updates containers")
    check(fake.by_name("broken")["Id"] == broken_id and fake.by_name("portainer")["Id"] == portainer_id,
          "docker: schedule skips excluded and opted-out containers")
    pulls_after = sum(1 for c, _ in fake.calls if c == "pull")
    await agent.docker._tick()
    check(sum(1 for c, _ in fake.calls if c == "pull") == pulls_after and pulls_after == pulls_before + 1,
          "docker: window runs once per day")
