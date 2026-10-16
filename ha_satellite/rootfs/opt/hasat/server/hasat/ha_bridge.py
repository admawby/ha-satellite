"""Publish satellite health into Home Assistant.

Preferred path: MQTT discovery (devices, sensors, buttons) when an MQTT broker is
provided by the Supervisor. Fallback: plain state objects via the Core REST API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from . import __version__
from .config import SUPERVISOR_TOKEN, SUPERVISOR_URL
from .ha_mqtt import HAMqttAdapter
from .store import Satellite

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt_t

_LOGGER = logging.getLogger(__name__)
DISCOVERY_PREFIX = "homeassistant"
BASE = "hasat"


@dataclass(frozen=True)
class Entity:
    key: str
    name: str
    component: str = "sensor"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    category: str | None = None
    precision: int | None = None
    feature: str = "health"  # entity only exists if the satellite offers this feature
    always_available: bool = False  # not tied to the satellite's availability (e.g. Connection)


SENSORS: list[Entity] = [
    Entity("cpu_temp", "CPU temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1),
    Entity("cpu_percent", "CPU usage", unit="%", state_class="measurement", icon="mdi:cpu-64-bit", precision=0),
    Entity("mem_percent", "Memory usage", unit="%", state_class="measurement", icon="mdi:memory", precision=0),
    Entity("disk_percent", "Disk usage", unit="%", state_class="measurement", icon="mdi:harddisk", precision=0),
    Entity("load_1", "Load (1m)", state_class="measurement", icon="mdi:gauge", precision=2),
    Entity("boot_time", "Last boot", device_class="timestamp", category="diagnostic"),
    Entity("updates_available", "Package updates", icon="mdi:package-up", state_class="measurement", feature="updates"),
    Entity("docker_updates", "Container updates", icon="mdi:docker", state_class="measurement", feature="docker"),
    Entity("containers_running", "Containers running", icon="mdi:docker", state_class="measurement", feature="docker"),
    Entity("agent_version", "Agent version", icon="mdi:tag", category="diagnostic"),
    Entity("connection", "Connection", icon="mdi:lan-connect", category="diagnostic", always_available=True),
    Entity("agent", "Satellite agent", component="update", device_class="firmware", category="config"),
    Entity("ip_address", "IP address", icon="mdi:ip-network", category="diagnostic"),
    Entity("throttled", "Throttled", component="binary_sensor", device_class="problem", feature="power"),
    Entity("under_voltage", "Under-voltage", component="binary_sensor", device_class="problem", feature="power"),
    Entity("reboot_required", "Reboot required", component="binary_sensor", device_class="update", category="diagnostic", feature="updates"),
    Entity("reboot", "Reboot", component="button", device_class="restart", category="config", feature="power"),
    Entity("check_updates", "Check for updates", component="button", icon="mdi:package-down", category="config", feature="updates"),
    Entity("apply_updates", "Install updates", component="button", icon="mdi:package-up", category="config", feature="updates"),
    Entity("update_containers", "Update containers", component="button", icon="mdi:docker", category="config", feature="docker"),
]

BUTTON_ACTIONS = {e.key for e in SENSORS if e.component == "button"} | {"update_agent"}
ALL_FEATURES = {"health", "terminal", "docker", "docker_install", "radios", "files",
                "updates", "logs", "power", "services", "firewall"}


def sat_features(sat: Satellite) -> set:
    """Features reported by the agent; older agents report none and have everything."""
    feats = (sat.metrics or {}).get("features")
    return set(feats) if feats else set(ALL_FEATURES)


def flatten(sat: Satellite) -> dict[str, Any]:
    """Map raw agent metrics onto the entity keys above."""
    m = sat.metrics or {}
    thr = m.get("throttled") or {}
    upd = m.get("updates") or {}
    load = m.get("load") or [None]
    dock = m.get("docker") or {}
    return {
        "cpu_temp": m.get("cpu_temp"),
        "cpu_percent": m.get("cpu_percent"),
        "mem_percent": m.get("mem_percent"),
        "disk_percent": m.get("disk_percent"),
        "load_1": load[0],
        "boot_time": m.get("boot_time_iso"),
        "updates_available": upd.get("available"),
        "docker_updates": dock.get("updates_available") if dock.get("available") else None,
        "containers_running": dock.get("running") if dock.get("available") else None,
        "agent_version": m.get("agent_version"),
        "connection": sat.status,
        "ip_address": sat.host,
        "throttled": bool(thr.get("throttled_now")),
        "under_voltage": bool(thr.get("under_voltage_now")),
        "reboot_required": bool(upd.get("reboot_required")),
    }


class HABridge:
    def __init__(self, on_command: Callable[[str, str], Awaitable[None]]) -> None:
        self._on_command = on_command
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mqtt: mqtt_t.Client | None = None
        self._mqtt_connected = False
        self._announced: dict[str, tuple] = {}
        self._http: aiohttp.ClientSession | None = None
        self.latest_agent_version: str | None = None  # set by the manager
        self._via_ha = False                # MQTT through Home Assistant's mqtt.publish service
        self._retry_task: asyncio.Task | None = None
        self._rest_cleaned: set[str] = set()

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
        creds = await self._mqtt_service()
        if creds:
            self._start_mqtt(creds)
        elif SUPERVISOR_TOKEN and await HAMqttAdapter.available(self._http):
            self._use_ha_mqtt()
        else:
            self._rest_fallback_notice()
            if SUPERVISOR_TOKEN:
                self._retry_task = asyncio.create_task(self._retry_ha_mqtt())

    async def stop(self) -> None:
        if self._retry_task:
            self._retry_task.cancel()
        if self._via_ha:
            self._mqtt.publish(f"{BASE}/bridge/availability", "offline", retain=True)
            await self._mqtt.stop()
        elif self._mqtt:
            try:
                self._mqtt.publish(f"{BASE}/bridge/availability", "offline", retain=True).wait_for_publish(2)
            except (RuntimeError, ValueError):
                pass  # not connected; the broker's last-will covers it
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        if self._http:
            await self._http.close()

    async def _mqtt_service(self) -> dict | None:
        if not SUPERVISOR_TOKEN or not self._http:
            return None
        try:
            async with self._http.get(f"{SUPERVISOR_URL}/services/mqtt", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return (await r.json()).get("data")
        except aiohttp.ClientError:
            return None

    def _start_mqtt(self, creds: dict) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-satellite-manager")
        if creds.get("username"):
            client.username_pw_set(creds["username"], creds.get("password"))
        if creds.get("ssl"):
            client.tls_set()
        client.will_set(f"{BASE}/bridge/availability", "offline", retain=True)
        client.on_connect = self._mqtt_on_connect
        client.on_disconnect = self._mqtt_on_disconnect
        client.on_message = self._mqtt_on_message
        client.reconnect_delay_set(1, 60)
        client.connect_async(creds["host"], int(creds.get("port", 1883)))
        client.loop_start()
        self._mqtt = client
        _LOGGER.info("Publishing satellites to Home Assistant via MQTT at %s", creds["host"])

    def _mqtt_on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if getattr(reason_code, "is_failure", False):
            _LOGGER.error("MQTT connection refused: %s", reason_code)
            return
        self._mqtt_connected = True
        self._announced.clear()  # re-send discovery after (re)connect
        client.publish(f"{BASE}/bridge/availability", "online", retain=True)
        client.subscribe(f"{BASE}/+/cmd")
        client.subscribe(f"{DISCOVERY_PREFIX}/status")

    def _mqtt_on_disconnect(self, *_args) -> None:
        self._mqtt_connected = False

    def _mqtt_on_message(self, _client, _userdata, msg) -> None:
        if msg.topic == f"{DISCOVERY_PREFIX}/status":
            if msg.payload == b"online":
                self._announced.clear()  # HA restarted: re-announce on next poll
            return
        parts = msg.topic.split("/")
        action = msg.payload.decode(errors="ignore").strip()
        if len(parts) == 3 and action in BUTTON_ACTIONS and self._loop:
            asyncio.run_coroutine_threadsafe(self._on_command(parts[1], action), self._loop)

    HA_MQTT_RETRY = 300.0  # seconds between checks whether HA's MQTT works (again)

    def _use_ha_mqtt(self) -> None:
        """Broker outside HA's add-ons: publish through HA's own MQTT integration."""
        adapter = HAMqttAdapter(self._http, self._ha_mqtt_message, self._ha_mqtt_reconnected,
                                [f"{BASE}/+/cmd", f"{DISCOVERY_PREFIX}/status"])
        adapter.start()
        self._mqtt, self._mqtt_connected, self._via_ha = adapter, True, True
        self._announced.clear()
        self._rest_cleaned.clear()
        _LOGGER.info("No MQTT add-on found; publishing via Home Assistant's MQTT integration (mqtt.publish)")

    @staticmethod
    def _rest_fallback_notice() -> None:
        _LOGGER.info("No working MQTT; publishing sensors through the Home Assistant REST API "
                     "(these entities have no unique ID; connect the MQTT integration to a broker to manage them in the UI)")

    async def _fall_back_to_rest(self) -> None:
        """HA's MQTT stopped working: go back to plain states so the entities stay visible."""
        _LOGGER.warning("Home Assistant could not publish over MQTT %d times in a row; switching back to plain "
                        "states and re-checking MQTT every %d s", HAMqttAdapter.FAILURE_LIMIT, int(self.HA_MQTT_RETRY))
        adapter = self._mqtt
        self._mqtt, self._mqtt_connected, self._via_ha = None, False, False
        self._announced.clear()
        self._rest_cleaned.clear()
        asyncio.create_task(adapter.stop())
        if not self._retry_task or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_ha_mqtt())

    async def _retry_ha_mqtt(self) -> None:
        while not self._mqtt:
            await asyncio.sleep(self.HA_MQTT_RETRY)
            if not self._mqtt and await HAMqttAdapter.available(self._http):
                self._use_ha_mqtt()

    async def _ha_mqtt_message(self, topic: str, payload: str) -> None:
        if topic == f"{DISCOVERY_PREFIX}/status":
            if payload == "online":
                self._announced.clear()
            return
        parts = topic.split("/")
        action = payload.strip()
        if len(parts) == 3 and action in BUTTON_ACTIONS:
            await self._on_command(parts[1], action)

    def _ha_mqtt_reconnected(self) -> None:
        self._announced.clear()  # HA may have restarted: announce entities again
        if self._mqtt:
            self._mqtt.publish(f"{BASE}/bridge/availability", "online", retain=True)

    async def _delete_rest_states(self, sat: Satellite) -> None:
        """Remove plain states written by the REST fallback (they have no unique ID).

        Done before MQTT discovery so the new entities can take over the same entity IDs,
        which keeps their history."""
        if not (self._http and SUPERVISOR_TOKEN):
            return
        for ent in SENSORS:
            if ent.component not in ("sensor", "binary_sensor"):
                continue
            try:
                async with self._http.delete(f"{SUPERVISOR_URL}/core/api/states/{ent.component}.hasat_{sat.slug}_{ent.key}",
                                             timeout=aiohttp.ClientTimeout(total=10)):
                    pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

    # ------------------------------------------------------------ publishing
    async def publish(self, sat: Satellite) -> None:
        try:
            if self._via_ha and not self._mqtt.healthy:
                await self._fall_back_to_rest()
            if self._mqtt:
                if sat.id not in self._rest_cleaned:
                    self._rest_cleaned.add(sat.id)
                    await self._delete_rest_states(sat)
                self._publish_mqtt(sat)
            else:
                await self._publish_rest(sat)
        except Exception:  # noqa: BLE001 - never let HA publishing break polling
            _LOGGER.exception("Failed publishing %s to Home Assistant", sat.name)

    def _device(self, sat: Satellite) -> dict:
        m = sat.metrics or {}
        return {
            "identifiers": [f"hasat_{sat.id}"],
            "name": f"Satellite {sat.name}",
            "manufacturer": "Raspberry Pi" if "raspberry" in str(m.get("model", "")).lower() else "HA Satellite",
            "model": m.get("model") or "Linux host",
            "sw_version": m.get("os") or None,
        }

    def _publish_mqtt(self, sat: Satellite) -> None:
        if not self._mqtt or not self._mqtt_connected:
            return
        avail_topic = f"{BASE}/{sat.id}/availability"
        state_topic = f"{BASE}/{sat.id}/state"
        feats = sat_features(sat)
        key = tuple(sorted(feats))
        # Announce once we know the satellite's features, and again if they change.
        if sat.metrics and self._announced.get(sat.id) != key:
            device = self._device(sat)
            for ent in SENSORS:
                topic = f"{DISCOVERY_PREFIX}/{ent.component}/hasat_{sat.id}/{ent.key}/config"
                if ent.feature not in feats:
                    self._mqtt.publish(topic, "", retain=True)  # drop entities this satellite cannot have
                    continue
                uid = f"hasat_{sat.id}_{ent.key}"
                cfg: dict[str, Any] = {
                    "name": ent.name,
                    "unique_id": uid,
                    "default_entity_id": f"{ent.component}.hasat_{sat.slug}_{ent.key}",
                    "device": device,
                    "availability_mode": "all",
                    "availability": [{"topic": f"{BASE}/bridge/availability"}]
                    + ([] if ent.always_available else [{"topic": avail_topic}]),
                    "origin": {"name": "HA Satellite", "sw_version": __version__},
                }
                if ent.component == "button":
                    cfg.update(command_topic=f"{BASE}/{sat.id}/cmd", payload_press=ent.key)
                elif ent.component == "update":
                    # Native HA update entity: versions + "Installing..." while the agent updates.
                    cfg.update(state_topic=f"{BASE}/{sat.id}/agent_update", command_topic=f"{BASE}/{sat.id}/cmd",
                               payload_install="update_agent", title="HA Satellite agent")
                elif ent.key == "connection":
                    cfg.update(state_topic=f"{BASE}/{sat.id}/connection")  # plain text, always published
                else:
                    cfg.update(state_topic=state_topic, value_template=f"{{{{ value_json.{ent.key} }}}}")
                if ent.component == "binary_sensor":
                    cfg["value_template"] = f"{{{{ 'ON' if value_json.{ent.key} else 'OFF' }}}}"
                for attr, val in (("unit_of_measurement", ent.unit), ("device_class", ent.device_class),
                                  ("state_class", ent.state_class), ("icon", ent.icon),
                                  ("entity_category", ent.category),
                                  ("suggested_display_precision", ent.precision)):
                    if val is not None:
                        cfg[attr] = val
                self._mqtt.publish(topic, json.dumps(cfg), retain=True)
            self._announced[sat.id] = key
        # During an agent update the device keeps "online" availability: the restart is expected.
        self._mqtt.publish(avail_topic, "online" if sat.online else "offline", retain=True)
        self._mqtt.publish(f"{BASE}/{sat.id}/connection", sat.status, retain=True)
        if sat.online:
            self._mqtt.publish(state_topic, json.dumps(flatten(sat)), retain=True)
        installed = (sat.metrics or {}).get("agent_version")
        if installed:
            self._mqtt.publish(f"{BASE}/{sat.id}/agent_update", json.dumps({
                "installed_version": installed,
                "latest_version": self.latest_agent_version or installed,
                "in_progress": bool(sat.agent_update),
            }), retain=True)

    async def _publish_rest(self, sat: Satellite) -> None:
        if not self._http or not SUPERVISOR_TOKEN:
            return
        values = flatten(sat)
        feats = sat_features(sat)
        for ent in SENSORS:
            if ent.component in ("button", "update") or ent.feature not in feats:
                continue
            domain = "binary_sensor" if ent.component == "binary_sensor" else "sensor"
            entity_id = f"{domain}.hasat_{sat.slug}_{ent.key}"
            val = values.get(ent.key)
            if not sat.online and not ent.always_available:
                state = "unavailable"
            elif ent.component == "binary_sensor":
                state = "on" if val else "off"
            else:
                state = "unknown" if val is None else str(val)
            attrs: dict[str, Any] = {"friendly_name": f"Satellite {sat.name} {ent.name}"}
            for attr, v in (("unit_of_measurement", ent.unit), ("device_class", ent.device_class),
                            ("state_class", ent.state_class), ("icon", ent.icon)):
                if v is not None:
                    attrs[attr] = v
            async with self._http.post(
                f"{SUPERVISOR_URL}/core/api/states/{entity_id}",
                json={"state": state, "attributes": attrs},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.debug("REST publish of %s failed: %s", entity_id, resp.status)

    async def remove(self, sat: Satellite) -> None:
        """Delete the satellite's entities from HA (MQTT discovery and REST states)."""
        if not self._mqtt:
            await self._delete_rest_states(sat)
        self._rest_cleaned.discard(sat.id)
        if self._mqtt and self._mqtt_connected:
            for ent in SENSORS:
                self._mqtt.publish(f"{DISCOVERY_PREFIX}/{ent.component}/hasat_{sat.id}/{ent.key}/config", "", retain=True)
            for suffix in ("state", "availability", "connection", "agent_update"):
                self._mqtt.publish(f"{BASE}/{sat.id}/{suffix}", "", retain=True)
        self._announced.pop(sat.id, None)
