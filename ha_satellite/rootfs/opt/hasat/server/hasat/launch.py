"""Launch a satellite over SSH: log in once with the user's credentials and run the installer.

Credentials are used for this single session only and are never stored or logged.
The installer itself is fetched by the Pi from the enrollment endpoint with a
pinned public key, exactly like the manual one-liner.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shlex
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncssh

if TYPE_CHECKING:
    from .manager import Manager

_LOGGER = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    host: str
    status: str = "running"  # running | success | failed
    log: list[str] = field(default_factory=list)
    satellite_id: str | None = None
    host_key: str = ""
    started: float = field(default_factory=time.time)

    def write(self, line: str) -> None:
        self.log.append(line.rstrip("\n"))
        del self.log[:-500]

    def to_api(self) -> dict:
        return {
            "id": self.id, "host": self.host, "status": self.status, "log": self.log,
            "satellite_id": self.satellite_id, "host_key": self.host_key,
        }


class Launcher:
    def __init__(self, mgr: "Manager") -> None:
        self.mgr = mgr
        self.jobs: dict[str, Job] = {}
        self.token_jobs: dict[str, Job] = {}

    def start(self, host: str, port: int, username: str, password: str, private_key: str, name: str) -> Job:
        job = Job(id=secrets.token_hex(6), host=host)
        self.jobs[job.id] = job
        for old in [j for j in self.jobs.values() if time.time() - j.started > 3600]:
            self.jobs.pop(old.id, None)
        asyncio.create_task(self._run(job, host, port, username, password, private_key, name))
        return job

    def enrolled(self, token: str, sat_id: str) -> None:
        job = self.token_jobs.pop(token, None)
        if job:
            job.satellite_id = sat_id

    async def _run(self, job: Job, host: str, port: int, username: str, password: str, private_key: str, name: str) -> None:
        try:
            addrs = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ip = addrs[0][4][0]
            if not self.mgr.options.address_allowed(ip):
                raise RuntimeError(f"{host} ({ip}) is not inside allowed_networks; refusing to launch a satellite there")

            tok = self.mgr.store.new_token(name)
            self.token_jobs[tok.token] = job
            inner = self.mgr.install_command(tok.token, sudo=False)

            keys = []
            if private_key.strip():
                keys.append(asyncssh.import_private_key(private_key.strip(), password or None))

            job.write(f"Connecting to {username}@{host}:{port} ...")
            async with asyncssh.connect(
                ip, port=port, username=username,
                password=password or None, client_keys=keys or None,
                known_hosts=None,  # first contact: we display the fingerprint instead
                connect_timeout=20, agent_path=None,
            ) as conn:
                hk = conn.get_server_host_key()
                if hk:
                    job.host_key = hk.get_fingerprint("sha256")
                    job.write(f"SSH host key: {job.host_key}")

                if username == "root":
                    command = f"bash -c {shlex.quote(inner)}"
                    stdin = None
                else:
                    command = f"sudo -S -p '' bash -c {shlex.quote(inner)}"
                    stdin = (password + "\n") if password else None

                job.write("Running satellite installer (this can take a few minutes) ...")
                proc = await conn.create_process(command, stderr=asyncssh.STDOUT)
                if stdin:
                    proc.stdin.write(stdin)
                proc.stdin.write_eof()
                async for line in proc.stdout:
                    job.write(line)
                await proc.wait()
                if proc.exit_status != 0:
                    raise RuntimeError(f"installer exited with status {proc.exit_status}")

            if job.satellite_id:
                job.write("Satellite launched successfully.")
                job.status = "success"
                await self.mgr.poll_one(job.satellite_id)
            else:
                raise RuntimeError("installer finished but the satellite never enrolled")
        except (asyncssh.PermissionDenied,) as err:
            job.write(f"ERROR: authentication failed ({err.reason})")
            job.status = "failed"
        except Exception as err:  # noqa: BLE001 - surface every failure to the UI
            _LOGGER.warning("Launching a satellite on %s failed: %s", host, err)
            job.write(f"ERROR: {err}")
            job.status = "failed"
        finally:
            password = ""  # noqa: F841 - drop the reference as early as possible
