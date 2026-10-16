"""Live polling (presence) and bulk-update scenarios, run by test_e2e.py."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

import aiohttp

from hasat import bulk as bulk_mod


async def run_live(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, sat) -> None:
    async with http.post(f"{ui}/api/presence", json={"client": "tab-1", "active": True}) as r:
        d = await r.json()
    check(d["live"] and d["poll_interval"] == 3, "live: an active tab switches polling to 3 s")
    async with http.get(f"{ui}/api/state") as r:
        st = await r.json()
    check(st["live"] and st["poll_interval"] == 3, "live: state reports live mode")

    # sendBeacon posts text/plain when the tab closes
    async with http.post(f"{ui}/api/presence", data=json.dumps({"client": "tab-1", "active": False}),
                         headers={"Content-Type": "text/plain;charset=UTF-8"}) as r:
        d = await r.json()
    check(not d["live"] and d["poll_interval"] == mgr.options.poll_interval,
          "live: closing/hiding the tab returns to the normal interval")

    mgr.set_viewer("tab-2", True)
    mgr._viewers["tab-2"] = time.monotonic() - 1  # browser vanished without saying goodbye
    check(not mgr.live, "live: lease expires when heartbeats stop")
    mgr.set_viewer("a", True)
    mgr.set_viewer("b", True)
    mgr.set_viewer("a", False)
    check(mgr.live, "live: stays on while any tab is active")
    mgr.set_viewer("b", False)

    # The real poll loop: fast while live, normal interval otherwise; HA publishing stays slow.
    polls, published = [], []
    orig_poll, orig_publish = mgr._poll, mgr.bridge.publish

    async def counting_poll(s):
        polls.append(time.monotonic())
        await orig_poll(s)

    async def counting_publish(s):
        published.append(time.monotonic())

    mgr._poll, mgr.bridge.publish = counting_poll, counting_publish
    mgr._published.clear()
    loop = asyncio.create_task(mgr._poll_loop())
    try:
        await asyncio.sleep(0.5)
        polls.clear()
        mgr.set_viewer("tab-3", True)
        await asyncio.sleep(7)
        live_polls = len(polls)
        check(live_polls >= 2, f"live: device polled every ~3 s while active ({live_polls} polls in 7 s)")
        check(len(published) <= 1, f"live: Home Assistant still updated at the normal pace ({len(published)} publish)")
        mgr.set_viewer("tab-3", False)
        await asyncio.sleep(0.2)
        polls.clear()
        await asyncio.sleep(4)
        check(len(polls) == 0, "live: back to the normal interval when inactive")
    finally:
        loop.cancel()
        mgr._poll, mgr.bridge.publish = orig_poll, orig_publish


async def run_bulk(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, sat, agent, fake) -> None:
    bulk_mod.FOLLOW_EVERY = 0.2
    order: list = []
    real_run, real_list = agent.updater._run, agent.updater._list_upgradable

    async def fake_apt(*cmd, timeout=1800):
        order.append(("apt", cmd[-1], time.monotonic()))
        agent.updater.state["log"].append("$ " + " ".join(cmd))
        await asyncio.sleep(0.8)
        return 0

    async def fake_list():
        return []

    agent.updater._run, agent.updater._list_upgradable = fake_apt, fake_list
    orig_update = agent.docker.update

    async def tracking_update(names=None):
        order.append(("docker", "update", time.monotonic()))
        return await orig_update(names)

    agent.docker.update = tracking_update
    fake.publish(fake.make_image("koenkk/zigbee2mqtt:latest", "9.0"))  # a new image to pick up
    ghost = type(sat)(id="sat-0ff11e01", name="Offline Pi", host="127.0.0.1", port=1)
    mgr.store.satellites[ghost.id] = ghost
    try:
        async with http.post(f"{ui}/api/bulk-update", json={"scope": "bogus"}) as r:
            check(r.status == 400, "bulk: unknown scope rejected")
        async with http.post(f"{ui}/api/bulk-update", json={"scope": "all"}) as r:
            job = await r.json()
            check(r.status == 200 and job["running"] and len(job["satellites"]) == 2, "bulk: Update All started on every satellite")
        async with http.post(f"{ui}/api/bulk-update", json={"scope": "packages", "satellites": [sat.id]}) as r:
            check(r.status == 409, "bulk: second update on a busy satellite refused")
        for _ in range(150):
            async with http.get(f"{ui}/api/bulk-update") as r:
                job = await r.json()
            if not job["running"]:
                break
            await asyncio.sleep(0.2)
        runs = {r["sat_id"]: r for r in job["satellites"]}
        mine = {s["kind"]: s for s in runs[sat.id]["steps"]}
        check(not job["running"] and mine["packages"]["state"] == "done" and mine["docker"]["state"] == "done",
              f"bulk: packages and Docker images updated ({mine['packages']['detail']} / {mine['docker']['detail']})")
        apt_end = max(t for kind, _c, t in order if kind == "apt")
        docker_start = min(t for kind, _c, t in order if kind == "docker")
        check(docker_start >= apt_end, "bulk: packages finish before Docker images start on a satellite")
        env = dict(e.split("=", 1) for e in fake.by_name("zigbee2mqtt")["Config"]["Env"])
        check(env.get("IMAGE_VERSION") == "9.0", "bulk: container actually recreated on the new image")
        check(all(s["state"] == "skipped" and "offline" in s["detail"] for s in runs[ghost.id]["steps"]),
              "bulk: offline satellites are skipped, not failed")
        async with http.get(f"{ui}/api/state") as r:
            check((await r.json())["bulk"]["id"] == job["id"], "bulk: progress available to the All view")

        order.clear()
        async with http.post(f"{ui}/api/bulk-update", json={"scope": "docker", "satellites": [sat.id]}) as r:
            job = await r.json()
        for _ in range(150):
            async with http.get(f"{ui}/api/bulk-update") as r:
                job = await r.json()
            if not job["running"]:
                break
            await asyncio.sleep(0.2)
        check(len(job["satellites"]) == 1 and [s["kind"] for s in job["satellites"][0]["steps"]] == ["docker"]
              and not any(k == "apt" for k, _c, _t in order), "bulk: per-tile Docker update touches only that satellite's images")
        async with http.post(f"{ui}/api/bulk-update/dismiss", json={"id": job["id"]}) as r:
            check(r.status == 200, "bulk: finished progress panel can be dismissed")
        async with http.get(f"{ui}/api/state") as r:
            check((await r.json())["bulk"]["dismissed"] is True, "bulk: dismissal is remembered by the add-on (survives reloads)")
        async with http.post(f"{ui}/api/bulk-update/dismiss", json={"id": "nope"}) as r:
            check(r.status == 404, "bulk: dismissing an unknown job is rejected")
    finally:
        agent.updater._run, agent.updater._list_upgradable = real_run, real_list
        agent.docker.update = orig_update
        del mgr.store.satellites[ghost.id]

    # Without apt the package job must finish with an error instead of staying "running".
    res = await agent.updater.apply()
    check(res["running"] is None and "fail" in (res["last_result"] or ""), "updates: a missing apt reports failure, never hangs")
