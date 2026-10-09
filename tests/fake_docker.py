"""A small in-memory Docker Engine API used by the end-to-end tests.

It implements just enough of the real API (same paths, payload shapes and status
codes) for the agent's container listing, logs, actions, registry checks, pulls and
the stop/rename/create/connect/start/remove recreate sequence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
from typing import Any, Dict, Optional

from aiohttp import web


def _id(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class FakeDocker:
    def __init__(self) -> None:
        self.images: Dict[str, Dict[str, Any]] = {}      # image id -> image
        self.tags: Dict[str, str] = {}                     # "repo:tag" -> image id
        self.remote: Dict[str, Dict[str, Any]] = {}        # "repo:tag" -> image published in the registry
        self.containers: Dict[str, Dict[str, Any]] = {}    # container id -> container
        self.calls: list = []
        self._n = 0

    # ------------------------------------------------------------- fixtures
    def make_image(self, ref: str, version: str, env=None, labels=None, cmd=None) -> Dict[str, Any]:
        repo = ref.rsplit(":", 1)[0]
        img_id = _id(f"{ref}-{version}")
        digest = _id(f"digest-{ref}-{version}")
        return {
            "Id": img_id,
            "RepoTags": [ref],
            "RepoDigests": [f"{repo}@{digest}"],
            "Digest": digest,
            "Config": {"Env": env or ["PATH=/usr/bin", f"IMAGE_VERSION={version}"],
                       "Cmd": cmd or ["run"], "Labels": labels or {"org.opencontainers.image.version": version},
                       "WorkingDir": "/app"},
        }

    def add_local(self, img: Dict[str, Any]) -> None:
        self.images[img["Id"]] = copy.deepcopy(img)
        self.tags[img["RepoTags"][0]] = img["Id"]

    def publish(self, img: Dict[str, Any]) -> None:
        self.remote[img["RepoTags"][0]] = copy.deepcopy(img)

    def run(self, name: str, ref: str, *, env=None, labels=None, host=None, networks=None, running=True) -> str:
        img = self.images[self.tags[ref]]
        body = {"Image": ref, "Env": env or [], "Labels": labels or {}, "HostConfig": host or {"NetworkMode": "bridge"}}
        cid = self._create(name, body)
        c = self.containers[cid]
        for net, ep in (networks or {}).items():
            c["NetworkSettings"]["Networks"][net] = dict({"Aliases": [cid[:12], name]}, **ep)
        c["State"]["Running"] = running
        assert img
        return cid

    # -------------------------------------------------------------- helpers
    def _create(self, name: str, body: Dict[str, Any]) -> str:
        self._n += 1
        cid = hashlib.sha256(f"{name}-{self._n}".encode()).hexdigest()
        ref = body["Image"]
        img = self.images[self.tags[ref]]
        icfg = img["Config"]
        env = {e.split("=", 1)[0]: e for e in icfg.get("Env") or []}
        env.update({e.split("=", 1)[0]: e for e in body.get("Env") or []})
        cfg = {
            "Image": ref,
            "Env": list(env.values()),
            "Cmd": body.get("Cmd") or icfg.get("Cmd"),
            "WorkingDir": body.get("WorkingDir") or icfg.get("WorkingDir"),
            "Labels": dict(icfg.get("Labels") or {}, **(body.get("Labels") or {})),
            "Hostname": body.get("Hostname") or cid[:12],
            "Tty": False,
        }
        host = copy.deepcopy(body.get("HostConfig") or {"NetworkMode": "bridge"})
        nets = {}
        mode = host.get("NetworkMode", "bridge")
        endpoints = (body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}
        if endpoints:
            for net, ep in endpoints.items():
                nets[net] = dict({"Aliases": [cid[:12]] + list(ep.get("Aliases") or [])}, **{k: v for k, v in ep.items() if k != "Aliases"})
        elif mode not in ("host", "none") and not mode.startswith("container:"):
            nets["bridge" if mode == "default" else mode] = {"Aliases": [cid[:12]]}
        self.containers[cid] = {
            "Id": cid, "Name": "/" + name, "Image": img["Id"], "Config": cfg, "HostConfig": host,
            "State": {"Running": False, "Status": "created"},
            "NetworkSettings": {"Networks": nets},
        }
        return cid

    def find(self, key: str) -> Optional[Dict[str, Any]]:
        for c in self.containers.values():
            if c["Id"] == key or c["Id"].startswith(key) or c["Name"] == "/" + key:
                return c
        return None

    def by_name(self, name: str) -> Dict[str, Any]:
        c = self.find(name)
        assert c, name
        return c

    # ------------------------------------------------------------------ app
    def app(self) -> web.Application:
        def err(status: int, msg: str) -> web.Response:
            return web.json_response({"message": msg}, status=status)

        def container_or_404(request):
            c = self.find(request.match_info["key"])
            if not c:
                raise web.HTTPNotFound(text=json.dumps({"message": "No such container"}), content_type="application/json")
            return c

        async def list_containers(request):
            out = []
            for c in self.containers.values():
                running = c["State"]["Running"]
                out.append({
                    "Id": c["Id"], "Names": [c["Name"]], "Image": c["Config"]["Image"], "ImageID": c["Image"],
                    "State": "running" if running else "exited", "Status": "Up 2 hours" if running else "Exited (0)",
                    "Ports": [{"PrivatePort": int(p.split("/")[0]), "PublicPort": int(b[0]["HostPort"]), "Type": p.split("/")[1]}
                              for p, b in (c["HostConfig"].get("PortBindings") or {}).items() if b],
                    "Labels": c["Config"]["Labels"], "Created": 1700000000,
                    "HostConfig": {"NetworkMode": c["HostConfig"].get("NetworkMode", "bridge")},
                })
            return web.json_response(out)

        async def inspect(request):
            return web.json_response(container_or_404(request))

        async def action(request):
            c = container_or_404(request)
            act = request.match_info["action"]
            self.calls.append((act, c["Name"]))
            if act == "start":
                if c["State"]["Running"]:
                    return web.Response(status=304)
                if c["Config"]["Labels"].get("test.fail-start") == "true" and c.get("_recreated"):
                    return err(500, "simulated start failure")
                c["State"]["Running"] = True
            elif act == "stop":
                if not c["State"]["Running"]:
                    return web.Response(status=304)
                c["State"]["Running"] = False
            elif act == "restart":
                c["State"]["Running"] = True
            elif act == "rename":
                new = request.query["name"]
                if self.find(new) and self.find(new) is not c:
                    return err(409, "name in use")
                c["Name"] = "/" + new
            return web.Response(status=204)

        async def create(request):
            body = await request.json()
            name = request.query["name"]
            self.calls.append(("create", name))
            if self.find(name):
                return err(409, f'Conflict. The container name "/{name}" is already in use')
            if (body.get("Labels") or {}).get("test.fail-create") == "true":
                return err(500, "simulated create failure")
            cid = self._create(name, body)
            self.containers[cid]["_recreated"] = True
            self.containers[cid]["_create_body"] = body
            return web.json_response({"Id": cid, "Warnings": []}, status=201)

        async def connect(request):
            body = await request.json()
            c = self.find(body["Container"])
            net = request.match_info["net"]
            ep = body.get("EndpointConfig") or {}
            c["NetworkSettings"]["Networks"][net] = dict({"Aliases": [c["Id"][:12]] + list(ep.get("Aliases") or [])},
                                                         **{k: v for k, v in ep.items() if k != "Aliases"})
            return web.Response(status=200)

        async def remove(request):
            c = container_or_404(request)
            if c["State"]["Running"] and request.query.get("force") != "1":
                return err(409, "container is running")
            self.calls.append(("remove", c["Name"]))
            del self.containers[c["Id"]]
            return web.Response(status=204)

        async def logs(request):
            container_or_404(request)
            frames = b""
            for stream, text in ((1, "2026-10-09T10:00:00Z hello from stdout\n"), (2, "2026-10-09T10:00:01Z warning on stderr\n")):
                data = text.encode()
                frames += struct.pack(">BxxxI", stream, len(data)) + data
            return web.Response(body=frames, content_type="application/vnd.docker.multiplexed-stream")

        async def image_inspect(request):
            ref = request.match_info["ref"]
            img_id = self.tags.get(ref) or (ref if ref in self.images else None)
            if not img_id or img_id not in self.images:
                return err(404, f"No such image: {ref}")
            return web.json_response(self.images[img_id])

        async def image_delete(request):
            img_id = request.match_info["ref"]
            if any(c["Image"] == img_id for c in self.containers.values()):
                return err(409, "image is being used by a container")
            if img_id in self.images:
                del self.images[img_id]
                self.calls.append(("rmi", img_id))
                return web.json_response([{"Deleted": img_id}])
            return err(404, "No such image")

        async def distribution(request):
            ref = request.match_info["ref"]
            if ref not in self.remote:
                return err(404, "manifest unknown")
            return web.json_response({"Descriptor": {"mediaType": "application/vnd.oci.image.index.v1+json",
                                                     "digest": self.remote[ref]["Digest"], "size": 1000}})

        async def pull(request):
            ref = f"{request.query['fromImage']}:{request.query.get('tag', 'latest')}"
            self.calls.append(("pull", ref))
            resp = web.StreamResponse(headers={"Content-Type": "application/json"})
            await resp.prepare(request)
            if ref not in self.remote:
                await resp.write(json.dumps({"error": "manifest unknown"}).encode() + b"\r\n")
                return resp
            img = self.remote[ref]
            await resp.write(json.dumps({"status": f"Pulling from {ref}"}).encode() + b"\r\n")
            await resp.write(json.dumps({"status": "Downloading", "progressDetail": {"current": 1}}).encode() + b"\r\n")
            self.add_local(img)
            await resp.write(json.dumps({"status": f"Digest: {img['Digest']}"}).encode() + b"\r\n")
            await resp.write(json.dumps({"status": f"Status: Downloaded newer image for {ref}"}).encode() + b"\r\n")
            return resp

        app = web.Application()
        r = app.router
        r.add_get("/containers/json", list_containers)
        r.add_post("/containers/create", create)
        r.add_get("/containers/{key}/json", inspect)
        r.add_get("/containers/{key}/logs", logs)
        r.add_post("/containers/{key}/{action:start|stop|restart|rename}", action)
        r.add_delete("/containers/{key}", remove)
        r.add_post("/networks/{net}/connect", connect)
        r.add_get("/images/{ref:.+}/json", image_inspect)
        r.add_delete("/images/{ref:.+}", image_delete)
        r.add_post("/images/create", pull)
        r.add_get("/distribution/{ref:.+}/json", distribution)
        return app
