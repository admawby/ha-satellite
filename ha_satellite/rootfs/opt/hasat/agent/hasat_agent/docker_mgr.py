"""Docker container management and scheduled image updates.

Talks to the Docker Engine API on /var/run/docker.sock (no docker CLI needed).

Update strategy (same idea as Watchtower, so Portainer/compose keep working):
  1. Check: compare the registry digest of each container's image reference
     (Engine /distribution endpoint, no pull) with the local image's RepoDigests.
  2. Update: pull the image; if it changed, recreate the container with its exact
     configuration (env, volumes, ports, networks, labels, restart policy, ...).
     Settings that merely came from the *old image* are dropped so the new image's
     defaults apply. On any failure the original container is restored.

Containers are never touched when they are excluded in settings, carry the label
``hasat.update=false`` (or Watchtower's ``com.centurylinklabs.watchtower.enable=false``),
use an image pinned by digest/ID, are ``--rm`` containers, or share their network
namespace with other containers.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import shutil
import struct
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .schedule import due_window
from .settings import CONTAINER_RE, STATE

_LOGGER = logging.getLogger(__name__)

DOCKER_HOST = os.environ.get("HASAT_DOCKER_HOST", "unix:///var/run/docker.sock")
DOCKER_CONFIG = Path(os.environ.get("HASAT_DOCKER_CONFIG", "/root/.docker/config.json"))
STATE_FILE = STATE / "docker.json"
INSTALL_SCRIPT_URL = os.environ.get("HASAT_DOCKER_INSTALL_URL", "https://get.docker.com")
OPT_OUT_LABELS = {"hasat.update": "false", "com.centurylinklabs.watchtower.enable": "false"}
# Config keys that may have been inherited from the image rather than set by the user.
IMAGE_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "ExposedPorts",
              "Volumes", "StopSignal", "Shell", "OnBuild", "ArgsEscaped")


class DockerError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- helpers
def split_ref(ref: str) -> Optional[Tuple[str, str]]:
    """'nginx' -> ('nginx', 'latest'); None for digest-pinned refs or bare image IDs."""
    if not ref or "@" in ref or ref.startswith("sha256:"):
        return None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def registry_of(repo: str) -> str:
    first = repo.split("/", 1)[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def registry_auth(repo: str) -> Optional[str]:
    """X-Registry-Auth header value from root's docker config.json (plain 'auth' entries only)."""
    try:
        auths = json.loads(DOCKER_CONFIG.read_text()).get("auths", {})
    except (OSError, ValueError):
        return None
    registry = registry_of(repo)
    for key, entry in auths.items():
        host = key.split("://", 1)[-1].split("/", 1)[0]
        if host == registry or (registry == "docker.io" and host in ("index.docker.io", "registry-1.docker.io")):
            if not entry.get("auth"):
                return None
            user, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
            payload = {"username": user, "password": password, "serveraddress": key}
            return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return None


def demux_logs(raw: bytes) -> str:
    """Docker multiplexes stdout/stderr with 8-byte frame headers when there is no TTY."""
    if len(raw) < 8 or raw[0] not in (0, 1, 2) or raw[1:4] != b"\x00\x00\x00":
        return raw.decode(errors="replace")
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode(errors="replace")


