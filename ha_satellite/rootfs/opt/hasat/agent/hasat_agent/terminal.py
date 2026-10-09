"""Interactive shell over a WebSocket, backed by a real PTY.

Protocol (browser -> agent, text frames, JSON):
    {"t": "i", "d": "<keystrokes>"}      input
    {"t": "r", "c": <cols>, "r": <rows>}  resize
Agent -> browser: binary frames with raw terminal output.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import pwd
import signal
import struct
import termios

from aiohttp import WSMsgType, web

_LOGGER = logging.getLogger(__name__)


def _spawn(user: str):
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        pw = pwd.getpwnam("root")
    pid, fd = pty.fork()
    if pid == 0:  # child
        env = {
            "TERM": "xterm-256color",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": pw.pw_dir,
            "USER": pw.pw_name,
            "LOGNAME": pw.pw_name,
            "SHELL": pw.pw_shell or "/bin/bash",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        try:
            os.chdir(pw.pw_dir)
        except OSError:
            os.chdir("/")
        if pw.pw_uid == 0:
            os.execvpe(env["SHELL"], [env["SHELL"], "-l"], env)
        else:
            os.execvpe("su", ["su", "-", pw.pw_name], env)
        os._exit(1)
    return pid, fd


def _resize(fd: int, cols: int, rows: int) -> None:
    cols = max(10, min(int(cols), 500))
    rows = max(5, min(int(rows), 200))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _reap(pid: int) -> None:
    """Hang up the shell's process group and collect it, escalating to SIGKILL."""
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(20):
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if done:
                return
            await asyncio.sleep(0.1)


async def handle(request: web.Request, user: str) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    pid, fd = _spawn(user)
    os.set_blocking(fd, False)
    _LOGGER.info("Terminal opened (user=%s, pid=%s)", user, pid)
    out_q: asyncio.Queue = asyncio.Queue()

    def readable() -> None:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        out_q.put_nowait(data)
        if not data:
            loop.remove_reader(fd)

    loop.add_reader(fd, readable)

    async def pump_out() -> None:
        while True:
            data = await out_q.get()
            if not data:
                break
            await ws.send_bytes(data)

    async def pump_in() -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except ValueError:
                continue
            if payload.get("t") == "i":
                data = str(payload.get("d", "")).encode()
                while data:
                    try:
                        n = os.write(fd, data)
                        data = data[n:]
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
            elif payload.get("t") == "r":
                _resize(fd, payload.get("c", 80), payload.get("r", 24))

    tasks = [asyncio.ensure_future(pump_out()), asyncio.ensure_future(pump_in())]
    try:
        # Ends when either the shell exits or the browser disconnects.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        loop.remove_reader(fd)
        await _reap(pid)
        os.close(fd)
        await ws.close()
        _LOGGER.info("Terminal closed (pid=%s)", pid)
    return ws
