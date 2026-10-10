"""Central state: satellites, polling, HA publishing and agent updates."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time

import aiohttp

from .launch import Launcher
from .agent_client import AgentClient, AgentError, build_agent_bundle
from .config import AGENT_SRC_DIR, DATA_DIR, ENROLL_PORT, SUPERVISOR_TOKEN, SUPERVISOR_URL, Options
from .ha_bridge import HABridge
from .pki import PKI
from .store import Satellite, Store

_LOGGER = logging.getLogger(__name__)


def _agent_version() -> str:
    text = (AGENT_SRC_DIR / "hasat_agent" / "__init__.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0"


class Manager:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.pki = PKI(DATA_DIR / "pki")
        self.store = Store(DATA_DIR / "satellites.json")
        self.client = AgentClient(self.pki.client_ssl_context())
        self.bridge = HABridge(self.handle_ha_command)
        self.launcher = Launcher(self)
        self.agent_bundle = build_agent_bundle(AGENT_SRC_DIR)
        self.agent_version = _agent_version()
        self.install_template = (AGENT_SRC_DIR / "install.sh").read_text()
        self.enroll_host = options.enrollment_host
        self.enroll_port = ENROLL_PORT
        self._poll_task: asyncio.Task | None = None
        self._updating: set[str] = set()

    # -------------------------------------------------------------- startup
    async def start(self) -> None:
        await self.client.start()
        await self._discover_network()
        if self.options.publish_to_ha:
            await self.bridge.start()
        self._poll_task = asyncio.create_task(self._poll_loop())
        _LOGGER.info(
            "Manager ready: %d satellite(s), enrollment at https://%s:%s, CA %s",
            len(self.store.satellites), self.enroll_host, self.enroll_port, self.pki.ca_fingerprint,
        )

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await self.bridge.stop()
        await self.client.close()

    async def _discover_network(self) -> None:
        """Work out the LAN address/port satellites should use to reach this add-on."""
        if not SUPERVISOR_TOKEN:
            self.enroll_host = self.enroll_host or socket.gethostbyname(socket.gethostname())
            return
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as http:
            try:
                async with http.get(f"{SUPERVISOR_URL}/addons/self/info") as r:
                    net = ((await r.json()).get("data") or {}).get("network") or {}
                    mapped = net.get(f"{ENROLL_PORT}/tcp")
                    if mapped:
                        self.enroll_port = int(mapped)
            except (aiohttp.ClientError, ValueError) as err:
                _LOGGER.warning("Could not read add-on network mapping: %s", err)
            if self.enroll_host:
                return
            try:
                async with http.get(f"{SUPERVISOR_URL}/network/info") as r:
                    data = (await r.json()).get("data") or {}
                for iface in data.get("interfaces", []):
                    if iface.get("primary") and iface.get("ipv4", {}).get("address"):
                        self.enroll_host = iface["ipv4"]["address"][0].split("/")[0]
                        break
            except (aiohttp.ClientError, ValueError, IndexError) as err:
                _LOGGER.warning("Could not detect host IP, set enrollment_host: %s", err)
        if not self.enroll_host:
            _LOGGER.error("Host IP unknown — set the 'enrollment_host' option so satellites can enroll")

    # ----------------------------------------------------------- enrollment
    def trusted_ips(self) -> list[str]:
        """Source addresses a satellite's firewall should accept for agent/serial ports."""
        ips: list[str] = []
        for entry in [self.enroll_host, *self.options.extra_trusted_ips]:
            if not entry:
                continue
            try:
                for info in socket.getaddrinfo(entry, None):
                    addr = info[4][0]
                    if addr not in ips:
                        ips.append(addr)
            except OSError:
                _LOGGER.warning("Could not resolve trusted host %s", entry)
        return ips

    def install_command(self, token: str, sudo: bool = True) -> str:
        url = f"https://{self.enroll_host}:{self.enroll_port}/enroll/{token}/install.sh"
        pin = f"sha256//{self.pki.enroll_pin}"
        fetch = f"curl -fsSk --pinnedpubkey '{pin}' '{url}'"
        return f"{fetch} | sudo bash" if sudo else f"{fetch} | bash"

    def render_install_script(self, token: str) -> str:
        replacements = {
            "@@CONTROLLER_HOST@@": self.enroll_host,
            "@@CONTROLLER_PORT@@": str(self.enroll_port),
            "@@TOKEN@@": token,
            "@@PIN@@": self.pki.enroll_pin,
            "@@AGENT_PORT@@": str(self.options.agent_port),
        }
        script = self.install_template
        for key, val in replacements.items():
            script = script.replace(key, val)
        return script

    def on_enrolled(self, sat: Satellite, token: str) -> None:
        self.launcher.enrolled(token, sat.id)
        # Give the agent a moment to start, then poll it.
        asyncio.get_running_loop().call_later(5, lambda: asyncio.create_task(self.poll_one(sat.id)))

    # -------------------------------------------------------------- polling
    async def _poll_loop(self) -> None:
        while True:
            sats = list(self.store.satellites.values())
            if sats:
                await asyncio.gather(*(self._poll(s) for s in sats), return_exceptions=True)
            await asyncio.sleep(self.options.poll_interval)

    async def poll_one(self, sat_id: str) -> None:
        sat = self.store.satellites.get(sat_id)
        if sat:
            await self._poll(sat)

    async def _poll(self, sat: Satellite) -> None:
        try:
            sat.metrics = await self.client.request(sat, "GET", "/api/status", timeout=20)
            if not sat.online:
                _LOGGER.info("Satellite %s is online", sat.name)
            sat.online, sat.last_seen, sat.last_error = True, time.time(), ""
            hostname = sat.metrics.get("hostname")
            if hostname and hostname != sat.hostname:
                sat.hostname = hostname
                self.store.save()
        except AgentError as err:
            if sat.online:
                _LOGGER.warning("Satellite %s went offline: %s", sat.name, err)
            sat.online, sat.last_error = False, str(err)
        await self.bridge.publish(sat)
        if sat.online and self.options.auto_update_agents:
            current = sat.metrics.get("agent_version")
            if current and current != self.agent_version and sat.id not in self._updating:
                asyncio.create_task(self.push_agent_update(sat))

    # ------------------------------------------------------------- actions
    async def push_agent_update(self, sat: Satellite) -> dict:
        self._updating.add(sat.id)
        try:
            _LOGGER.info("Updating agent on %s to %s", sat.name, self.agent_version)
            return await self.client.request(
                sat, "POST", "/api/agent/update", data=self.agent_bundle,
                params={"version": self.agent_version}, timeout=60,
            )
        except AgentError as err:
            _LOGGER.warning("Agent update on %s failed: %s", sat.name, err)
            raise
        finally:
            # Leave time for the agent to restart before trying again.
            asyncio.get_running_loop().call_later(300, self._updating.discard, sat.id)

    async def handle_ha_command(self, sat_id: str, action: str) -> None:
        sat = self.store.satellites.get(sat_id)
        if not sat:
            return
        path = {
            "reboot": "/api/system/reboot",
            "check_updates": "/api/updates/check",
            "apply_updates": "/api/updates/apply",
            "update_containers": "/api/docker/update",
        }.get(action)
        if not path:
            return
        _LOGGER.info("Home Assistant requested %s on %s", action, sat.name)
        try:
            await self.client.request(sat, "POST", path, timeout=30)
        except AgentError as err:
            _LOGGER.warning("%s on %s failed: %s", action, sat.name, err)

    async def remove(self, sat: Satellite, uninstall: bool, remove_docker: bool = False) -> dict:
        """Forget a satellite. With uninstall, the device first removes every trace of
        HA Satellite; if that request fails the satellite is kept so it can be retried."""
        report: dict = {"uninstalled": False}
        if uninstall:
            report = await self.client.request(
                sat, "POST", "/api/agent/uninstall", json={"remove_docker": remove_docker}, timeout=60)
            report["uninstalled"] = True
            _LOGGER.info("Satellite %s is removing HA Satellite from the device", sat.name)
        await self.bridge.remove(sat)
        self.store.satellites.pop(sat.id, None)
        self.store.save()
        return report
