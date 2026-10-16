"""MQTT through Home Assistant: for setups whose broker is not a Home Assistant add-on.

When the Supervisor offers no MQTT service (e.g. the broker runs on another machine)
but Home Assistant's MQTT integration is set up, the add-on still uses MQTT
discovery, so entities get unique IDs, devices and full UI settings:

* messages are published with HA's own ``mqtt.publish`` service (HA's broker
  connection and credentials are used; the add-on needs none)
* commands (buttons, update "Install") arrive through an MQTT trigger subscribed
  over HA's websocket API

The class mimics the small part of paho's client the bridge uses: publish() and
a "connected" flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import aiohttp

from .config import SUPERVISOR_TOKEN, SUPERVISOR_URL

_LOGGER = logging.getLogger(__name__)


class _Done:
    """Return value of publish(), like paho's MQTTMessageInfo."""

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        return None


class HAMqttAdapter:
    def __init__(self, http: aiohttp.ClientSession, on_message: Callable[[str, str], Awaitable[None]],
                 on_reconnect: Callable[[], None], subscribe: list) -> None:
        self._http = http
        self._on_message = on_message
        self._on_reconnect = on_reconnect
        self._subscribe = subscribe
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list = []
        self.connected = False

    @staticmethod
    async def available(http: aiohttp.ClientSession) -> bool:
        """True when Home Assistant has the MQTT integration loaded."""
        try:
            async with http.get(f"{SUPERVISOR_URL}/core/api/config", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return False
                return "mqtt" in (await r.json()).get("components", [])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._listener())]
        self.connected = True

    async def stop(self) -> None:
        # Flush what is queued (e.g. "offline" availability) before shutting down.
        try:
            await asyncio.wait_for(self._queue.join(), 5)
        except asyncio.TimeoutError:
            pass
        for task in self._tasks:
            task.cancel()
        self.connected = False

    # ------------------------------------------------------------- publish
    def publish(self, topic: str, payload, retain: bool = False) -> _Done:
        self._queue.put_nowait((topic, "" if payload is None else str(payload), retain))
        return _Done()

    async def _sender(self) -> None:
        while True:
            topic, payload, retain = await self._queue.get()
            try:
                for attempt in range(3):
                    try:
                        async with self._http.post(
                            f"{SUPERVISOR_URL}/core/api/services/mqtt/publish",
                            json={"topic": topic, "payload": payload, "retain": retain, "qos": 0},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as r:
                            if r.status < 400:
                                break
                            _LOGGER.debug("mqtt.publish %s -> HTTP %s", topic, r.status)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                        _LOGGER.debug("mqtt.publish %s failed: %s", topic, err)
                    await asyncio.sleep(2 * (attempt + 1))
            finally:
                self._queue.task_done()

    # -------------------------------------------------------------- subscribe
    async def _listener(self) -> None:
        """Receive MQTT messages via an MQTT trigger on HA's websocket; reconnect forever."""
        url = SUPERVISOR_URL.replace("http://", "ws://").replace("https://", "wss://") + "/core/websocket"
        delay = 2
        while True:
            try:
                async with self._http.ws_connect(url, heartbeat=30) as ws:
                    hello = await ws.receive_json()
                    if hello.get("type") == "auth_required":
                        await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                        if (await ws.receive_json()).get("type") != "auth_ok":
                            raise RuntimeError("Home Assistant websocket authentication failed")
                    for i, topic in enumerate(self._subscribe, start=1):
                        await ws.send_json({"id": i, "type": "subscribe_trigger",
                                            "trigger": {"platform": "mqtt", "topic": topic}})
                    delay = 2
                    self._on_reconnect()  # HA (re)connected: announce entities again
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = json.loads(msg.data)
                        if data.get("type") != "event":
                            continue
                        trig = ((data.get("event") or {}).get("variables") or {}).get("trigger") or {}
                        if trig.get("topic"):
                            await self._on_message(trig["topic"], str(trig.get("payload", "")))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep listening across HA restarts
                _LOGGER.debug("HA websocket for MQTT commands: %s", err)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
