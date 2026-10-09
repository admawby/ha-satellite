"""APT package updates: on-demand checks/upgrades plus a scheduled maintenance window."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from .schedule import due_window
from .settings import STATE

_LOGGER = logging.getLogger(__name__)
STATE_FILE = STATE / "updates.json"
REBOOT_FLAG = Path("/var/run/reboot-required")
APT_ENV = {
    **os.environ,
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "a",
}
DPKG_OPTS = ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]


class Updater:
    def __init__(self, get_settings: Callable[[], Dict[str, Any]], reboot: Callable[[], None]) -> None:
        self._get_settings = get_settings
        self._reboot = reboot
        self._lock = asyncio.Lock()
        self.state: Dict[str, Any] = {
            "available": None,
            "packages": [],
            "last_check": None,
            "last_upgrade": None,
            "last_result": None,
            "last_window": None,
            "running": None,
            "log": [],
        }
        if STATE_FILE.exists():
            try:
                self.state.update(json.loads(STATE_FILE.read_text()))
            except ValueError:
                pass
        self.state["running"] = None

    def _save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state))

    def status(self) -> Dict[str, Any]:
        data = {k: v for k, v in self.state.items() if k != "log"}
        data["reboot_required"] = REBOOT_FLAG.exists()
        data["log"] = self.state["log"][-200:]
        return data

    async def _run(self, *cmd: str, timeout: int = 1800) -> int:
        self.state["log"].append(f"$ {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=APT_ENV
        )

        async def pump() -> None:
            assert proc.stdout is not None
            async for line in proc.stdout:
                self.state["log"].append(line.decode(errors="replace").rstrip())
                del self.state["log"][:-400]

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            self.state["log"].append("!! timed out")
            return -1
        return proc.returncode or 0

    async def _list_upgradable(self) -> List[Dict[str, str]]:
        proc = await asyncio.create_subprocess_exec(
            "apt", "list", "--upgradable", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=APT_ENV,
        )
        out, _ = await proc.communicate()
        pkgs = []
        for line in out.decode(errors="replace").splitlines():
            if "/" not in line or "upgradable from" not in line:
                continue
            name = line.split("/", 1)[0]
            parts = line.split()
            new = parts[1] if len(parts) > 1 else ""
            old = line.rsplit("from:", 1)[-1].strip(" ]")
            pkgs.append({"name": name, "new": new, "old": old})
        return pkgs

    async def check(self) -> Dict[str, Any]:
        if self._lock.locked():
            return self.status()
        async with self._lock:
            self.state["running"] = "check"
            self.state["log"] = []
            rc = await self._run("apt-get", "update", "-q", timeout=600)
            pkgs = await self._list_upgradable()
            self.state.update(
                available=len(pkgs), packages=pkgs, last_check=time.time(), running=None,
                last_result="check ok" if rc == 0 else f"apt-get update failed ({rc})",
            )
            self._save()
        return self.status()

    async def apply(self) -> Dict[str, Any]:
        if self._lock.locked():
            raise RuntimeError("an update operation is already running")
        settings = self._get_settings()["auto_update"]
        async with self._lock:
            self.state["running"] = "upgrade"
            self.state["log"] = []
            rc = await self._run("apt-get", "update", "-q", timeout=600)
            verb = "full-upgrade" if settings.get("full_upgrade") else "upgrade"
            if rc == 0:
                rc = await self._run("apt-get", "-y", "-q", *DPKG_OPTS, verb)
            if rc == 0 and settings.get("autoremove"):
                await self._run("apt-get", "-y", "-q", "autoremove", "--purge")
            pkgs = await self._list_upgradable()
            self.state.update(
                available=len(pkgs), packages=pkgs, last_check=time.time(), last_upgrade=time.time(),
                running=None, last_result="upgrade ok" if rc == 0 else f"upgrade failed ({rc})",
            )
            self._save()
        return self.status()

    async def scheduler(self) -> None:
        """Run the maintenance window and refresh the update count every 6 hours."""
        await asyncio.sleep(60)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                _LOGGER.exception("Update scheduler error")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        cfg = self._get_settings()["auto_update"]
        today = due_window(cfg, self.state.get("last_window"))
        if today and not self._lock.locked():
            self.state["last_window"] = today
            _LOGGER.info("Starting scheduled package upgrade")
            await self.apply()
            if cfg.get("auto_reboot") and REBOOT_FLAG.exists():
                _LOGGER.info("Rebooting after upgrade (reboot-required present)")
                self._reboot()
            return
        last = self.state.get("last_check") or 0
        if time.time() - last > 6 * 3600 and not self._lock.locked():
            await self.check()
