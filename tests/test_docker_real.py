"""Integration test against a REAL Docker daemon (runs in CI on Linux; skipped elsewhere).

Publishes two versions of an image to a throw-away local registry, runs a container
with volumes, ports, env, labels, restart policy and two networks, then lets the
agent's DockerManager detect the new image via the registry and recreate the
container. Verifies the configuration survived and that opted-out containers are
left alone.

Run:  python tests/test_docker_real.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="hasat-docker-"))
os.environ["HASAT_STATE"] = str(TMP / "state")
os.environ["HASAT_ETC"] = str(TMP / "etc")
os.environ["HASAT_DOCKER_CONFIG"] = str(TMP / "none.json")
sys.path.insert(0, str(ROOT / "ha_satellite/rootfs/opt/hasat/agent"))

from hasat_agent.docker_mgr import DockerManager  # noqa: E402
from hasat_agent.settings import DEFAULT_SETTINGS  # noqa: E402

REG = "localhost:5055"
IMAGE = f"{REG}/hasat-test:latest"
PREFIX = "hasat-it"
PASSED: list = []


def sh(*args: str, check: bool = True, input: str | None = None) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, input=input)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)
    PASSED.append(what)
    print(f"  ok  {what}")


def inspect(name: str) -> dict:
    return json.loads(sh("docker", "inspect", name))[0]


def build(version: str) -> str:
    ctx = TMP / f"img{version}"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "Dockerfile").write_text(
        "FROM mirror.gcr.io/library/busybox:1.36\n"
        f"ENV IMAGE_VERSION={version}\n"
        f'LABEL org.opencontainers.image.version="{version}"\n'
        'CMD ["sh", "-c", "echo started $IMAGE_VERSION; sleep 3600"]\n'
    )
    sh("docker", "build", "-q", "-t", f"hasat-build:{version}", str(ctx))
    return sh("docker", "image", "inspect", "-f", "{{.Id}}", f"hasat-build:{version}")


def cleanup() -> None:
    for c in sh("docker", "ps", "-aq", "--filter", f"name={PREFIX}", check=False).split():
        sh("docker", "rm", "-f", c, check=False)
    sh("docker", "rm", "-f", f"{PREFIX}-registry", check=False)
    sh("docker", "network", "rm", f"{PREFIX}-net", check=False)
    sh("docker", "volume", "rm", f"{PREFIX}-vol", check=False)
    sh("docker", "rmi", "-f", IMAGE, "hasat-build:1", "hasat-build:2", check=False)


async def main() -> None:
    cleanup()
    sh("docker", "run", "-d", "--name", f"{PREFIX}-registry", "-p", "5055:5000", "mirror.gcr.io/library/registry:2")
    for _ in range(30):
        if subprocess.run(["curl", "-fs", f"http://{REG}/v2/"], capture_output=True).returncode == 0:
            break
        await asyncio.sleep(1)

    v1 = build("1")
    v2 = build("2")
    sh("docker", "tag", "hasat-build:1", IMAGE)
    sh("docker", "push", "-q", IMAGE)
    sh("docker", "network", "create", f"{PREFIX}-net")
    sh("docker", "run", "-d", "--name", f"{PREFIX}-app",
       "--restart", "unless-stopped",
       "-e", "USER_SETTING=keep-me",
       "-v", f"{PREFIX}-vol:/data",
       "-p", "18080:8080",
       "--label", "com.docker.compose.project=demo",
       "--network", "bridge",
       IMAGE)
    sh("docker", "network", "connect", "--alias", "app-alias", f"{PREFIX}-net", f"{PREFIX}-app")
    sh("docker", "run", "-d", "--name", f"{PREFIX}-optout", "--label", "hasat.update=false", IMAGE)

    # Publish v2 to the registry, but keep v1 as the local tag (as on a Pi that has not pulled yet).
    sh("docker", "tag", "hasat-build:2", IMAGE)
    sh("docker", "push", "-q", IMAGE)
    sh("docker", "tag", "hasat-build:1", IMAGE)

    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    mgr = DockerManager(lambda: settings)
    check(mgr.api.available(), "real docker socket reachable")

    items = {c["name"]: c for c in await mgr.containers()}
    check(f"{PREFIX}-app" in items and items[f"{PREFIX}-app"]["project"] == "demo", "containers listed from real daemon")

    await mgr.check()
    items = {c["name"]: c for c in await mgr.containers()}
    check(items[f"{PREFIX}-app"]["update_available"], f"registry digest check detects new image ({mgr.state['log'][-3:]})")
    check(items[f"{PREFIX}-optout"]["auto_update"] is False, "opt-out label honoured")

    before = inspect(f"{PREFIX}-app")
    await mgr.update()  # "update all" path: check + update eligible containers
    after = inspect(f"{PREFIX}-app")
    env = dict(e.split("=", 1) for e in after["Config"]["Env"])
    print("   log:", *mgr.state["log"], sep="\n     ")
    check(after["Id"] != before["Id"] and after["Image"] == v2 and after["State"]["Running"], "container recreated on new image and running")
    check(env["USER_SETTING"] == "keep-me" and env["IMAGE_VERSION"] == "2", "user env kept, image env refreshed")
    check(after["Config"]["Labels"]["com.docker.compose.project"] == "demo"
          and after["Config"]["Labels"]["org.opencontainers.image.version"] == "2", "compose label kept, image label refreshed")
    check(after["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", "restart policy kept")
    check(after["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostPort"] == "18080", "port binding kept")
    check(any(m.get("Name") == f"{PREFIX}-vol" and m["Destination"] == "/data" for m in after["Mounts"]), "named volume kept")
    nets = after["NetworkSettings"]["Networks"]
    check(set(nets) == {"bridge", f"{PREFIX}-net"} and "app-alias" in (nets[f"{PREFIX}-net"].get("Aliases") or []),
          "both networks and alias restored")
    check("started 2" in sh("docker", "logs", f"{PREFIX}-app"), "new container actually runs v2")
    check(inspect(f"{PREFIX}-optout")["Image"] == v1, "opted-out container untouched")
    check(not sh("docker", "ps", "-aq", "--filter", f"name={PREFIX}-app-hasat-old"), "no leftover backup container")

    await mgr.check()
    items = {c["name"]: c for c in await mgr.containers()}
    check(not items[f"{PREFIX}-app"]["update_available"], "up to date after update")
    logs = await mgr.logs(f"{PREFIX}-app", 20)
    check("started 2" in logs, "container logs via Engine API")
    await mgr.api.close()


if __name__ == "__main__":
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIPPED: no Docker daemon available")
        sys.exit(0)
    try:
        asyncio.run(main())
        print(f"\nALL {len(PASSED)} REAL-DOCKER CHECKS PASSED")
    finally:
        cleanup()
        shutil.rmtree(TMP, ignore_errors=True)
