"""Regression tests for the 0.9.0 Raspberry Pi hang: the V3D stats lookup walked /sys
recursively (symlink loops -> never finished), the status request hung, the satellite
looked offline, and offline satellites never received the fixed agent."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Callable


async def run(check: Callable[[bool, str], None], mgr, sat, agent, restarted: asyncio.Event) -> None:
    from hasat_agent import gpu, metrics

    # 1) V3D stats are found through the DRM card, without a recursive walk of /sys.
    root = tempfile.mkdtemp(prefix="hasat-drm-")
    card = os.path.join(root, "card0")
    os.makedirs(os.path.join(card, "device"))
    open(os.path.join(card, "device", "gpu_stats"), "w").close()
    real_glob = gpu.glob.glob

    def no_recursion(pattern, recursive=False):
        assert not recursive and "**" not in pattern, f"recursive sysfs search: {pattern}"
        return real_glob(pattern)

    gpu.glob.glob = no_recursion
    try:
        found = gpu._find_v3d_stats([(card, "v3d", "fec00000.v3d")])
        none = gpu._find_v3d_stats([(card, "i915", "0000:00:02.0")])
    finally:
        gpu.glob.glob = real_glob
    check(found == f"{card}/device/gpu_stats" and none is None, "hang: Pi GPU stats located via the DRM card, no /sys walk")

    # 2) A GPU probe that hangs cannot block the health report.
    real_collect, real_timeout = gpu.collect, metrics.GPU_TIMEOUT
    calls = []

    def stuck(*_a):
        calls.append(time.monotonic())
        time.sleep(2.0)
        return [{"key": "x"}]

    gpu.collect, metrics.GPU_TIMEOUT = stuck, 0.3
    metrics._GPU_STATE.update(future=None, last=[], warned=False)
    try:
        t0 = time.monotonic()
        first = metrics._gpus()
        second = metrics._gpus()  # probe still running: no new probe, last values returned
        took = time.monotonic() - t0
    finally:
        gpu.collect, metrics.GPU_TIMEOUT = real_collect, real_timeout
    check(first == [] and second == [] and took < 1.0 and len(calls) == 1,
          f"hang: stuck GPU probe skipped within the time limit ({took:.2f}s, {len(calls)} probe)")
    await asyncio.sleep(2.2)  # let the stuck probe finish before continuing
    metrics._GPU_STATE.update(future=None, last=[], warned=False)

    # 3) Agent answers but /api/status hangs -> the add-on reinstalls the agent by itself.
    real_status = agent.status
    agent.status = lambda: (time.sleep(3.5), real_status())[1]
    mgr.set_viewer("hang-test", True)  # live mode -> 8 s status timeout
    restarted.clear()
    sat.agent_update = None
    mgr._rescued.clear()
    mgr._updating.discard(sat.id)
    orig_request = mgr.client.request

    async def short_status(s, method, path, **kw):
        if path == "/api/status":
            kw["timeout"] = 1.5  # keep the test quick; production uses 8-20 s
        return await orig_request(s, method, path, **kw)

    mgr.client.request = short_status
    saved_auto = mgr.options.auto_update_agents
    mgr.options.auto_update_agents = True  # the rescue follows the "Auto-update agents" option
    try:
        await mgr.poll_one(sat.id)
        await asyncio.wait_for(restarted.wait(), 5)
        check(sat.status == "updating" and "rescue" not in sat.last_error,
              "hang: hung-but-reachable agent is reinstalled automatically (shows 'updating')")
        pushes = []
        real_push = mgr.push_agent_update

        async def counting_push(s):
            pushes.append(s.id)
            return await real_push(s)

        mgr.push_agent_update = counting_push
        sat.agent_update = None
        mgr._updating.discard(sat.id)
        await mgr.poll_one(sat.id)
        check(pushes == [], "hang: rescue is rate-limited (no reinstall loop)")
        mgr.push_agent_update = real_push
    finally:
        mgr.options.auto_update_agents = saved_auto
        agent.status = real_status
        mgr.client.request = orig_request
        mgr.set_viewer("hang-test", False)
        sat.agent_update = None
        mgr._updating.discard(sat.id)
        await asyncio.sleep(3.6)  # the slow status thread finishes
    await mgr.poll_one(sat.id)
    check(sat.online and sat.status == "online", "hang: satellite back online once status answers again")
