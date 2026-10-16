"""Map satellite metrics to their Home Assistant entity IDs.

The UI uses this to open Home Assistant's own "more info" dialog (history graph)
when you click a value. Entity IDs are looked up in HA's entity registry by their
unique ID (hasat_<satellite id>_<key>), so entities you renamed in HA still work.
With the REST fallback (no MQTT) the entity IDs are fixed: sensor.hasat_<name>_<key>.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict

import aiohttp

from .config import SUPERVISOR_TOKEN, SUPERVISOR_URL
from .ha_bridge import entities_for

if TYPE_CHECKING:
    from .manager import Manager

_LOGGER = logging.getLogger(__name__)
CACHE_SECONDS = 300
RETRY_SECONDS = 30  # re-check sooner when a satellite has no entities yet (just launched)


class EntityResolver:
    def __init__(self, mgr: "Manager") -> None:
        self.mgr = mgr
        self._registry: Dict[str, str] = {}   # unique_id -> entity_id
        self._fetched = 0.0
        self._failed_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_registry(self) -> None:
        await asyncio.wait_for(self._fetch_registry_inner(), 20)

    async def _fetch_registry_inner(self) -> None:
        """Read HA's entity registry over the Core WebSocket API (via the Supervisor)."""
        url = SUPERVISOR_URL.replace("http://", "ws://").replace("https://", "wss://") + "/core/websocket"
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url) as ws:
                hello = await ws.receive_json()
                if hello.get("type") == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                    auth = await ws.receive_json()
                    if auth.get("type") != "auth_ok":
                        raise RuntimeError(f"HA websocket auth failed: {auth.get('message', auth.get('type'))}")
                await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
                while True:
                    msg = await ws.receive_json()
                    if msg.get("id") == 1:
                        break
        if not msg.get("success"):
            raise RuntimeError(f"entity registry: {msg.get('error')}")
        self._registry = {
            e["unique_id"]: e["entity_id"]
            for e in msg.get("result", [])
            if str(e.get("unique_id", "")).startswith("hasat_") and not e.get("disabled_by")
        }
        self._fetched = time.monotonic()

    async def entity_map(self) -> Dict[str, Dict[str, str]]:
        """{satellite id: {metric key: entity_id}} for every entity HA knows about."""
        if not self.mgr.options.publish_to_ha:
            return {}
        sats = list(self.mgr.store.satellites.values())
        now = time.monotonic()
        missing = any(not any(u.startswith(f"hasat_{s.id}_") for u in self._registry) for s in sats)
        stale = now - self._fetched > CACHE_SECONDS or (missing and now - self._fetched > RETRY_SECONDS)
        if SUPERVISOR_TOKEN and stale and now - self._failed_at > RETRY_SECONDS and not self._lock.locked():
            async with self._lock:  # concurrent page refreshes use the cache meanwhile
                try:
                    await self._fetch_registry()
                except Exception as err:  # noqa: BLE001 - history links are a convenience
                    self._failed_at = now
                    _LOGGER.debug("Could not read HA entity registry: %s", err)

        result: Dict[str, Dict[str, str]] = {}
        rest_mode = self.mgr.bridge._mqtt is None
        for sat in sats:
            ents: Dict[str, str] = {}
            for ent in entities_for(sat):
                if ent.component == "button":
                    continue
                entity_id = self._registry.get(f"hasat_{sat.id}_{ent.key}")
                if not entity_id and rest_mode and ent.component in ("sensor", "binary_sensor"):
                    entity_id = f"{ent.component}.hasat_{sat.slug}_{ent.key}"  # REST fallback ids are fixed
                if entity_id:
                    ents[ent.key] = entity_id
            if ents:
                result[sat.id] = ents
        return result
