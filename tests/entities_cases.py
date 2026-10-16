"""History links: satellite metrics -> Home Assistant entity IDs (via a fake HA websocket)."""

from __future__ import annotations

from typing import Callable

import aiohttp
from aiohttp import web

TOKEN = "test-supervisor-token"


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
        req = await ws.receive_json()
        assert req["type"] == "config/entity_registry/list"
        await ws.send_json({"id": 999, "type": "event", "event": {}})  # unrelated traffic is skipped
        await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": [
            {"unique_id": f"hasat_{sat_id}_cpu_temp", "entity_id": "sensor.garage_pi_temperature", "platform": "mqtt", "disabled_by": None},
            {"unique_id": f"hasat_{sat_id}_disk_percent", "entity_id": "sensor.hasat_test_pi_disk_percent", "platform": "mqtt", "disabled_by": None},
            {"unique_id": f"hasat_{sat_id}_agent", "entity_id": "update.hasat_test_pi_agent", "platform": "mqtt", "disabled_by": None},
            {"unique_id": f"hasat_{sat_id}_load_1", "entity_id": "sensor.hasat_test_pi_load_1", "platform": "mqtt", "disabled_by": "user"},
            {"unique_id": "something_else", "entity_id": "light.kitchen", "platform": "hue", "disabled_by": None},
        ]})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/core/websocket", websocket)
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
