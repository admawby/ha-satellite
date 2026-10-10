"""Agent HTTPS API. Every request must present the manager's CA-signed client certificate."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import ssl
import subprocess
import tarfile
from typing import Any, Dict, Optional

from aiohttp import web

from . import __version__, cleanup, files, firewall, metrics, mode, serial_bridge, storage, terminal, usb
from .settings import (CA_FILE, CERT_FILE, INSTALL_DIR, KEY_FILE, SYSTEMD_DIR, load_config, load_settings,
                       merge_settings, save_settings)
from .docker_mgr import DockerError, DockerManager
from .updates import Updater

_LOGGER = logging.getLogger(__name__)
_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]{1,64}$")


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    ctx.load_verify_locations(cafile=str(CA_FILE))
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS: no client cert, no connection
    return ctx


def _peer_cn(request: web.Request) -> str:
    cert = request.transport.get_extra_info("peercert") if request.transport else None
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


def _delayed(cmd: list, delay: float = 2.0) -> None:
    """Run a system command shortly after the HTTP response has been sent."""
    asyncio.get_running_loop().call_later(delay, lambda: subprocess.Popen(cmd, start_new_session=True))


class Agent:
    def __init__(self) -> None:
        self.config: Dict[str, Any] = load_config()
        self.settings: Dict[str, Any] = load_settings()
        self.updater = Updater(lambda: self.settings, self.reboot)
        self.docker = DockerManager(lambda: self.settings)
        self.errors: Dict[str, str] = {}

    # -------------------------------------------------------------- helpers
    def restart(self) -> None:
        """Exit; systemd (Restart=always) brings the agent back up on the new code."""
        os._exit(0)

    def reboot(self) -> None:
        subprocess.Popen(["systemctl", "reboot"], start_new_session=True)

    def apply_network(self) -> None:
        """(Re)write ser2net and firewall state from settings."""
        if mode.container_mode():
            return  # appliance OS (e.g. Synology DSM): no ser2net / nftables changes
        bridges = self.settings.get("serial", [])
        err = serial_bridge.apply(bridges)
        self._set_error("serial", err)
        ports = [self.config["agent_port"]] + [b["port"] for b in bridges]
        err = firewall.apply(self.settings.get("firewall_enabled", True), self.config.get("trusted_ips", []), ports)
        self._set_error("firewall", err)

    def _set_error(self, key: str, err: Any) -> None:
        if err:
            self.errors[key] = str(err)
        else:
            self.errors.pop(key, None)

    def status(self) -> Dict[str, Any]:
        data = metrics.collect()
        feats = mode.features()
        data.update(
            agent_version=__version__,
            satellite_id=self.config.get("id"),
            mode=mode.mode_name(),
            features=feats,
            updates=dict(self.updater.status(), log=[]) if "updates" in feats else {},  # full log via /api/updates
            serial=serial_bridge.status(self.settings.get("serial", [])) if "radios" in feats else [],
            settings={k: v for k, v in self.settings.items() if k != "serial"},
            radios=usb.serial_devices() if "radios" in feats else [],
            errors=self.errors,
        )
        return data

    # ------------------------------------------------------------- handlers
    async def h_status(self, _r: web.Request) -> web.Response:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, self.status)
        data["docker"] = await self.docker.summary()
        return web.json_response(data)

    # ------------------------------------------------------- files / storage
    async def _blocking(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def h_storage(self, _r: web.Request) -> web.Response:
        return web.json_response(await self._blocking(storage.summary))

    async def h_storage_mount(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(await self._blocking(storage.mount, str(body.get("device", "")), bool(body.get("read_only"))))

    async def h_storage_unmount(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(await self._blocking(storage.unmount, str(body.get("mountpoint", ""))))

    async def h_files_list(self, request: web.Request) -> web.Response:
        data = await self._blocking(files.list_dir, request.query.get("path", "/"))
        data["quick_links"] = files.quick_links()
        return web.json_response(data)

    async def h_files_read(self, request: web.Request) -> web.Response:
        return web.json_response(await self._blocking(files.read_text, request.query.get("path", "")))

    async def h_files_write(self, request: web.Request) -> web.Response:
        body = await request.json()
        result = await self._blocking(
            files.write_text, str(body.get("path", "")), str(body.get("content", "")),
            body.get("expect_mtime"), bool(body.get("create")),
        )
        return web.json_response(result)

    async def h_files_op(self, request: web.Request) -> web.Response:
        body = await request.json()
        op = request.match_info["op"]
        if op == "mkdir":
            result = await self._blocking(files.mkdir, str(body.get("path", "")))
        elif op == "rename":
            result = await self._blocking(files.rename, str(body.get("from", "")), str(body.get("to", "")))
        elif op == "delete":
            result = await self._blocking(files.delete, str(body.get("path", "")), bool(body.get("recursive")))
        else:
            raise web.HTTPNotFound()
        return web.json_response(result)

    async def h_files_download(self, request: web.Request) -> web.StreamResponse:
        path = files.download_source(request.query.get("path", ""))
        name = os.path.basename(path).replace('"', "")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def h_files_upload(self, request: web.Request) -> web.Response:
        target = files.upload_target(request.query.get("path", ""), request.query.get("overwrite") == "1")
        tmp = os.path.join(os.path.dirname(target), f".hasat-upload-{os.getpid()}-{id(request)}")
        size = 0
        try:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = await request.content.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        _LOGGER.info("Uploaded %s (%d bytes)", target, size)
        return web.json_response({"path": target, "size": size})

    # --------------------------------------------------------------- docker
    def _docker_job(self, coro) -> None:
        if self.docker.busy:
            coro.close()
            raise RuntimeError("a Docker job is already running")

        async def run() -> None:
            try:
                await coro
            except Exception as err:  # noqa: BLE001 - surfaced through the job log
                self.docker._log(f"ERROR: {err}")
                _LOGGER.exception("Docker job failed")

        asyncio.ensure_future(run())

    async def h_docker(self, _r: web.Request) -> web.Response:
        if not self.docker.api.available():
            return web.json_response({
                "available": False,
                "installed": self.docker.installed(),
                "job": self.docker.status(),
                "settings": self.settings["docker_update"],
            })
        return web.json_response({
            "available": True,
            "containers": await self.docker.containers(),
            "job": self.docker.status(),
            "settings": self.settings["docker_update"],
        })

    async def h_docker_install(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        method = str((body or {}).get("method", "official"))
        if method not in self.docker.INSTALL_COMMANDS:
            raise ValueError("unknown install method")
        if method != "start" and self.docker.api.available():
            raise RuntimeError("Docker is already installed and running")
        self._docker_job(self.docker.install(method))
        await asyncio.sleep(0.2)
        return web.json_response(self.docker.status())

    async def h_docker_check(self, _r: web.Request) -> web.Response:
        self._docker_job(self.docker.check())
        await asyncio.sleep(0.2)
        return web.json_response(self.docker.status())

    async def h_docker_update(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        names = (body or {}).get("names")
        if names is not None and not isinstance(names, list):
            raise ValueError("names must be a list")
        self._docker_job(self.docker.update([str(n) for n in names] if names else None))
        await asyncio.sleep(0.2)
        return web.json_response(self.docker.status())

    async def h_docker_container(self, request: web.Request) -> web.Response:
        name, action = request.match_info["name"], request.match_info["action"]
        if action == "update":
            self.docker._check_name(name)
            self._docker_job(self.docker.update([name]))
            await asyncio.sleep(0.2)
            return web.json_response(self.docker.status())
        await self.docker.action(name, action)
        return web.json_response({"ok": True})

    async def h_docker_logs(self, request: web.Request) -> web.Response:
        lines = max(10, min(int(request.query.get("lines", "300")), 5000))
        return web.json_response({"output": await self.docker.logs(request.match_info["name"], lines)})

    async def h_usb(self, _r: web.Request) -> web.Response:
        return web.json_response({"serial": usb.serial_devices(), "usb": usb.usb_tree()})

    async def h_serial_get(self, _r: web.Request) -> web.Response:
        return web.json_response(serial_bridge.status(self.settings.get("serial", [])))

    async def h_serial_put(self, request: web.Request) -> web.Response:
        body = await request.json()
        bridges = serial_bridge.validate(body.get("bridges", []), self.config["agent_port"])
        self.settings["serial"] = bridges
        save_settings(self.settings)
        await asyncio.get_running_loop().run_in_executor(None, self.apply_network)
        await asyncio.sleep(1)
        return web.json_response({"bridges": serial_bridge.status(bridges), "errors": self.errors})

    async def h_settings_get(self, _r: web.Request) -> web.Response:
        return web.json_response(self.settings)

    async def h_settings_put(self, request: web.Request) -> web.Response:
        patch = await request.json()
        self.settings = merge_settings(self.settings, patch)
        save_settings(self.settings)
        if "firewall_enabled" in patch:
            await asyncio.get_running_loop().run_in_executor(None, self.apply_network)
        return web.json_response(self.settings)

    async def h_updates(self, _r: web.Request) -> web.Response:
        return web.json_response(self.updater.status())

    async def h_updates_check(self, _r: web.Request) -> web.Response:
        return web.json_response(await self.updater.check())

    async def h_updates_apply(self, _r: web.Request) -> web.Response:
        # Long-running: start in the background, the UI polls /api/updates.
        if self.updater.state.get("running"):
            return web.json_response({"error": "update already running"}, status=409)
        asyncio.ensure_future(self.updater.apply())
        await asyncio.sleep(0.2)
        return web.json_response(self.updater.status())

    async def h_reboot(self, _r: web.Request) -> web.Response:
        _LOGGER.warning("Reboot requested by manager")
        _delayed(["systemctl", "reboot"])
        return web.json_response({"ok": True})

    async def h_shutdown(self, _r: web.Request) -> web.Response:
        _LOGGER.warning("Shutdown requested by manager")
        _delayed(["systemctl", "poweroff"])
        return web.json_response({"ok": True})

    async def h_service(self, request: web.Request) -> web.Response:
        unit = request.match_info["unit"]
        action = request.match_info["action"]
        if not _UNIT_RE.match(unit) or action not in ("restart", "start", "stop"):
            raise web.HTTPBadRequest()
        if unit.startswith("hasat-agent"):
            _delayed(["systemctl", action, unit])
            return web.json_response({"ok": True, "deferred": True})
        proc = await asyncio.create_subprocess_exec(
            "systemctl", action, unit, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return web.json_response({"ok": proc.returncode == 0, "output": out.decode(errors="replace")})

    async def h_exec(self, request: web.Request) -> web.Response:
        body = await request.json()
        command = str(body.get("command", ""))
        timeout = max(1, min(int(body.get("timeout", 60)), 900))
        if not command.strip():
            raise web.HTTPBadRequest(text="command required")
        _LOGGER.info("exec: %s", command)
        shell = ["/bin/sh", "-lc", command] if mode.container_mode() else ["/bin/bash", "-lc", command]
        proc = await asyncio.create_subprocess_exec(
            *mode.host_cmd(shell),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            rc = proc.returncode
        except asyncio.TimeoutError:
            os.killpg(proc.pid, 9)
            out, rc = b"\n[timed out]", -1
        text = out.decode(errors="replace")
        if len(text) > 256 * 1024:
            text = "[output truncated]\n" + text[-256 * 1024:]
        return web.json_response({"rc": rc, "output": text})

    async def h_logs(self, request: web.Request) -> web.Response:
        unit = request.query.get("unit", "")
        lines = max(10, min(int(request.query.get("lines", "200")), 5000))
        cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
        if unit:
            if not _UNIT_RE.match(unit):
                raise web.HTTPBadRequest()
            cmd += ["-u", unit]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return web.json_response({"output": out.decode(errors="replace")})

    async def h_terminal(self, request: web.Request) -> web.WebSocketResponse:
        return await terminal.handle(request, self.settings.get("terminal_user", "root"), mode.container_mode())

    async def h_agent_update(self, request: web.Request) -> web.Response:
        blob = await request.read()
        staging = INSTALL_DIR.with_name("hasat-agent.new")
        backup = INSTALL_DIR.with_name("hasat-agent.old")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                target = (staging / member.name).resolve()
                if not str(target).startswith(str(staging.resolve())) or member.issym() or member.islnk():
                    raise web.HTTPBadRequest(text=f"unsafe archive member {member.name}")
            tar.extractall(staging)
        if not (staging / "hasat_agent" / "__init__.py").exists():
            raise web.HTTPBadRequest(text="archive does not contain the agent")
        shutil.rmtree(backup, ignore_errors=True)
        if INSTALL_DIR.exists():
            INSTALL_DIR.rename(backup)
        staging.rename(INSTALL_DIR)
        unit_src = INSTALL_DIR / "hasat-agent.service"
        unit_dst = SYSTEMD_DIR / "hasat-agent.service"
        if unit_dst.parent.is_dir() and unit_src.exists() and (
            not unit_dst.exists() or unit_src.read_bytes() != unit_dst.read_bytes()
        ):
            shutil.copy2(unit_src, unit_dst)
            try:
                subprocess.run(["systemctl", "daemon-reload"], timeout=30)
            except OSError as err:
                _LOGGER.warning("daemon-reload failed: %s", err)
        _LOGGER.warning("Agent updated to %s; restarting", request.query.get("version", "?"))
        asyncio.get_running_loop().call_later(1.5, self.restart)
        return web.json_response({"ok": True, "restarting": True})

    async def _host_base(self) -> Optional[str]:
        """Host path of the agent's data folder (container mode)."""
        base = os.environ.get("HASAT_HOST_BASE") or cleanup.load_record().get("host_base")
        name = mode.own_container()
        if not base and name:
            try:
                info = await self.docker.api.request("GET", f"/containers/{name}/json")
                base = next((m["Source"] for m in info.get("Mounts", []) if m.get("Destination") == "/opt/hasat"), None)
            except DockerError:
                base = None
        return base

    async def h_uninstall_plan(self, request: web.Request) -> web.Response:
        remove_docker = request.query.get("remove_docker") == "1"
        return web.json_response(cleanup.plan(mode.container_mode(), remove_docker, await self._host_base()))

    def launch_cleanup(self, script: str) -> None:
        """Run the final cleanup as a transient systemd unit on the host, so it outlives the agent."""
        cmd = mode.host_cmd(["systemd-run", "--unit", "hasat-uninstall", "--collect", "--quiet", "/bin/sh", "-c", script])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"could not start cleanup: {proc.stderr.strip() or proc.returncode}")

    async def h_uninstall(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        remove_docker = bool((body or {}).get("remove_docker"))
        container = mode.container_mode()
        host_base = await self._host_base() if container else None
        report = cleanup.plan(container, remove_docker, host_base)
        _LOGGER.warning("Removing HA Satellite from this device: %s", "; ".join(report["remove"]))
        if container:
            script = cleanup.container_script(mode.own_container() or "hasat-agent", host_base)
        else:
            script = cleanup.host_script(remove_docker)
        await asyncio.get_running_loop().run_in_executor(None, self.launch_cleanup, script)
        return web.json_response(dict(report, ok=True))

    # ------------------------------------------------------------------ app
    def build_app(self) -> web.Application:
        controller_cn = self.config.get("controller_cn", "hasat-controller")

        @web.middleware
        async def auth(request: web.Request, handler):
            cn = _peer_cn(request)
            if cn != controller_cn:
                _LOGGER.warning("Rejected request from %s with certificate CN=%r", request.remote, cn)
                raise web.HTTPForbidden()
            feature = mode.feature_for(request.path)
            if feature and feature not in mode.features():
                return web.json_response(
                    {"error": f"'{feature}' is not available on this satellite ({mode.mode_name()} mode)"}, status=403)
            try:
                return await handler(request)
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            except RuntimeError as err:
                return web.json_response({"error": str(err)}, status=409)
            except DockerError as err:
                return web.json_response({"error": f"Docker: {err.message}"}, status=502)
            except OSError as err:  # e.g. a system tool (journalctl, apt, nft) is missing
                _LOGGER.warning("%s %s failed: %s", request.method, request.path, err)
                return web.json_response({"error": str(err)}, status=500)

        app = web.Application(middlewares=[auth], client_max_size=32 * 1024 * 1024)
        r = app.router
        r.add_get("/api/status", self.h_status)
        r.add_get("/api/usb", self.h_usb)
        r.add_get("/api/serial", self.h_serial_get)
        r.add_put("/api/serial", self.h_serial_put)
        r.add_get("/api/settings", self.h_settings_get)
        r.add_put("/api/settings", self.h_settings_put)
        r.add_get("/api/updates", self.h_updates)
        r.add_post("/api/updates/check", self.h_updates_check)
        r.add_post("/api/updates/apply", self.h_updates_apply)
        r.add_post("/api/system/reboot", self.h_reboot)
        r.add_post("/api/system/shutdown", self.h_shutdown)
        r.add_post("/api/services/{unit}/{action}", self.h_service)
        r.add_post("/api/exec", self.h_exec)
        r.add_get("/api/logs", self.h_logs)
        r.add_get("/api/terminal", self.h_terminal)
        r.add_get("/api/storage", self.h_storage)
        r.add_post("/api/storage/mount", self.h_storage_mount)
        r.add_post("/api/storage/unmount", self.h_storage_unmount)
        r.add_get("/api/files/list", self.h_files_list)
        r.add_get("/api/files/read", self.h_files_read)
        r.add_put("/api/files/write", self.h_files_write)
        r.add_post("/api/files/{op:mkdir|rename|delete}", self.h_files_op)
        r.add_get("/api/files/download", self.h_files_download)
        r.add_post("/api/files/upload", self.h_files_upload)
        r.add_get("/api/docker", self.h_docker)
        r.add_post("/api/docker/check", self.h_docker_check)
        r.add_post("/api/docker/install", self.h_docker_install)
        r.add_post("/api/docker/update", self.h_docker_update)
        r.add_post("/api/docker/containers/{name}/{action}", self.h_docker_container)
        r.add_get("/api/docker/containers/{name}/logs", self.h_docker_logs)
        r.add_post("/api/agent/update", self.h_agent_update)
        r.add_post("/api/agent/uninstall", self.h_uninstall)
        r.add_get("/api/agent/uninstall-plan", self.h_uninstall_plan)
        return app

    async def run(self) -> None:
        metrics.prime()
        await asyncio.get_running_loop().run_in_executor(None, self.apply_network)
        if "updates" in mode.features():
            asyncio.ensure_future(self.updater.scheduler())
        if "docker" in mode.features():
            asyncio.ensure_future(self.docker.scheduler())
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        port = int(self.config["agent_port"])
        site = web.TCPSite(runner, None, port, ssl_context=_ssl_context())
        await site.start()
        _LOGGER.info("HA Satellite agent %s listening on :%s (satellite %s, %s mode)",
                     __version__, port, self.config.get("id"), mode.mode_name())
        await asyncio.Event().wait()
