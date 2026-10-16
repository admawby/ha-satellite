"""History links (entity registry) and "MQTT through Home Assistant", against a fake
Supervisor/HA: no MQTT add-on, but HA's MQTT integration is loaded."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import aiohttp
from aiohttp import web

TOKEN = "test-supervisor-token"
EVENTS: list = []  # ("publish", topic, payload, retain) / ("delete", entity_id) in order


def fake_supervisor(sat_id: str) -> web.Application:
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        async for msg in ws:
            req = json.loads(msg.data)
            if req["type"] == "config/entity_registry/list":
                await ws.send_json({"id": 999, "type": "event", "event": {}})  # unrelated traffic is skipped
                await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": [
                    {"unique_id": f"hasat_{sat_id}_cpu_temp", "entity_id": "sensor.garage_pi_temperature", "platform": "mqtt", "disabled_by": None},
                    {"unique_id": f"hasat_{sat_id}_disk_percent", "entity_id": "sensor.hasat_test_pi_disk_percent", "platform": "mqtt", "disabled_by": None},
                    {"unique_id": f"hasat_{sat_id}_agent", "entity_id": "update.hasat_test_pi_agent", "platform": "mqtt", "disabled_by": None},
                    {"unique_id": f"hasat_{sat_id}_load_1", "entity_id": "sensor.hasat_test_pi_load_1", "platform": "mqtt", "disabled_by": "user"},
                    {"unique_id": "something_else", "entity_id": "light.kitchen", "platform": "hue", "disabled_by": None},
                ]})
            elif req["type"] == "subscribe_trigger":
                await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": None})
                if req["trigger"]["topic"].endswith("/cmd"):
                    await asyncio.sleep(0.3)
                    await ws.send_json({"id": req["id"], "type": "event", "event": {"variables": {"trigger": {
                        "platform": "mqtt", "topic": f"hasat/{sat_id}/cmd", "payload": "check_updates"}}}})
        return ws

    async def no_mqtt_service(_request):
        return web.json_response({"result": "error", "message": "No service"}, status=400)

    async def core_config(_request):
        return web.json_response({"components": ["sensor", "mqtt", "update"], "version": "2026.10.0"})

    async def mqtt_publish(request):
        body = await request.json()
        EVENTS.append(("publish", body["topic"], body["payload"], body.get("retain")))
        return web.json_response([])

    async def delete_state(request):
        EVENTS.append(("delete", request.match_info["entity_id"]))
        return web.json_response({"message": "Entity not found."}, status=404)

    app = web.Application()
    app.router.add_get("/core/websocket", websocket)
    app.router.add_get("/services/mqtt", no_mqtt_service)
    app.router.add_get("/core/api/config", core_config)
    app.router.add_post("/core/api/services/mqtt/publish", mqtt_publish)
    app.router.add_delete("/core/api/states/{entity_id}", delete_state)
    return app


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, mgr, sat) -> None:
    from hasat import ha_entities

    mgr.options.publish_to_ha = True
    saved_mqtt = mgr.bridge._mqtt
    try:
        mgr.bridge._mqtt = object()  # pretend MQTT discovery is in use
        async with http.get(f"{ui}/api/state") as r:
            ents = (await r.json())["entities"].get(sat.id, {})
        check(ents.get("cpu_temp") == "sensor.garage_pi_temperature", "history: entity renamed in HA is still found")
        check(ents.get("disk_percent") == "sensor.hasat_test_pi_disk_percent" and ents.get("agent") == "update.hasat_test_pi_agent",
              "history: metrics mapped to their entity IDs (incl. update entity)")
        check("load_1" not in ents and "light.kitchen" not in ents.values(), "history: disabled and unrelated entities ignored")

        mgr.bridge._mqtt = None  # REST fallback: ids are fixed
        mgr.entities._registry = {}
        mgr.entities._fetched = 10**12  # keep the empty cache
        async with http.get(f"{ui}/api/state") as r:
            ents = (await r.json())["entities"].get(sat.id, {})
        check(ents.get("cpu_percent") == f"sensor.hasat_{sat.slug}_cpu_percent" and "agent" not in ents,
              "history: REST fallback uses the fixed sensor.hasat_* ids")

        ha_entities.SUPERVISOR_TOKEN = "wrong"
        mgr.entities._registry, mgr.entities._fetched, mgr.entities._failed_at = {}, 0.0, 0.0
        mgr.bridge._mqtt = object()
        async with http.get(f"{ui}/api/state") as r:
            body = await r.json()
        check(r.status == 200 and body["entities"] == {}, "history: HA unreachable or refusing -> page still works, no links")
    finally:
        ha_entities.SUPERVISOR_TOKEN = TOKEN
        mgr.bridge._mqtt = saved_mqtt
        mgr.options.publish_to_ha = False


async def run_ha_mqtt(check: Callable[[bool, str], None], sat) -> None:
    """Broker not provided by an add-on, but HA's MQTT integration is loaded."""
    from hasat.ha_bridge import HABridge

    EVENTS.clear()
    commands: list = []

    async def on_command(sat_id, action):
        commands.append((sat_id, action))

    bridge = HABridge(on_command)
    bridge.latest_agent_version = "9.9.9"
    await bridge.start()
    try:
        check(bridge._via_ha, "mqtt via HA: used when there is no MQTT add-on but HA's MQTT integration is set up")
        await bridge.publish(sat)
        for _ in range(50):
            if any(e[0] == "publish" and e[1].endswith("/state") for e in EVENTS):
                break
            await asyncio.sleep(0.1)
        pubs = {e[1]: e for e in EVENTS if e[0] == "publish"}
        cfg_topic = f"homeassistant/sensor/hasat_{sat.id}/cpu_temp/config"
        cfg = json.loads(pubs[cfg_topic][2])
        check(cfg["unique_id"] == f"hasat_{sat.id}_cpu_temp" and pubs[cfg_topic][3] is True
              and cfg["default_entity_id"] == f"sensor.hasat_{sat.slug}_cpu_temp",
              "mqtt via HA: discovery with unique IDs published (retained) through mqtt.publish")
        first_publish = next(i for i, e in enumerate(EVENTS) if e[0] == "publish")
        deleted = [e[1] for e in EVENTS[:first_publish] if e[0] == "delete"]
        check(f"sensor.hasat_{sat.slug}_cpu_temp" in deleted,
              "mqtt via HA: old ID-less states removed first, so entities keep their IDs and history")
        check(f"hasat/{sat.id}/state" in pubs and json.loads(pubs[f"hasat/{sat.id}/state"][2])["cpu_percent"] is not None,
              "mqtt via HA: sensor values published")
        for _ in range(50):
            if commands:
                break
            await asyncio.sleep(0.1)
        check(commands == [(sat.id, "check_updates")], "mqtt via HA: button presses arrive through HA's websocket")
    finally:
        await bridge.stop()
    last = [e for e in EVENTS if e[0] == "publish" and e[1] == "hasat/bridge/availability"]
    check(last and last[-1][2] == "offline", "mqtt via HA: marked unavailable when the add-on stops")
