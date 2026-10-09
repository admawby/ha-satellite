"""LAN-facing enrollment endpoint (TLS, public-key pinned by the install command).

Everything here requires a valid, unused one-time token. A token is created from the
add-on UI, expires after 30 minutes and is burned when the satellite submits its CSR.
The private key of the satellite is generated on the Pi and never leaves it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiohttp import web

from .store import Satellite

if TYPE_CHECKING:
    from .manager import Manager

_LOGGER = logging.getLogger(__name__)
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


def build_enroll_app(mgr: "Manager") -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler):
        remote = request.remote or ""
        if not mgr.options.address_allowed(remote):
            _LOGGER.warning("Enrollment request from %s rejected (not in allowed_networks)", remote)
            raise web.HTTPForbidden(text="forbidden\n")
        token = request.match_info.get("token", "")
        tok = mgr.store.valid_token(token)
        if tok is None:
            _LOGGER.warning("Enrollment request from %s with invalid/expired token", remote)
            raise web.HTTPForbidden(text="invalid or expired enrollment token\n")
        request["enroll_token"] = tok
        return await handler(request)

    async def install_script(request: web.Request) -> web.Response:
        tok = request["enroll_token"]
        script = mgr.render_install_script(tok.token)
        return web.Response(text=script, content_type="text/x-shellscript")

    async def agent_bundle(request: web.Request) -> web.Response:
        return web.Response(body=mgr.agent_bundle, content_type="application/gzip")

    async def submit_csr(request: web.Request) -> web.Response:
        tok = request["enroll_token"]
        csr = await request.read()
        if len(csr) > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(csr))
        hostname = request.query.get("hostname", "")
        if not _HOSTNAME_RE.match(hostname):
            hostname = "raspberrypi"
        sat_id = mgr.store.new_satellite_id()
        remote = request.remote or ""
        # Prefer the address the Pi reports for itself: behind Docker NAT HA may only
        # see a gateway address. It must still be inside allowed_networks.
        reported = request.query.get("ip", "")
        if reported and mgr.options.address_allowed(reported):
            remote = reported
        try:
            cert = mgr.pki.sign_agent_csr(csr, sat_id, remote)
        except Exception as err:  # noqa: BLE001 - report any parse failure to the installer
            raise web.HTTPBadRequest(text=f"bad CSR: {err}\n") from err

        tok.used = True
        sat = Satellite(
            id=sat_id,
            name=mgr.store.unique_name(tok.name or hostname),
            host=remote,
            port=mgr.options.agent_port,
            hostname=hostname,
        )
        mgr.store.satellites[sat.id] = sat
        mgr.store.save()
        _LOGGER.info("Enrolled satellite %s (%s) at %s", sat.name, sat.id, remote)
        mgr.on_enrolled(sat, tok.token)
        return web.json_response({
            "id": sat.id,
            "name": sat.name,
            "cert": cert.decode(),
            "ca": mgr.pki.ca_pem.decode(),
            "controller_cn": "hasat-controller",
            "trusted_ips": mgr.trusted_ips(),
            "agent_port": sat.port,
        })

    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app.router.add_get("/enroll/{token}/install.sh", install_script)
    app.router.add_get("/enroll/{token}/agent.tar.gz", agent_bundle)
    app.router.add_post("/enroll/{token}/csr", submit_csr)
    return app
