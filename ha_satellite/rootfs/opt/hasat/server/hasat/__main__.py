"""Entry point: python3 -m hasat"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from . import __version__
from .config import ENROLL_PORT, INGRESS_PORT, load_options
from .enroll import build_enroll_app
from .manager import Manager
from .web import build_ui_app


async def main() -> None:
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    log = logging.getLogger("hasat")
    log.info("HA Satellite manager %s starting", __version__)

    mgr = Manager(options)
    await mgr.start()

    ui = web.AppRunner(build_ui_app(mgr), access_log=None)
    await ui.setup()
    await web.TCPSite(ui, "0.0.0.0", INGRESS_PORT).start()

    enroll = web.AppRunner(build_enroll_app(mgr), access_log=None)
    await enroll.setup()
    await web.TCPSite(enroll, "0.0.0.0", ENROLL_PORT, ssl_context=mgr.pki.enroll_ssl_context()).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Shutting down")
    await enroll.cleanup()
    await ui.cleanup()
    await mgr.stop()


if __name__ == "__main__":
    asyncio.run(main())
