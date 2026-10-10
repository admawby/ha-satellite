"""Real host-mode install and trace-free removal (Linux CI with systemd + passwordless sudo).

1. snapshot the machine (dpkg packages, paths, systemd unit, nftables tables)
2. run the real install.sh through the manager's enrollment endpoint (curl | sudo bash)
3. poll the agent (systemd service) over mutual TLS
4. remove the satellite with "leave no trace" through the manager
5. wait for the cleanup unit and compare the machine with the snapshot

    python tests/test_host_install_real.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="hasat-host-"))
(TMP / "data").mkdir()
os.environ["HASAT_DATA_DIR"] = str(TMP / "data")
os.environ["HASAT_AGENT_DIR"] = str(ROOT / "ha_satellite/rootfs/opt/hasat/agent")
sys.path.insert(0, str(ROOT / "ha_satellite/rootfs/opt/hasat/server"))

from aiohttp import web  # noqa: E402

from hasat.config import load_options  # noqa: E402
from hasat.enroll import build_enroll_app  # noqa: E402
from hasat.manager import Manager  # noqa: E402

import terminal_cases  # noqa: E402

AGENT_PORT, ENROLL_PORT = 18766, 18767
TRACE_PATHS = ["/opt/hasat-agent", "/opt/hasat-agent.old", "/etc/hasat-agent", "/var/lib/hasat-agent",
               "/etc/systemd/system/hasat-agent.service", "/media/hasat", "/etc/ser2net.yaml",
               "/etc/ser2net.yaml.hasat-orig"]
PASSED: list = []


def run(*cmd: str, check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {proc.stderr.strip()}")
    return proc.stdout


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)
    PASSED.append(what)
    print(f"  ok  {what}")


def snapshot() -> dict:
    pkgs = run("dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\n")
    return {
        "packages": sorted(line.split()[1] for line in pkgs.splitlines() if line.startswith("ii")),
        "paths": sorted(p for p in TRACE_PATHS if os.path.lexists(p)),
        "nft": run("sudo", "nft", "list", "tables", check=False).strip() if shutil.which("nft") else "",
        "units": run("systemctl", "list-unit-files", "hasat*", "--no-legend", check=False).strip(),
    }


async def main() -> None:
    (TMP / "data" / "options.json").write_text(json.dumps({
        "enrollment_host": "127.0.0.1", "allowed_networks": ["127.0.0.0/8"], "extra_trusted_ips": [],
        "agent_port": AGENT_PORT, "poll_interval": 3600, "auto_update_agents": False,
        "publish_to_ha": False, "log_level": "info",
    }))
    mgr = Manager(load_options())
    await mgr.client.start()
    mgr.enroll_port = ENROLL_PORT
    runner = web.AppRunner(build_enroll_app(mgr))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", ENROLL_PORT, ssl_context=mgr.pki.enroll_ssl_context()).start()

    before = snapshot()
    print("before:", json.dumps({k: (len(v) if k == "packages" else v) for k, v in before.items()}))
    check(not before["paths"] and not before["units"], "machine starts clean")

    token = mgr.store.new_token("CI Pi").token
    command = mgr.install_command(token)
    print("running installer:", command.replace(token, "<token>"))
    proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    print(out.decode(errors="replace"))
    check(proc.returncode == 0, "install.sh completed (host mode)")

    sat = next(iter(mgr.store.satellites.values()))
    for _ in range(30):
        await mgr.poll_one(sat.id)
        if sat.online:
            break
        await asyncio.sleep(1)
    check(sat.online and sat.metrics["mode"] == "host" and "radios" in sat.metrics["features"], "agent service reachable over mTLS")
    check(sat.metrics["hostname"] == socket.gethostname(), "agent reports this machine")
    record = json.loads(run("sudo", "cat", "/var/lib/hasat-agent/install-record.json"))
    added = set(snapshot()["packages"]) - set(before["packages"])
    check(set(record["packages_added"]) == added and "ser2net" in added, f"install record lists the {len(added)} added packages")
    check("hasat" in run("sudo", "nft", "list", "tables"), "firewall table installed")
    check(os.path.exists("/etc/ser2net.yaml"), "ser2net config written")

    await terminal_cases.run_with(check, terminal_cases.agent_connector(mgr.client, sat), "installed-agent terminal")

    plan = await mgr.client.request(sat, "GET", "/api/agent/uninstall-plan")
    check(any("ser2net" in x for x in plan["remove"]), "removal plan lists added packages")
    report = await mgr.remove(sat, uninstall=True)
    check(report["uninstalled"] and sat.id not in mgr.store.satellites, "satellite removed from manager")

    print("waiting for the on-device cleanup ...")
    for _ in range(240):
        state = run("systemctl", "is-active", "hasat-uninstall", check=False).strip()
        if state not in ("active", "activating"):
            break
        await asyncio.sleep(1)
    print(run("sudo", "journalctl", "-u", "hasat-uninstall", "--no-pager", "-n", "60", check=False))

    after = snapshot()
    missing = sorted(set(before["packages"]) - set(after["packages"]))
    extra = sorted(set(after["packages"]) - set(before["packages"]))
    print("packages still added:", extra, "| packages lost:", missing)
    check(not extra, "every package the installer added was removed")
    check(not missing, "no package that was there before was removed")
    check(after["paths"] == before["paths"], f"no files left behind ({after['paths']})")
    check(after["units"] == before["units"], "no systemd units left behind")
    check(after["nft"] == before["nft"], "firewall back to its original state")
    try:
        await mgr.client.request(sat, "GET", "/api/status", timeout=5)
        gone = False
    except Exception:  # noqa: BLE001
        gone = True
    check(gone, "agent no longer listening")
    await mgr.client.close()
    await runner.cleanup()


if __name__ == "__main__":
    ok = (sys.platform.startswith("linux") and shutil.which("apt-get") and shutil.which("systemctl")
          and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0)
    if not ok:
        print("SKIPPED: needs Debian/Ubuntu with systemd and passwordless sudo")
        sys.exit(0)
    try:
        asyncio.run(main())
        print(f"\nALL {len(PASSED)} HOST INSTALL/REMOVAL CHECKS PASSED")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
