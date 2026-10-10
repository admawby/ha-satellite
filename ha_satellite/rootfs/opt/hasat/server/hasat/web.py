"""Ingress UI and JSON API (reachable only through Home Assistant's authenticated ingress)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import WSMsgType, web

from . import __version__
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
    ("GET", r"storage"),
    ("POST", r"storage/(mount|unmount)"),
    ("GET", r"files/(list|read)"),
    ("PUT", r"files/write"),
    ("POST", r"files/(mkdir|rename|delete)"),
    ("GET", r"agent/uninstall-plan"),
    ("GET", r"docker"),
    ("POST", r"docker/install"),
    ("POST", r"docker/(check|update)"),
    ("POST", r"docker/containers/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/(start|stop|restart|update)"),
    ("GET", r"docker/containers/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/logs"),
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
    async def index(_request: web.Request) -> web.Response:
        # Version + mtime in asset URLs so browsers fetch new CSS/JS after an update.
        stamp = int(max((WEB_DIR / f).stat().st_mtime for f in ("app.js", "style.css")))
        html = (WEB_DIR / "index.html").read_text().replace("__VERSION__", f"{__version__}-{stamp}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

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

    async def launch(request: web.Request) -> web.Response:
        body = await request.json()
        host = str(body.get("host", "")).strip()
        username = str(body.get("username", "")).strip()
        if not host or not username:
            return web.json_response({"error": "host and username are required"}, status=400)
        if not body.get("password") and not body.get("private_key"):
            return web.json_response({"error": "password or private key required"}, status=400)
        job = mgr.launcher.start(
            host=host,
            port=int(body.get("port") or 22),
            username=username,
            password=str(body.get("password") or ""),
            private_key=str(body.get("private_key") or ""),
            name=str(body.get("name") or "")[:64],
        )
        return web.json_response(job.to_api())

    async def job(request: web.Request) -> web.Response:
        j = mgr.launcher.jobs.get(request.match_info["job_id"])
        if not j:
            raise web.HTTPNotFound()
        return web.json_response(j.to_api())

    async def update_sat(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        body = await request.json()
        if name := str(body.get("name", "")).strip():
            await mgr.bridge.remove(sat)  # entity ids follow the name
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
        report = await mgr.remove(sat, uninstall=request.query.get("uninstall") == "1",
                                  remove_docker=request.query.get("remove_docker") == "1")
        return web.json_response(dict(report, ok=True))

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
        timeout = 900 if path in ("updates/apply", "exec") else 180 if path.startswith("docker") else 60
        result = await mgr.client.request(
            sat, request.method, f"/api/{path}", json=payload, params=dict(request.query), timeout=timeout
        )
        if path.startswith(("serial", "settings")) and request.method == "PUT":
            asyncio.create_task(mgr.poll_one(sat.id))
        return web.json_response(result)

    async def _agent_error(resp) -> web.Response:
        try:
            msg = (await resp.json()).get("error")
        except (ValueError, aiohttp.ContentTypeError):
            msg = await resp.text()
        return web.json_response({"error": f"{resp.status}: {msg}"}, status=502)

    async def file_download(request: web.Request) -> web.StreamResponse:
        sat = sat_or_404(request)
        async with mgr.client.raw(sat, "GET", "/api/files/download", params={"path": request.query.get("path", "")}) as resp:
            if resp.status >= 400:
                return await _agent_error(resp)
            out = web.StreamResponse(headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": resp.headers.get("Content-Disposition", "attachment"),
            })
            if resp.content_length is not None:
                out.content_length = resp.content_length
            await out.prepare(request)
            async for chunk in resp.content.iter_chunked(256 * 1024):
                await out.write(chunk)
            await out.write_eof()
            return out

    async def file_upload(request: web.Request) -> web.Response:
        sat = sat_or_404(request)
        params = {"path": request.query.get("path", ""), "overwrite": request.query.get("overwrite", "0")}
        async with mgr.client.raw(sat, "POST", "/api/files/upload", params=params, data=request.content) as resp:
            if resp.status >= 400:
                return await _agent_error(resp)
            return web.json_response(await resp.json())

    async def terminal(request: web.Request) -> web.WebSocketResponse:
        sat = sat_or_404(request)
        browser = web.WebSocketResponse(heartbeat=30)
        await browser.prepare(request)
        await browser.send_bytes(f"\x1b[90mConnecting to {sat.name} ({sat.host})...\x1b[0m\r\n".encode())
        try:
            agent = await mgr.client.ws_connect(sat, "/api/terminal")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
            reason = ("no answer within 15 s (device off, IP changed, or firewall?)"
                      if isinstance(err, asyncio.TimeoutError) else str(err))
            _LOGGER.warning("Terminal: cannot reach %s: %s", sat.name, reason)
            await browser.send_bytes(f"\x1b[31mCannot reach {sat.name}: {reason}\x1b[0m\r\n".encode())
            await browser.close(code=4502, message=b"satellite unreachable")
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
    app.router.add_post("/api/launch", launch)
    app.router.add_get("/api/jobs/{job_id}", job)
    app.router.add_patch("/api/sat/{sat_id}", update_sat)
    app.router.add_delete("/api/sat/{sat_id}", delete_sat)
    app.router.add_post("/api/sat/{sat_id}/agent-update", agent_update)
    app.router.add_get("/api/sat/{sat_id}/terminal", terminal)
    app.router.add_get("/api/sat/{sat_id}/files/download", file_download)
    app.router.add_post("/api/sat/{sat_id}/files/upload", file_upload)
    app.router.add_route("*", "/api/sat/{sat_id}/proxy/{path:.+}", proxy)
    return app
