"""Entry point: python3 -m hasat_agent"""

from __future__ import annotations

import asyncio
import logging
import os

from .server import Agent


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HASAT_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s (%(name)s) %(message)s",  # journald adds timestamps
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(Agent().run())


if __name__ == "__main__":
    main()
