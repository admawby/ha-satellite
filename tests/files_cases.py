"""USB & Storage tab scenarios (file browser/editor, transfers, drives) and the Docker
install flow, run by test_e2e.py through the real UI proxy -> mTLS -> agent path."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Callable

import aiohttp


async def run_files(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, sat_id: str, root: Path) -> None:
    proxy = f"{ui}/api/sat/{sat_id}/proxy"
    r_ = root.as_posix()
    (root / "sub").mkdir(parents=True)
    (root / "hello.txt").write_bytes(b"hello\nworld\n")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (root / "sub" / "inner.txt").write_text("x")

    async def call(method: str, path: str, **kw):
        async with http.request(method, f"{proxy}/{path}", **kw) as r:
            return r.status, await r.json()

    st, data = await call("GET", "files/list", params={"path": r_})
    names = [e["name"] for e in data["entries"]]
    check(st == 200 and names[0] == "sub" and set(names) == {"sub", "hello.txt", "blob.bin"}, "files: listing, folders first")
    check(data["usage"]["total"] > 0 and data["parent"], "files: disk usage and parent reported")

    st, f = await call("GET", "files/read", params={"path": f"{r_}/hello.txt"})
    check(st == 200 and f["editable"] and f["content"] == "hello\nworld\n", "files: read text file")
    st, b = await call("GET", "files/read", params={"path": f"{r_}/blob.bin"})
    check(st == 200 and not b["editable"] and "binary" in b["reason"], "files: binary files not opened in editor")

    st, w = await call("PUT", "files/write", json={"path": f"{r_}/hello.txt", "content": "edited ✓\n", "expect_mtime": f["mtime"]})
    check(st == 200 and (root / "hello.txt").read_bytes() == "edited ✓\n".encode(), "files: save edited file")
    st, err = await call("PUT", "files/write", json={"path": f"{r_}/hello.txt", "content": "stale", "expect_mtime": f["mtime"] - 100})
    check(st == 502 and "changed on the satellite" in err["error"], "files: stale save rejected (concurrent change)")
    st, _ = await call("PUT", "files/write", json={"path": f"{r_}/new.conf", "content": "", "create": True})
    check(st == 200 and (root / "new.conf").exists(), "files: create new file")
    st, err = await call("PUT", "files/write", json={"path": f"{r_}/new.conf", "content": "", "create": True})
    check(st == 502 and "already exists" in err["error"], "files: create refuses to overwrite")

    st, _ = await call("POST", "files/mkdir", json={"path": f"{r_}/made"})
    check(st == 200 and (root / "made").is_dir(), "files: new folder")
    st, _ = await call("POST", "files/rename", json={"from": f"{r_}/new.conf", "to": f"{r_}/made/renamed.conf"})
    check(st == 200 and (root / "made" / "renamed.conf").exists(), "files: rename / move")
    st, err = await call("POST", "files/delete", json={"path": f"{r_}/sub", "recursive": False})
    check(st == 502 and "not empty" in err["error"], "files: non-recursive delete of non-empty folder refused")
    st, _ = await call("POST", "files/delete", json={"path": f"{r_}/sub", "recursive": True})
    check(st == 200 and not (root / "sub").exists(), "files: recursive folder delete")

    st, err = await call("POST", "files/delete", json={"path": "/", "recursive": True})
    check(st == 502 and "refusing" in err["error"], "files: system directories protected")
    st, err = await call("PUT", "files/write", json={"path": "/proc/sys/kernel/hostname", "content": "x"})
    check(st == 502 and "pseudo file system" in err["error"], "files: /proc writes refused")
    st, err = await call("GET", "files/list", params={"path": "relative/path"})
    check(st == 502 and "absolute path" in err["error"], "files: relative paths rejected")

    # Streaming upload/download through the manager (bigger than any JSON body limit).
    payload = os.urandom(3 * 1024 * 1024 + 123)
    up = f"{ui}/api/sat/{sat_id}/files/upload"
    async with http.post(up, params={"path": f"{r_}/big.bin"}, data=payload) as r:
        res = await r.json()
        check(r.status == 200 and res["size"] == len(payload) and (root / "big.bin").read_bytes() == payload, "files: 3 MB upload streamed")
    async with http.post(up, params={"path": f"{r_}/big.bin"}, data=b"again") as r:
        check(r.status == 502 and "already exists" in (await r.json())["error"], "files: upload asks before overwriting")
    async with http.post(up, params={"path": f"{r_}/big.bin", "overwrite": "1"}, data=payload[::-1]) as r:
        check(r.status == 200 and (root / "big.bin").read_bytes() == payload[::-1], "files: upload overwrite")
    async with http.get(f"{ui}/api/sat/{sat_id}/files/download", params={"path": f"{r_}/big.bin"}) as r:
        body = await r.read()
        check(r.status == 200 and body == payload[::-1] and "big.bin" in r.headers.get("Content-Disposition", ""), "files: download streamed")
    async with http.get(f"{ui}/api/sat/{sat_id}/files/download", params={"path": f"{r_}/missing"}) as r:
        check(r.status == 502, "files: download of missing file reports error")
    check(not [p for p in root.iterdir() if p.name.startswith(".hasat-")], "files: no temp files left behind")

    st, sto = await call("GET", "storage")
    check(st == 200 and isinstance(sto["devices"], list) and sto["mounts"], "storage: drives and mounts listed")
    st, err = await call("POST", "storage/mount", json={"device": "/dev/does-not-exist"})
    check(st == 502 and "unknown device" in err["error"], "storage: mounting unknown device refused")
    st, err = await call("POST", "storage/unmount", json={"mountpoint": "/"})
    check(st == 502 and "only removable media" in err["error"], "storage: system mounts cannot be unmounted")


async def run_docker_install(check: Callable[[bool, str], None], http: aiohttp.ClientSession, ui: str, sat_id: str, agent) -> None:
    from hasat_agent.docker_mgr import DockerAPI

    proxy = f"{ui}/api/sat/{sat_id}/proxy"
    # Docker is "running" (fake engine) -> installing again must be refused.
    async with http.post(f"{proxy}/docker/install", json={"method": "official"}) as r:
        check(r.status == 502 and "already installed" in (await r.json())["error"], "docker install: refused when Docker runs")

    real_api, real_cmds, real_wait = agent.docker.api, agent.docker.INSTALL_COMMANDS, agent.docker.INSTALL_WAIT
    agent.docker.api = DockerAPI("unix:///nonexistent/hasat/docker.sock")
    agent.docker.INSTALL_WAIT = 1
    try:
        async with http.get(f"{proxy}/docker") as r:
            d = await r.json()
            check(d["available"] is False and "installed" in d, "docker install: tab reports Docker missing")

        agent.docker.INSTALL_COMMANDS = {
            "official": [[sys.executable, "-c", "print('downloading get-docker.sh'); print('installing docker-ce')"],
                         [sys.executable, "-c", "print('enabling docker.service')"]],
            "debian": [[sys.executable, "-c", "import sys; print('E: unable to locate package'); sys.exit(100)"]],
            "start": [[sys.executable, "-c", "print('started')"]],
        }
        async with http.post(f"{proxy}/docker/install", json={"method": "official"}) as r:
            check(r.status == 200, "docker install: job started")
        for _ in range(50):
            async with http.get(f"{proxy}/docker") as r:
                d = await r.json()
            if not d["job"]["running"]:
                break
            await asyncio.sleep(0.1)
        log = "\n".join(d["job"]["log"])
        check("installing docker-ce" in log and "enabling docker.service" in log, "docker install: output streamed to job log")
        check("did not appear" in d["job"]["last_result"], "docker install: result reported")

        async with http.post(f"{proxy}/docker/install", json={"method": "debian"}) as r:
            check(r.status == 200, "docker install: second method accepted")
        for _ in range(50):
            async with http.get(f"{proxy}/docker") as r:
                d = await r.json()
            if not d["job"]["running"]:
                break
            await asyncio.sleep(0.1)
        check("install failed" in d["job"]["last_result"] and "exited 100" in d["job"]["last_result"],
              "docker install: failing step stops the install")
        async with http.post(f"{proxy}/docker/install", json={"method": "curl | sh"}) as r:
            check(r.status == 502, "docker install: unknown method rejected")
    finally:
        agent.docker.api, agent.docker.INSTALL_COMMANDS, agent.docker.INSTALL_WAIT = real_api, real_cmds, real_wait
