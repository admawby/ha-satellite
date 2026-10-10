"""Removal ("leave no trace") scenarios, run by test_e2e.py. The cleanup script is
captured instead of executed, then checked for content and shell syntax."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import aiohttp

from hasat.agent_client import AgentError


def _sh_syntax_ok(script: str) -> bool:
    sh = shutil.which("bash") or shutil.which("sh")
    if not sh:
        return True
    return subprocess.run([sh, "-n"], input=script, text=True, capture_output=True).returncode == 0


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, sat, agent, state_dir: Path) -> None:
    proxy = f"{ui}/api/sat/{sat.id}/proxy"
    record = state_dir / "install-record.json"
    captured: list = []
    agent.launch_cleanup = captured.append

    # ---- host mode (Raspberry Pi)
    record.write_text(json.dumps({
        "mode": "host", "ser2net_conf_existed": False,
        "packages_added": ["ser2net", "python3-aiohttp", "python3-yarl"],
        "docker_installed": "official", "docker_packages": ["docker-ce", "pigz"],
    }))
    async with http.get(f"{proxy}/agent/uninstall-plan") as r:
        plan = await r.json()
    check(r.status == 200 and plan["mode"] == "host" and plan["docker_installed_by_hasat"] == "official",
          "removal: plan reachable through the UI")
    check(any("python3-yarl" in x for x in plan["remove"]) and any("Docker and your containers" in x for x in plan["keep"]),
          "removal: plan lists recorded packages, keeps Docker by default")
    async with http.get(f"{proxy}/agent/uninstall-plan", params={"remove_docker": "1"}) as r:
        plan = await r.json()
    check(any("ALL containers" in x for x in plan["remove"]), "removal: plan warns when Docker will be removed")

    report = await mgr.client.request(sat, "POST", "/api/agent/uninstall", json={"remove_docker": False})
    script = captured.pop()
    check(report["ok"] and "systemctl disable --now hasat-agent" in script and "rm -f /etc/systemd/system/hasat-agent.service" in script,
          "removal: service and unit file removed")
    check("nft delete table inet hasat" in script and "rm -f /etc/ser2net.yaml" in script and "hasat-orig" in script,
          "removal: firewall table removed, ser2net config restored/removed")
    check("apt-mark auto" in script and "python3-yarl" in script and "autoremove" in script and "grep -Fx" in script,
          "removal: only installer-added packages that nothing needs are purged")
    check("/opt/hasat-agent" in script and "/etc/hasat-agent" in script and "/var/lib/hasat-agent" in script
          and "/media/hasat" in script, "removal: code, certificates, state and mounts deleted")
    check("docker-ce" not in script and "/var/lib/docker" not in script, "removal: Docker untouched unless asked")
    check(_sh_syntax_ok(script), "removal: host cleanup script is valid shell")

    await mgr.client.request(sat, "POST", "/api/agent/uninstall", json={"remove_docker": True})
    script = captured.pop()
    check("apt-get purge -y -q" in script and "docker-ce" in script and "pigz" in script and "/var/lib/docker" in script
          and "groupdel docker" in script, "removal: Docker installed by HA Satellite removed on request")
    check(_sh_syntax_ok(script), "removal: host cleanup script with Docker is valid shell")

    record.write_text(json.dumps({"mode": "host", "ser2net_conf_existed": True}))
    await mgr.client.request(sat, "POST", "/api/agent/uninstall", json={"remove_docker": True})
    script = captured.pop()
    check("rm -f /etc/ser2net.yaml;" not in script and "docker-ce" not in script and "apt-mark" not in script,
          "removal: nothing recorded -> pre-existing ser2net config, packages and Docker kept")

    # ---- container mode (Synology)
    saved = {k: os.environ.get(k) for k in ("HASAT_MODE", "HASAT_CONTAINER_NAME", "HASAT_HOST_BASE")}
    os.environ.update(HASAT_MODE="container", HASAT_CONTAINER_NAME="hasat-agent", HASAT_HOST_BASE="/volume1/docker/hasat-agent")
    try:
        record.write_text(json.dumps({"mode": "container", "host_base": "/volume1/docker/hasat-agent",
                                      "images": ["hasat-agent:local", "python:3.12-slim"]}))
        async with http.get(f"{proxy}/agent/uninstall-plan") as r:
            plan = await r.json()
        check(plan["mode"] == "container" and any("/volume1/docker/hasat-agent" in x for x in plan["remove"])
              and any("Container Manager" in x for x in plan["keep"]), "removal (NAS): plan shown")
        report = await mgr.client.request(sat, "POST", "/api/agent/uninstall", json={})
        script = captured.pop()
        check("docker rm -f hasat-agent" in script and "hasat-agent:local" in script and "python:3.12-slim" in script
              and "rm -rf /volume1/docker/hasat-agent" in script, "removal (NAS): container, images and data folder removed")
        check("apt" not in script and "systemctl" not in script, "removal (NAS): DSM itself untouched")
        check(_sh_syntax_ok(script), "removal (NAS): cleanup script is valid shell")

        os.environ["HASAT_HOST_BASE"] = "/volume1"
        record.write_text(json.dumps({"mode": "container", "images": ["hasat-agent:local"]}))
        await mgr.client.request(sat, "POST", "/api/agent/uninstall", json={})
        script = captured.pop()
        async with http.get(f"{proxy}/agent/uninstall-plan") as r:
            plan = await r.json()
        check("rm -rf" not in script and "python:3.12-slim" not in script and any("delete it by hand" in x for x in plan["keep"]),
              "removal (NAS): never deletes a whole volume, keeps base image it did not download")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # A failing cleanup must not make the satellite disappear from HA.
    def fail(_script):
        raise RuntimeError("systemd-run not found")

    agent.launch_cleanup = fail
    try:
        await mgr.remove(sat, uninstall=True)
        kept = False
    except AgentError as err:
        kept = sat.id in mgr.store.satellites and "systemd-run" in str(err)
    check(kept, "removal: satellite kept in HA when on-device cleanup fails")
    record.unlink()
