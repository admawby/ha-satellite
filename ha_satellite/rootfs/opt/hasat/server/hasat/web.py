"""Ingress UI and JSON API (reachable only through Home Assistant's authenticated ingress)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import WSMsgType, web

from .agent_client import AgentError
from .config import INGRESS_PROXY_IPS, WEB_DIR

if TYPE_CHECKING:
    from .manager import Manager

_LOGGER = logging.getLogger(__name__)

# Agent endpoints the UI may reach through the proxy, as (method, path regex).
PROXY_ALLOW = [
    ("GET", r"status"),
    ("GET", r"usb"),
    ("GET", r"serial"),
    ("PUT", r"serial"),
    ("GET", r"updates"),
    ("POST", r"updates/(check|apply)"),
    ("GET", r"settings"),
    ("PUT", r"settings"),
    ("POST", r"system/(reboot|shutdown)"),
    ("POST", r"services/[a-z0-9@_.-]+/(restart|start|stop)"),
    ("POST", r"exec"),
    ("GET", r"logs"),
]


def build_ui_app(mgr: "Manager") -> web.Application:
    @web.middleware
    async def ingress_only(request: web.Request, handler):
        if request.remote not in INGRESS_PROXY_IPS:
            _LOGGER.warning("Blocked non-ingress UI request from %s", request.remote)
            raise web.HTTPForbidden()
        return await handler(request)

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except AgentError as err:
            return web.json_response({"error": str(err)}, status=502)

    def sat_or_404(request: web.Request):
        sat = mgr.store.satellites.get(request.match_info["sat_id"])
        if not sat:
            raise web.HTTPNotFound()
        return sat

    # ---------------------------------------------------------------- pages
    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ api
    async def state(_request: web.Request) -> web.Response:
        return web.json_response({
            "controller": {
                "enroll_host": mgr.enroll_host,
                "enroll_port": mgr.enroll_port,
                "ca_fingerprint": mgr.pki.ca_fingerprint,
                "agent_version": mgr.agent_version,
                "trusted_ips": mgr.trusted_ips(),
            },
            "satellites": [s.to_api() for s in mgr.store.satellites.values()],
        })

    async def enroll_token(request: web.Request) -> web.Response:
        body = await request.json()
        if not mgr.enroll_host:
            return web.json_response({"error": "Set the enrollment_host option first"}, status=400)
        tok = mgr.store.new_token(str(body.get("name", ""))[:64])
        return web.json_response({"command": mgr.install_command(tok.token), "expires": tok.expires})

    async def adopt(request: web.Request) -> web.Response:
        body = await request.json()
        host = str(body.get("host", "")).strip()
        username = str(body.get("username", "")).strip()
        if not host or not username:
            return web.json_response({"error": "host and username are required"}, status=400)
        if not body.get("password") and not body.get("private_key"):
            return web.json_response({"error": "password or private key required"}, status=400)
        job = mgr.adopter.start(
            host=host,
            port=int(body.get("port") or 22),
            username=username,
            password=str(body.get("password") or ""),
            private_key=str(body.get("private_key") or ""),
            name=str(body.get("name") or "")[:64],
        )
        return web.json_response(job.to_api())

    async def job(request: web.Request) -> web.Response:
        j = mgr.adopter.jobs.get(request.match_info["job_id"])
        if not j:
            raise web.HTTPNotFound()
        return web.json_response(j.to_api())

    async def update_sat(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        body = await request.json()
        if name := str(body.get("name", "")).strip():
            mgr.bridge.remove(sat)  # entity ids follow the name
            sat.name = mgr.store.unique_name(name[:64]) if name != sat.name else sat.name
        if host := str(body.get("host", "")).strip():
            if not mgr.options.address_allowed(host):
                return web.json_response({"error": "address not in allowed_networks"}, status=400)
            sat.host = host
        mgr.store.save()
        await mgr.poll_one(sat.id)
        return web.json_response(sat.to_api())

    async def delete_sat(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        await mgr.remove(sat, uninstall=request.query.get("uninstall") == "1")
        return web.json_response({"ok": True})

    async def agent_update(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        return web.json_response(await mgr.push_agent_update(sat))

    async def proxy(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        path = request.match_info["path"]
        if not any(request.method == m and re.fullmatch(p, path) for m, p in PROXY_ALLOW):
            raise web.HTTPForbidden(text="endpoint not allowed")
        raw = await request.text()
        payload = json.loads(raw) if raw.strip() else None
        timeout = 900 if path in ("updates/apply", "exec") else 60
        result = await mgr.client.request(
            sat, request.method, f"/api/{path}", json=payload, params=dict(request.query), timeout=timeout
        )
        if path.startswith(("serial", "settings")) and request.method == "PUT":
            asyncio.create_task(mgr.poll_one(sat.id))
        return web.json_response(result)

    async def terminal(request: web.Request) -> web.WebSocketResponse:
        sat = sat_or_404(request)
        browser = web.WebSocketResponse(heartbeat=30)
        await browser.prepare(request)
        try:
            agent = await mgr.client.ws_connect(sat, "/api/terminal")
        except (aiohttp.ClientError, OSError) as err:
            await browser.send_bytes(f"\r\n\x1b[31mCannot reach {sat.name}: {err}\x1b[0m\r\n".encode())
            await browser.close()
            return browser
        _LOGGER.info("Terminal session opened on %s", sat.name)

        async def browser_to_agent():
            async for msg in browser:
                if msg.type == WSMsgType.TEXT:
                    await agent.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await agent.send_bytes(msg.data)

        async def agent_to_browser():
            async for msg in agent:
                if msg.type == WSMsgType.BINARY:
                    await browser.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await browser.send_str(msg.data)

        tasks = [asyncio.create_task(browser_to_agent()), asyncio.create_task(agent_to_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await agent.close()
            await browser.close()
            _LOGGER.info("Terminal session closed on %s", sat.name)
        return browser

    app = web.Application(middlewares=[ingress_only, errors], client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_static("/static/", WEB_DIR, show_index=False)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/enroll", enroll_token)
    app.router.add_post("/api/adopt", adopt)
    app.router.add_get("/api/jobs/{job_id}", job)
    app.router.add_patch("/api/sat/{sat_id}", update_sat)
    app.router.add_delete("/api/sat/{sat_id}", delete_sat)
    app.router.add_post("/api/sat/{sat_id}/agent-update", agent_update)
    app.router.add_get("/api/sat/{sat_id}/terminal", terminal)
    app.router.add_route("*", "/api/sat/{sat_id}/proxy/{path:.+}", proxy)
    return app