# -------------------------------------------------------------- API client
class DockerAPI:
    def __init__(self, host: str = DOCKER_HOST) -> None:
        self.host = host
        self._session: Optional[aiohttp.ClientSession] = None
        if host.startswith("unix://"):
            self.socket: Optional[str] = host[len("unix://"):]
            self.base = "http://docker"
        else:
            self.socket = None
            self.base = "http://" + host.split("://", 1)[-1]

    def available(self) -> bool:
        return self.socket is None or os.path.exists(self.socket)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.UnixConnector(path=self.socket) if self.socket else aiohttp.TCPConnector()
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def request(self, method: str, path: str, *, params: Optional[dict] = None, body: Any = None,
                      headers: Optional[dict] = None, timeout: float = 60, raw: bool = False) -> Any:
        try:
            async with self._get_session().request(
                method, self.base + path, params=params, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.read()
                if resp.status >= 400:
                    try:
                        msg = json.loads(data).get("message", "")
                    except ValueError:
                        msg = data.decode(errors="replace")
                    raise DockerError(resp.status, msg.strip() or resp.reason or "error")
                if raw:
                    return data
                if not data:
                    return None
                try:
                    return json.loads(data)
                except ValueError:
                    return data.decode(errors="replace")
        except aiohttp.ClientError as err:
            raise DockerError(0, f"cannot reach Docker: {err}") from err

    async def pull(self, repo: str, tag: str, log: Callable[[str], None], timeout: float = 1800) -> None:
        headers = {}
        auth = registry_auth(repo)
        if auth:
            headers["X-Registry-Auth"] = auth
        try:
            async with self._get_session().post(
                self.base + "/images/create", params={"fromImage": repo, "tag": tag}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    try:
                        text = json.loads(text).get("message", text)
                    except ValueError:
                        pass
                    raise DockerError(resp.status, text.strip())
                last = ""
                async for line in resp.content:
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("error"):
                        raise DockerError(0, evt["error"])
                    status = evt.get("status", "")
                    if status and status != last and not evt.get("progressDetail"):
                        if status.startswith(("Status:", "Digest:", "Pulling from")) or "complete" in status.lower():
                            log(f"  {status}")
                        last = status
        except aiohttp.ClientError as err:
            raise DockerError(0, f"pull failed: {err}") from err


# --------------------------------------------------------------- manager
class DockerManager:
    def __init__(self, get_settings: Callable[[], Dict[str, Any]], api: Optional[DockerAPI] = None) -> None:
        self._get_settings = get_settings
        self.api = api or DockerAPI()
        self._lock = asyncio.Lock()
        self.state: Dict[str, Any] = {
            "last_check": None, "last_update": None, "last_window": None,
            "last_result": None, "running": None, "results": {}, "log": [],
        }
        if STATE_FILE.exists():
            try:
                self.state.update(json.loads(STATE_FILE.read_text()))
            except ValueError:
                pass
        self.state["running"] = None

    # ------------------------------------------------------------ plumbing
    def _save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state))

    def _log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.state["log"].append(f"[{stamp}] {line}")
        del self.state["log"][:-400]
        _LOGGER.info("docker: %s", line)

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # -------------------------------------------------------------- install
    # Commands per install method; class attribute so tests can substitute them.
    INSTALL_COMMANDS: Dict[str, List[List[str]]] = {
        # Docker's official convenience script: Docker CE + buildx + compose plugin.
        "official": [
            ["sh", "-c", f"curl -fsSL {INSTALL_SCRIPT_URL} -o /tmp/hasat-get-docker.sh"],
            ["sh", "/tmp/hasat-get-docker.sh"],
            ["systemctl", "enable", "--now", "docker"],
        ],
        # Debian/Raspberry Pi OS packaged Docker.
        "debian": [
            ["apt-get", "update", "-q"],
            ["apt-get", "install", "-y", "-q", "docker.io"],
            ["systemctl", "enable", "--now", "docker"],
        ],
        # Installed but the daemon is not running.
        "start": [["systemctl", "enable", "--now", "docker"]],
    }

    INSTALL_WAIT = 30  # seconds to wait for the daemon socket after installing

    @staticmethod
    def installed() -> bool:
        return bool(shutil.which("dockerd") or shutil.which("docker"))

    async def _exec_stream(self, cmd: List[str], timeout: float = 1800) -> int:
        self._log("$ " + " ".join(cmd))
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        except OSError as err:
            self._log(f"  cannot run {cmd[0]}: {err}")
            return 127

        async def pump() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._log("  " + line)

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            self._log("  timed out")
            return -1
        return proc.returncode or 0

    async def install(self, method: str) -> Dict[str, Any]:
        if method not in self.INSTALL_COMMANDS:
            raise ValueError("method must be one of: " + ", ".join(self.INSTALL_COMMANDS))
        if self.busy:
            raise RuntimeError("a Docker job is already running")
        if method != "start" and self.api.available():
            raise RuntimeError("Docker is already installed and running")
        async with self._lock:
            self.state["running"] = "install"
            self.state["log"] = []
            try:
                self._log(f"Installing Docker ({method}) ...")
                for cmd in self.INSTALL_COMMANDS[method]:
                    rc = await self._exec_stream(cmd)
                    if rc != 0:
                        self.state["last_result"] = f"install failed ({' '.join(cmd[:2])} exited {rc})"
                        self._log(self.state["last_result"])
                        return self.status()
                for _ in range(self.INSTALL_WAIT):  # wait for the daemon socket
                    if self.api.available():
                        break
                    await asyncio.sleep(1)
                if self.api.available():
                    try:
                        ver = await self.api.request("GET", "/version")
                        self.state["last_result"] = f"Docker {ver.get('Version', '?')} installed and running"
                    except DockerError as err:
                        self.state["last_result"] = f"installed, but the daemon does not answer yet ({err.message})"
                else:
                    self.state["last_result"] = "installed, but /var/run/docker.sock did not appear"
                self._log(self.state["last_result"])
            finally:
                self.state["running"] = None
                self._save()
        return self.status()

    # -------------------------------------------------------------- queries
    async def containers(self) -> List[Dict[str, Any]]:
        raw = await self.api.request("GET", "/containers/json", params={"all": "1"})
        cfg = self._get_settings()["docker_update"]
        results = self.state.get("results", {})
        items = []
        for c in raw:
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            labels = c.get("Labels") or {}
            skip = self._skip_reason(name, c.get("Image", ""), labels, cfg)
            res = results.get(name, {})
            items.append({
                "id": c["Id"][:12],
                "name": name,
                "image": c.get("Image"),
                "image_id": (c.get("ImageID") or "")[7:19],
                "state": c.get("State"),
                "status": c.get("Status"),
                "created": c.get("Created"),
                "ports": sorted({
                    f"{p['PublicPort']}->{p['PrivatePort']}/{p['Type']}" if p.get("PublicPort")
                    else f"{p['PrivatePort']}/{p['Type']}"
                    for p in c.get("Ports") or []
                }),
                "project": labels.get("com.docker.compose.project"),
                "auto_update": skip is None,
                "skip_reason": skip,
                "update_available": bool(res.get("update_available")) and res.get("image") == c.get("Image"),
                "check_error": res.get("error"),
                "checked": res.get("checked"),
            })
        return sorted(items, key=lambda x: x["name"])

    async def summary(self) -> Dict[str, Any]:
        if not self.api.available():
            return {"available": False}
        try:
            items = await self.containers()
        except DockerError as err:
            return {"available": False, "error": str(err)}
        return {
            "available": True,
            "total": len(items),
            "running": sum(1 for c in items if c["state"] == "running"),
            "updates_available": sum(1 for c in items if c["update_available"]),
            "last_check": self.state.get("last_check"),
            "last_update": self.state.get("last_update"),
            "running_job": self.state.get("running"),
        }

    def status(self) -> Dict[str, Any]:
        data = {k: v for k, v in self.state.items() if k != "log"}
        data["log"] = self.state["log"][-300:]
        return data

    @staticmethod
    def _skip_reason(name: str, ref: str, labels: Dict[str, str], cfg: Dict[str, Any]) -> Optional[str]:
        if name in cfg.get("exclude", []):
            return "excluded in settings"
        for key, val in OPT_OUT_LABELS.items():
            if str(labels.get(key, "")).lower() == val:
                return f"label {key}={val}"
        if split_ref(ref) is None:
            return "image pinned by digest or ID"
        return None

    async def logs(self, name: str, lines: int = 200) -> str:
        self._check_name(name)
        info = await self.api.request("GET", f"/containers/{name}/json")
        raw = await self.api.request(
            "GET", f"/containers/{name}/logs",
            params={"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(lines)}, raw=True,
        )
        return raw.decode(errors="replace") if info["Config"].get("Tty") else demux_logs(raw)

    async def action(self, name: str, action: str) -> None:
        self._check_name(name)
        if action not in ("start", "stop", "restart"):
            raise ValueError("unsupported action")
        try:
            await self.api.request("POST", f"/containers/{name}/{action}", timeout=120)
        except DockerError as err:
            if err.status != 304:  # already started/stopped
                raise

    @staticmethod
    def _check_name(name: str) -> None:
        if not CONTAINER_RE.match(name):
            raise ValueError("invalid container name")

    # --------------------------------------------------------------- check
    async def _remote_digest(self, ref: str) -> str:
        headers = {}
        parts = split_ref(ref)
        auth = registry_auth(parts[0]) if parts else None
        if auth:
            headers["X-Registry-Auth"] = auth
        desc = await self.api.request("GET", f"/distribution/{quote(ref, safe='/:')}/json", headers=headers, timeout=60)
        return desc["Descriptor"]["digest"]

    async def _check_image(self, ref: str, container_image_ids: List[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"image": ref, "checked": time.time(), "update_available": False}
        try:
            local = await self.api.request("GET", f"/images/{quote(ref, safe='/:')}/json")
        except DockerError as err:
            if err.status != 404:
                raise
            local = None
        if local and any(cid != local["Id"] for cid in container_image_ids):
            # A newer image was already pulled; the container still runs the old one.
            result["update_available"] = True
            result["reason"] = "newer image already pulled"
            return result
        local_digests = {d.split("@", 1)[1] for d in (local or {}).get("RepoDigests") or [] if "@" in d}
        if local and not local_digests:
            result["reason"] = "locally built image (no registry digest)"
            return result
        remote = await self._remote_digest(ref)
        result["remote_digest"] = remote
        result["update_available"] = remote not in local_digests
        return result

    async def check(self) -> Dict[str, Any]:
        if self.busy:
            return self.status()
        async with self._lock:
            self.state["running"] = "check"
            self.state["log"] = []
            await self._check_locked()
            self.state["running"] = None
            self._save()
        return self.status()

    async def _check_locked(self) -> None:
        self._log("Checking registries for newer images ...")
        raw = await self.api.request("GET", "/containers/json", params={"all": "1"})
        by_ref: Dict[str, List[Dict[str, Any]]] = {}
        for c in raw:
            if split_ref(c.get("Image", "")):
                by_ref.setdefault(c["Image"], []).append(c)
        results: Dict[str, Any] = {}
        found = 0
        for ref, conts in sorted(by_ref.items()):
            try:
                res = await self._check_image(ref, [c.get("ImageID", "") for c in conts])
            except DockerError as err:
                res = {"image": ref, "checked": time.time(), "update_available": False, "error": err.message}
                self._log(f"{ref}: check failed ({err.message})")
            else:
                if res["update_available"]:
                    found += 1
                    self._log(f"{ref}: update available")
            for c in conts:
                results[c["Names"][0].lstrip("/")] = res
        self.state["results"] = results
        self.state["last_check"] = time.time()
        self.state["last_result"] = f"{found} image(s) with updates"
        self._log(f"Check finished: {found} image(s) with updates")

    # --------------------------------------------------------------- update
    async def update(self, names: Optional[List[str]] = None) -> Dict[str, Any]:
        """Update the given containers, or every eligible container with an available update."""
        if self.busy:
            raise RuntimeError("a Docker update is already running")
        async with self._lock:
            self.state["running"] = "update"
            self.state["log"] = []
            try:
                await self._update_locked(names)
            finally:
                self.state["running"] = None
                self.state["last_update"] = time.time()
                self._save()
        return self.status()

    async def _update_locked(self, names: Optional[List[str]]) -> None:
        cfg = self._get_settings()["docker_update"]
        if names is None:
            await self._check_locked()
            items = await self.containers()
            names = [c["name"] for c in items if c["update_available"] and c["auto_update"]]
            if not names:
                self._log("All containers are up to date.")
                self.state["last_result"] = "nothing to update"
                return
        ok, failed = [], []
        for name in names:
            try:
                if await self._update_container(name, prune=cfg.get("prune", True)):
                    ok.append(name)
            except (DockerError, RuntimeError, ValueError) as err:
                failed.append(name)
                self._log(f"{name}: FAILED - {err}")
        # Refresh results so the UI shows the new state.
        await self._check_locked()
        summary = f"updated {len(ok)}" + (f", failed {len(failed)}: {', '.join(failed)}" if failed else "")
        self.state["last_result"] = summary
        self._log(f"Update finished: {summary}")

    async def _update_container(self, name: str, prune: bool) -> bool:
        self._check_name(name)
        info = await self.api.request("GET", f"/containers/{name}/json")
        ref = info["Config"]["Image"]
        parts = split_ref(ref)
        if parts is None:
            raise ValueError(f"image {ref} is pinned; not updating")
        if info["HostConfig"].get("AutoRemove"):
            raise ValueError("--rm containers cannot be recreated")
        dependants = await self._network_dependants(info)
        if dependants:
            raise ValueError(f"shares its network with {', '.join(dependants)}; update those together manually")

        self._log(f"{name}: pulling {ref}")
        await self.api.pull(parts[0], parts[1], self._log)
        new_image = await self.api.request("GET", f"/images/{quote(ref, safe='/:')}/json")
        old_image_id = info["Image"]
        if new_image["Id"] == old_image_id:
            self._log(f"{name}: already running the latest image")
            return False
        try:
            old_image = await self.api.request("GET", f"/images/{old_image_id}/json")
        except DockerError:
            old_image = {}
        await self._recreate(info, ref, (old_image or {}).get("Config") or {})
        self._log(f"{name}: updated {old_image_id[7:19]} -> {new_image['Id'][7:19]}")
        if prune:
            try:
                await self.api.request("DELETE", f"/images/{old_image_id}")
                self._log(f"{name}: removed old image {old_image_id[7:19]}")
            except DockerError:
                pass  # still used by another container or tag
        return True

    async def _network_dependants(self, info: Dict[str, Any]) -> List[str]:
        name = info["Name"].lstrip("/")
        targets = {f"container:{info['Id']}", f"container:{name}", f"container:{info['Id'][:12]}"}
        raw = await self.api.request("GET", "/containers/json", params={"all": "1"})
        out = []
        for c in raw:
            if c["Id"] == info["Id"]:
                continue
            mode = (c.get("HostConfig") or {}).get("NetworkMode", "")
            if mode in targets:
                out.append(c["Names"][0].lstrip("/"))
        return out

    @staticmethod
    def build_create_body(info: Dict[str, Any], ref: str, image_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Container create body reproducing ``info``; returns (body, extra_networks)."""
        cfg = copy.deepcopy(info["Config"])
        cfg["Image"] = ref
        for key in IMAGE_KEYS:
            if key in cfg and image_cfg.get(key) is not None and cfg[key] == image_cfg.get(key):
                cfg.pop(key)
        image_env = set(image_cfg.get("Env") or [])
        cfg["Env"] = [e for e in cfg.get("Env") or [] if e not in image_env]
        image_labels = image_cfg.get("Labels") or {}
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if image_labels.get(k) != v}
        if cfg.get("Hostname") == info["Id"][:12]:
            cfg.pop("Hostname")  # auto-generated from the old container id

        host = copy.deepcopy(info["HostConfig"])
        mode = host.get("NetworkMode", "default")
        networks = copy.deepcopy((info.get("NetworkSettings") or {}).get("Networks") or {})
        short_id = info["Id"][:12]

        def endpoint(ep: Dict[str, Any]) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            aliases = [a for a in ep.get("Aliases") or [] if a != short_id]
            if aliases:
                out["Aliases"] = aliases
            for key in ("IPAMConfig", "Links", "DriverOpts", "MacAddress"):
                if ep.get(key):
                    out[key] = ep[key]
            return out

        body = dict(cfg)
        body["HostConfig"] = host
        extra: Dict[str, Any] = {}
        if networks and not (mode in ("host", "none") or mode.startswith("container:")):
            primary = mode if mode in networks else ("bridge" if mode == "default" and "bridge" in networks
                                                     else next(iter(networks)))
            body["NetworkingConfig"] = {"EndpointsConfig": {primary: endpoint(networks[primary])}}
            extra = {n: endpoint(ep) for n, ep in networks.items() if n != primary}
        return body, extra

    async def _recreate(self, info: Dict[str, Any], ref: str, image_cfg: Dict[str, Any]) -> None:
        name = info["Name"].lstrip("/")
        old_id = info["Id"]
        was_running = bool(info["State"].get("Running"))
        body, extra_networks = self.build_create_body(info, ref, image_cfg)
        stop_timeout = info["Config"].get("StopTimeout") or 10
        backup_name = f"{name}-hasat-old"

        self._log(f"{name}: recreating container")
        if was_running:
            try:
                await self.api.request("POST", f"/containers/{old_id}/stop", params={"t": str(stop_timeout)},
                                       timeout=stop_timeout + 60)
            except DockerError as err:
                if err.status != 304:
                    raise
        await self.api.request("POST", f"/containers/{old_id}/rename", params={"name": backup_name})
        new_id = None
        try:
            created = await self.api.request("POST", "/containers/create", params={"name": name}, body=body)
            new_id = created["Id"]
            for net, ep in extra_networks.items():
                await self.api.request("POST", f"/networks/{quote(net, safe='')}/connect",
                                       body={"Container": new_id, "EndpointConfig": ep})
            if was_running:
                await self.api.request("POST", f"/containers/{new_id}/start", timeout=120)
        except Exception as err:
            self._log(f"{name}: recreate failed ({err}); restoring the original container")
            if new_id:
                try:
                    await self.api.request("DELETE", f"/containers/{new_id}", params={"force": "1"})
                except DockerError:
                    pass
            await self.api.request("POST", f"/containers/{old_id}/rename", params={"name": name})
            if was_running:
                await self.api.request("POST", f"/containers/{old_id}/start")
            raise
        await self.api.request("DELETE", f"/containers/{old_id}")

    # ------------------------------------------------------------ scheduler
    async def scheduler(self) -> None:
        await asyncio.sleep(90)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                _LOGGER.exception("Docker scheduler error")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        if not self.api.available():
            return
        cfg = self._get_settings()["docker_update"]
        today = due_window(cfg, self.state.get("last_window"))
        if not today or self.busy:
            return
        self.state["last_window"] = today
        self._save()
        if cfg.get("mode") == "check":
            _LOGGER.info("Scheduled container image check")
            await self.check()
        else:
            _LOGGER.info("Scheduled container image update")
            await self.update()
