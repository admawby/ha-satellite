"""Bulk updates across satellites ("Update All" buttons).

A job runs in the add-on (not the browser), so it keeps going when the tab closes.
Satellites are updated in parallel; on each satellite packages go first and Docker
images second, so an apt upgrade of Docker itself cannot restart the daemon in the
middle of an image update. Each step starts the agent's own update job and then
follows it until it finishes.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .agent_client import AgentError

if TYPE_CHECKING:
    from .manager import Manager
    from .store import Satellite

_LOGGER = logging.getLogger(__name__)

SCOPES = {"all": ("packages", "docker"), "packages": ("packages",), "docker": ("docker",)}
ALL_FEATURES = {"health", "terminal", "docker", "docker_install", "radios", "files",
                "updates", "logs", "power", "services", "firewall"}
STEP_TIMEOUT = 90 * 60     # give up following a step after this long (it keeps running on the device)
FOLLOW_EVERY = 3.0


@dataclass
class Step:
    kind: str                 # "packages" | "docker"
    state: str = "waiting"    # waiting | running | done | failed | skipped
    detail: str = ""

    def to_api(self) -> Dict[str, Any]:
        return {"kind": self.kind, "state": self.state, "detail": self.detail}


@dataclass
class SatelliteRun:
    sat_id: str
    name: str
    steps: List[Step]

    @property
    def state(self) -> str:
        states = {s.state for s in self.steps}
        if states & {"running", "waiting"}:
            return "running"
        return "failed" if "failed" in states else "done"

    def to_api(self) -> Dict[str, Any]:
        return {"sat_id": self.sat_id, "name": self.name, "state": self.state,
                "steps": [s.to_api() for s in self.steps]}


@dataclass
class BulkJob:
    id: str
    scope: str
    runs: List[SatelliteRun]
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    dismissed: bool = False  # hidden from the progress panel (kept by the add-on, so reloads respect it)

    @property
    def running(self) -> bool:
        return self.finished is None

    def to_api(self) -> Dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "running": self.running, "started": self.started,
                "finished": self.finished, "dismissed": self.dismissed,
                "satellites": [r.to_api() for r in self.runs]}


def _features(sat: "Satellite") -> set:
    feats = (sat.metrics or {}).get("features")
    return set(feats) if feats else set(ALL_FEATURES)


class BulkUpdates:
    def __init__(self, mgr: "Manager") -> None:
        self.mgr = mgr
        self.jobs: List[BulkJob] = []

    def busy_satellites(self) -> set:
        return {r.sat_id for j in self.jobs if j.running for r in j.runs if r.state == "running"}

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.jobs[-1].to_api() if self.jobs else None

    def dismiss(self, job_id: str) -> None:
        for job in self.jobs:
            if job.id == job_id:
                if job.running:
                    raise RuntimeError("the update is still running")
                job.dismissed = True
                return
        raise ValueError("unknown job")

    def start(self, scope: str, sat_ids: Optional[List[str]] = None) -> BulkJob:
        if scope not in SCOPES:
            raise ValueError("scope must be all, packages or docker")
        sats = [s for s in self.mgr.store.satellites.values() if not sat_ids or s.id in sat_ids]
        if not sats:
            raise ValueError("no satellites selected")
        busy = self.busy_satellites()
        clash = [s.name for s in sats if s.id in busy]
        if clash:
            raise RuntimeError(f"an update is already running on {', '.join(clash)}")
        job = BulkJob(id=secrets.token_hex(4), scope=scope,
                      runs=[SatelliteRun(s.id, s.name, [Step(k) for k in SCOPES[scope]]) for s in sats])
        self.jobs = self.jobs[-9:] + [job]
        asyncio.create_task(self._run(job))
        _LOGGER.info("Bulk update %s (%s) started on %s", job.id, scope, ", ".join(s.name for s in sats))
        return job

    async def _run(self, job: BulkJob) -> None:
        try:
            await asyncio.gather(*(self._run_satellite(r) for r in job.runs), return_exceptions=True)
        finally:
            job.finished = time.time()
            for run in job.runs:
                asyncio.create_task(self.mgr.poll_one(run.sat_id))
            _LOGGER.info("Bulk update %s finished: %s", job.id,
                         ", ".join(f"{r.name}={r.state}" for r in job.runs))

    async def _run_satellite(self, run: SatelliteRun) -> None:
        for step in run.steps:
            sat = self.mgr.store.satellites.get(run.sat_id)
            if sat is None:
                step.state, step.detail = "skipped", "satellite was removed"
                continue
            if not sat.online:
                step.state, step.detail = "skipped", "satellite is offline"
                continue
            try:
                if step.kind == "packages":
                    await self._packages(sat, step)
                else:
                    await self._docker(sat, step)
            except AgentError as err:
                step.state, step.detail = "failed", str(err)
            except Exception as err:  # noqa: BLE001 - report, never crash the job
                _LOGGER.exception("Bulk step %s on %s failed", step.kind, sat.name)
                step.state, step.detail = "failed", str(err)

    async def _follow(self, sat: "Satellite", path: str, extract) -> Dict[str, Any]:
        """Poll an agent job endpoint until it reports nothing running."""
        deadline = time.monotonic() + STEP_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(FOLLOW_EVERY)
            try:
                data = extract(await self.mgr.client.request(sat, "GET", path, timeout=20))
            except AgentError:
                continue  # transient (e.g. busy device); keep following
            if not data.get("running"):
                return data
        raise TimeoutError("still running on the device after 90 minutes")

    async def _packages(self, sat: "Satellite", step: Step) -> None:
        if "updates" not in _features(sat):
            step.state, step.detail = "skipped", "package updates are not available on this satellite"
            return
        step.state, step.detail = "running", "installing package updates"
        try:
            await self.mgr.client.request(sat, "POST", "/api/updates/apply", timeout=30)
        except AgentError as err:
            if "409" not in str(err):
                raise
            step.detail = "an update was already running; following it"
        result = await self._follow(sat, "/api/updates", lambda d: d)
        outcome = result.get("last_result") or "finished"
        step.state = "failed" if "fail" in outcome else "done"
        step.detail = outcome + (" (reboot required)" if result.get("reboot_required") else "")

    async def _docker(self, sat: "Satellite", step: Step) -> None:
        if "docker" not in _features(sat):
            step.state, step.detail = "skipped", "Docker management is not available on this satellite"
            return
        info = await self.mgr.client.request(sat, "GET", "/api/docker", timeout=20)
        if not info.get("available"):
            step.state, step.detail = "skipped", "Docker is not installed"
            return
        step.state, step.detail = "running", "checking and updating images"
        try:
            await self.mgr.client.request(sat, "POST", "/api/docker/update", timeout=30)
        except AgentError as err:
            if "409" not in str(err):
                raise
            step.detail = "a Docker job was already running; following it"
        result = await self._follow(sat, "/api/docker", lambda d: d.get("job") or {})
        outcome = result.get("last_result") or "finished"
        step.state = "failed" if "fail" in outcome else "done"
        step.detail = outcome
