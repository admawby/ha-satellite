"""Container-mode agent (Synology DSM path) against a REAL Docker daemon.

Builds the agent image from agent/container/Dockerfile, starts it with the same
run.sh the installer uses, then talks to it over mutual TLS like the add-on does.
Runs in CI on Linux (skipped where Docker is unavailable).

    python tests/test_container_agent.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = ROOT / "ha_satellite/rootfs/opt/hasat/agent"
TMP = Path(tempfile.mkdtemp(prefix="hasat-ct-"))
os.environ["HASAT_DATA_DIR"] = str(TMP / "data")
os.environ["HASAT_AGENT_DIR"] = str(AGENT_SRC)
sys.path.insert(0, str(ROOT / "ha_satellite/rootfs/opt/hasat/server"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from hasat.agent_client import AgentClient, AgentError, build_agent_bundle  # noqa: E402
from hasat.pki import PKI  # noqa: E402
from hasat.store import Satellite  # noqa: E402

import terminal_cases  # noqa: E402

PORT = 18765
NAME = "hasat-agent"
SAT_ID = "sat-c0ffee01"
BASE_IMAGE = os.environ.get("HASAT_TEST_BASE_IMAGE", "mirror.gcr.io/library/python:3.12-slim")
PASSED: list = []


def sh(*args: str, check: bool = True, **kw) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, **kw)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)
    PASSED.append(what)
    print(f"  ok  {what}")


def started_at() -> str:
    return sh("docker", "inspect", "-f", "{{.State.StartedAt}}", NAME, check=False)


async def wait_up(client: AgentClient, sat: Satellite, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return await client.request(sat, "GET", "/api/status", timeout=5)
        except AgentError:
            await asyncio.sleep(1)
    print(sh("docker", "logs", "--tail", "50", NAME, check=False))
    raise AssertionError("agent container did not come up")


async def main() -> None:
    base, vol = TMP / "base", TMP / "volume"
    vol.mkdir(parents=True)
    (base / "hasat-agent").mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(build_agent_bundle(AGENT_SRC))) as tar:
        tar.extractall(base / "hasat-agent")

    pki = PKI(TMP / "data" / "pki")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hasat-pending")]))
           .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    etc = base / "etc"
    etc.mkdir()
    (etc / "agent.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
    (etc / "agent.crt").write_bytes(pki.sign_agent_csr(csr, SAT_ID, "127.0.0.1"))
    (etc / "ca.crt").write_bytes(pki.ca_pem)
    (etc / "config.json").write_text(json.dumps({"id": SAT_ID, "name": "NAS", "controller_cn": "hasat-controller",
                                                 "trusted_ips": ["127.0.0.1"], "agent_port": PORT}))

    # Like the installer: the image was "downloaded by the installer", so removal must delete it.
    sh("docker", "pull", "-q", BASE_IMAGE)
    (base / "state").mkdir()
    (base / "state" / "install-record.json").write_text(json.dumps(
        {"mode": "container", "host_base": str(base), "images": [BASE_IMAGE]}))
    sh("sh", str(base / "hasat-agent" / "container" / "run.sh"), str(base), str(vol), BASE_IMAGE)

    client = AgentClient(pki.client_ssl_context())
    await client.start()
    sat = Satellite(id=SAT_ID, name="NAS", host="127.0.0.1", port=PORT)
    st = await wait_up(client, sat, timeout=180)  # first start installs aiohttp/psutil
    check((base / "pydeps").is_dir() and any((base / "pydeps").glob(".installed-*")),
          "Python libraries installed into the agent data folder (nothing built)")

    check(st["mode"] == "container" and st["features"] == ["health", "terminal", "docker"], "agent runs in container mode")
    check(st["hostname"] == socket.gethostname(), "host hostname reported (uts=host)")
    check(st["disk_total"] == shutil.disk_usage(vol).total, "disk usage of the data volume reported")
    check(st["cpu_percent"] is not None and st["mem_total"] > 0 and st["uptime"] > 0, "host health metrics")
    if Path("/etc.defaults/VERSION").exists():
        check(st["os"].startswith("DSM 7.2.1"), f"DSM version read from /etc.defaults/VERSION ({st['os']})")
    check(st["docker"]["available"] and st["docker"]["total"] >= 1, "Docker summary via the host socket")

    for path in ("/api/usb", "/api/files/list?path=/", "/api/updates", "/api/logs"):
        try:
            await client.request(sat, "GET", path)
            ok = False
        except AgentError as err:
            ok = "403" in str(err) and "not available" in str(err)
        check(ok, f"disabled endpoint refused: {path}")
    try:
        await client.request(sat, "POST", "/api/system/reboot")
        ok = False
    except AgentError as err:
        ok = "403" in str(err)
    check(ok, "reboot refused in container mode")

    host_id = sh("sh", "-c", ". /etc/os-release && echo $ID")
    out = await client.request(sat, "POST", "/api/exec", json={"command": ". /etc/os-release; echo OS=$ID; cat /proc/1/comm"})
    check(out["rc"] == 0 and f"OS={host_id}" in out["output"], f"command runner executes on the host ({out['output'].split()[0]})")

    ws = await client.ws_connect(sat, "/api/terminal")
    await ws.send_str(json.dumps({"t": "r", "c": 120, "r": 30}))
    await ws.send_str(json.dumps({"t": "i", "d": ". /etc/os-release; echo TERM-OS=$ID-$((6*7))\n"}))
    seen = ""

    async def read() -> None:
        nonlocal seen
        async for msg in ws:
            seen += msg.data.decode(errors="ignore") if isinstance(msg.data, bytes) else str(msg.data)
            if f"TERM-OS={host_id}-42" in seen:
                return

    await asyncio.wait_for(read(), 20)
    await ws.close()
    check(f"TERM-OS={host_id}-42" in seen, "interactive terminal is a shell on the host")

    await terminal_cases.run_with(check, terminal_cases.agent_connector(client, sat), "container terminal")

    d = await client.request(sat, "GET", "/api/docker")
    me = {c["name"]: c for c in d["containers"]}.get(NAME)
    check(me is not None and me["auto_update"] is False, "agent container listed and excluded from updates")
    try:
        await client.request(sat, "POST", f"/api/docker/containers/{NAME}/stop")
        ok = False
    except AgentError as err:
        ok = "own container" in str(err)
    check(ok and sh("docker", "inspect", "-f", "{{.State.Running}}", NAME) == "true", "agent refuses to stop itself")

    before = started_at()
    res = await client.request(sat, "POST", "/api/agent/update", data=build_agent_bundle(AGENT_SRC),
                               params={"version": "ci"}, timeout=60)
    check(res.get("restarting") is True, "agent update accepted")
    await asyncio.sleep(3)
    st2 = await wait_up(client, sat)
    check(started_at() != before and st2["mode"] == "container", "container restarted on updated code (restart policy)")
    check((base / "hasat-agent.old" / "hasat_agent").is_dir() and (base / "hasat-agent" / "hasat_agent").is_dir(),
          "update swapped code inside the bind mount")

    await client.request(sat, "POST", "/api/agent/uninstall")
    for _ in range(30):
        if not sh("docker", "ps", "-aq", "--filter", f"name=^{NAME}$", check=False):
            break
        await asyncio.sleep(1)
    check(not sh("docker", "ps", "-aq", "--filter", f"name=^{NAME}$", check=False), "uninstall removed the agent container")
    for _ in range(30):
        if not base.exists():
            break
        await asyncio.sleep(1)
    check(not base.exists(), "uninstall removed the whole data folder")
    for _ in range(30):
        if not sh("docker", "images", "-q", BASE_IMAGE, check=False):
            break
        await asyncio.sleep(1)
    check(not sh("docker", "images", "-q", BASE_IMAGE, check=False), "uninstall removed the image the installer downloaded")
    await client.close()


if __name__ == "__main__":
    if os.name != "posix" or not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIPPED: needs Linux with a Docker daemon")
        sys.exit(0)
    try:
        asyncio.run(main())
        print(f"\nALL {len(PASSED)} CONTAINER-MODE CHECKS PASSED")
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        subprocess.run(["sudo", "-n", "rm", "-rf", str(TMP)], capture_output=True)
        shutil.rmtree(TMP, ignore_errors=True)
