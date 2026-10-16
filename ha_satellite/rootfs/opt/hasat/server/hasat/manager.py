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
from .bulk import BulkUpdates
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
        self.bridge.latest_agent_version = self.agent_version
        self.install_template = (AGENT_SRC_DIR / "install.sh").read_text()
        self.enroll_host = options.enrollment_host
        self.enroll_port = ENROLL_PORT
        self._poll_task: asyncio.Task | None = None
        self._updating: set[str] = set()
        # Live mode: browser tabs that are actively viewing the UI hold a short lease.
        self._viewers: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._inflight: set[str] = set()
        self._published: dict[str, tuple[float, str]] = {}
        self.bulk = BulkUpdates(self)

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
    LIVE_INTERVAL = 3.0   # seconds between polls while someone is actively using the UI
    VIEWER_LEASE = 15.0   # a browser heartbeat keeps live mode on for this long

    def set_viewer(self, client_id: str, active: bool) -> None:
        """Called by the UI: active tabs renew a lease; hidden/idle/closed tabs drop it."""
        client_id = client_id[:64] or "anonymous"
        was_live = self.live
        if active:
            self._viewers[client_id] = time.monotonic() + self.VIEWER_LEASE
        else:
            self._viewers.pop(client_id, None)
        if self.live and not was_live:
            _LOGGER.debug("Live polling on (%d active viewer(s))", len(self._viewers))
            self._wake.set()  # switch to fast polling immediately

    @property
    def live(self) -> bool:
        now = time.monotonic()
        for cid in [c for c, exp in self._viewers.items() if exp < now]:
            del self._viewers[cid]
        return bool(self._viewers)

    def current_interval(self) -> float:
        return min(self.LIVE_INTERVAL, self.options.poll_interval) if self.live else float(self.options.poll_interval)

    async def _poll_loop(self) -> None:
        last_poll = float("-inf")
        while True:
            # The next poll is due one interval after the last one; the interval is
            # re-evaluated on every wake-up, so live mode starts and stops immediately.
            if time.monotonic() - last_poll >= self.current_interval():
                last_poll = time.monotonic()
                # Satellites are polled independently: a slow or unreachable one is skipped
                # while its previous poll is still running instead of delaying the others.
                for sat in list(self.store.satellites.values()):
                    if sat.id not in self._inflight:
                        self._inflight.add(sat.id)
                        asyncio.create_task(self._poll_tracked(sat))
            self._wake.clear()
            remaining = max(0.05, last_poll + self.current_interval() - time.monotonic())
            try:
                await asyncio.wait_for(self._wake.wait(), min(remaining, self.LIVE_INTERVAL))
            except asyncio.TimeoutError:
                pass

    async def _poll_tracked(self, sat: Satellite) -> None:
        try:
            await self._poll(sat)
        except Exception:  # noqa: BLE001 - keep polling other satellites
            _LOGGER.exception("Polling %s failed", sat.name)
        finally:
            self._inflight.discard(sat.id)

    async def poll_one(self, sat_id: str) -> None:
        sat = self.store.satellites.get(sat_id)
        if sat:
            await self._poll(sat)

    AGENT_UPDATE_GRACE = 180.0  # seconds a device may be unreachable while its agent restarts

    async def _poll(self, sat: Satellite) -> None:
        try:
            timeout = 8 if self.live else 20
            sat.metrics = await self.client.request(sat, "GET", "/api/status", timeout=timeout)
            if not sat.online:
                _LOGGER.info("Satellite %s is online", sat.name)
            sat.online, sat.last_seen, sat.last_error = True, time.time(), ""
            hostname = sat.metrics.get("hostname")
            if hostname and hostname != sat.hostname:
                sat.hostname = hostname
                self.store.save()
            upd = sat.agent_update
            if upd and (sat.metrics.get("agent_version") == upd["to"]
                        or time.time() - upd["since"] > self.AGENT_UPDATE_GRACE):
                if sat.metrics.get("agent_version") == upd["to"]:
                    _LOGGER.info("Satellite %s is back on agent %s", sat.name, upd["to"])
                else:
                    _LOGGER.warning("Satellite %s still reports agent %s after the update to %s",
                                    sat.name, sat.metrics.get("agent_version"), upd["to"])
                sat.agent_update = None
        except AgentError as err:
            upd = sat.agent_update
            if upd and time.time() - upd["since"] <= self.AGENT_UPDATE_GRACE:
                # Expected: the agent is restarting on its new version. Keep the device
                # "online" for Home Assistant and show "updating" instead of an error.
                sat.last_error = ""
            else:
                if upd:
                    err = AgentError(f"did not come back after the agent update to {upd['to']} ({err})")
                    sat.agent_update = None
                if sat.online:
                    _LOGGER.warning("Satellite %s went offline: %s", sat.name, err)
                sat.online, sat.last_error = False, str(err)
        # Home Assistant gets updates at the normal pace even while the UI polls fast,
        # so live mode does not flood HA's history database. State changes go out at once.
        last_time, last_status = self._published.get(sat.id, (0.0, ""))
        if sat.status != last_status or time.monotonic() - last_time >= self.options.poll_interval - 0.5:
            self._published[sat.id] = (time.monotonic(), sat.status)
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
            result = await self.client.request(
                sat, "POST", "/api/agent/update", data=self.agent_bundle,
                params={"version": self.agent_version}, timeout=60,
            )
            # The agent now restarts; tell HA and the UI right away that this is an update.
            sat.agent_update = {"to": self.agent_version, "since": time.time()}
            self._published[sat.id] = (time.monotonic(), sat.status)
            await self.bridge.publish(sat)
            return result
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
        if action == "update_agent":  # HA update entity "Install"
            if sat.online and not sat.agent_update:
                try:
                    await self.push_agent_update(sat)
                except AgentError:
                    pass
            return
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
