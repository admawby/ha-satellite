"""Interactive-terminal sessions through the UI proxy, including reconnects (Linux only)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Callable

import aiohttp


def ui_connector(http: aiohttp.ClientSession, url: str):
    return lambda: http.ws_connect(url)


def agent_connector(client, sat):
    """Talk to an agent directly (as the manager does), bypassing the UI proxy."""
    @asynccontextmanager
    async def connect():
        ws = await client.ws_connect(sat, "/api/terminal")
        try:
            yield ws
        finally:
            await ws.close()
    return connect


async def _session(connect, command: str, marker: str, timeout: float = 15) -> str:
    """Open a terminal, run one command, return everything seen until the marker appears."""
    seen = ""
    async with connect() as ws:
        await ws.send_str(json.dumps({"t": "r", "c": 100, "r": 30}))
        await ws.send_str(json.dumps({"t": "i", "d": command + "\n"}))

        async def read() -> None:
            nonlocal seen
            async for msg in ws:
                if msg.type in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    seen += msg.data.decode(errors="ignore") if isinstance(msg.data, bytes) else str(msg.data)
                    if marker in seen:
                        return
                else:
                    return

        try:
            await asyncio.wait_for(read(), timeout)
        except asyncio.TimeoutError:
            pass
    return seen


async def run(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, sat_id: str) -> None:
    await run_with(check, ui_connector(http, f"{ui}/api/sat/{sat_id}/terminal"))


async def run_with(check: Callable[[bool, str], None], connect, label: str = "terminal") -> None:

    seen = await _session(connect, "echo term-$((6*7))", "term-42")
    check("term-42" in seen, f"{label}: first session")

    # Reconnect immediately after closing (what the Reconnect button / tab switch does).
    for i in range(1, 4):
        t0 = time.monotonic()
        seen = await _session(connect, f"echo again-$((100+{i}))", f"again-{100 + i}")
        check(f"again-{100 + i}" in seen, f"{label}: reconnect #{i} works ({time.monotonic() - t0:.1f}s)")

    # The user types `exit`: the session ends, and a new one must still open.
    async with connect() as ws:
        await ws.send_str(json.dumps({"t": "i", "d": "exit\n"}))
        closed = False
        try:
            async def drain() -> None:
                async for _ in ws:
                    pass
            await asyncio.wait_for(drain(), 10)
            closed = True
        except asyncio.TimeoutError:
            closed = False
    check(closed, f"{label}: `exit` closes the session")
    seen = await _session(connect, "echo after-exit-$((7*7))", "after-exit-49")
    check("after-exit-49" in seen, f"{label}: new session after `exit`")

    # Two sessions at the same time (two browser tabs).
    a, b = await asyncio.gather(
        _session(connect, "echo tab-a-$((1+1))", "tab-a-2"),
        _session(connect, "echo tab-b-$((2+2))", "tab-b-4"),
    )
    check("tab-a-2" in a and "tab-b-4" in b, f"{label}: two concurrent sessions")

    # Long-idle session: connect, wait, then type.
    async with connect() as ws:
        await asyncio.sleep(3)
        await ws.send_str(json.dumps({"t": "i", "d": "echo idle-ok-$((5*5))\n"}))
        seen = ""
        try:
            async def read2() -> None:
                nonlocal seen
                async for msg in ws:
                    seen += msg.data.decode(errors="ignore") if isinstance(msg.data, bytes) else str(msg.data)
                    if "idle-ok-25" in seen:
                        return
            await asyncio.wait_for(read2(), 10)
        except asyncio.TimeoutError:
            pass
    check("idle-ok-25" in seen, f"{label}: input after idle period")
