"""Mutual-TLS client for talking to satellite agents."""

from __future__ import annotations

import io
import logging
import ssl
import tarfile
from pathlib import Path
from typing import Any

import aiohttp

from .store import Satellite

_LOGGER = logging.getLogger(__name__)


class AgentError(Exception):
    pass


def build_agent_bundle(src: Path) -> bytes:
    """Tar.gz of the agent package that gets installed on (or pushed to) satellites."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == "install.sh":
                continue
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(src)).replace("\\", "/"))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if path.is_file():
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    return buf.getvalue()


class AgentClient:
    def __init__(self, ssl_ctx: ssl.SSLContext) -> None:
        self._ssl = ssl_ctx
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl, limit=50))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    @staticmethod
    def base_url(sat: Satellite) -> str:
        host = f"[{sat.host}]" if ":" in sat.host else sat.host
        return f"https://{host}:{sat.port}"

    async def request(
        self,
        sat: Satellite,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: bytes | None = None,
        params: dict | None = None,
        timeout: float = 15,
    ) -> Any:
        assert self._session is not None
        url = self.base_url(sat) + path
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                data=data,
                params=params,
                server_hostname=sat.id,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                ctype = resp.headers.get("Content-Type", "")
                body = await (resp.json() if "json" in ctype else resp.text())
                if resp.status >= 400:
                    msg = body.get("error") if isinstance(body, dict) else body
                    raise AgentError(f"{resp.status}: {msg}")
                return body
        except aiohttp.ClientConnectorCertificateError as err:
            raise AgentError(f"certificate rejected: {err.certificate_error}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AgentError(str(err) or err.__class__.__name__) from err

    async def ws_connect(self, sat: Satellite, path: str) -> aiohttp.ClientWebSocketResponse:
        assert self._session is not None
        return await self._session.ws_connect(
            self.base_url(sat) + path, server_hostname=sat.id, heartbeat=30
        )
