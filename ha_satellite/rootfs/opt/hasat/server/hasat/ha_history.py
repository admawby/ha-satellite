"""History graphs: read a satellite's sensor history from Home Assistant's database.

* 1h / 6h / 24h: recorded states (REST /api/history/period), exact values
* 7d / 30d:     long-term statistics (hourly mean/min/max, recorder websocket), which
                HA keeps far longer than raw states

Series are downsampled here (bucket means, gaps preserved) so the page stays fast.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .config import SUPERVISOR_TOKEN, SUPERVISOR_URL

if TYPE_CHECKING:
    from .manager import Manager
    from .store import Satellite

_LOGGER = logging.getLogger(__name__)
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
STATISTICS_FROM = 7 * 86400   # ranges this long use long-term statistics
MAX_POINTS = 360
CACHE_SECONDS = 30
GPU_BRAND = {"nvidia": "NVIDIA", "intel": "Intel", "amd": "AMD", "pi": "VideoCore"}


def _groups(sat: "Satellite", ents: Dict[str, str]) -> List[Dict[str, Any]]:
    """Which charts this satellite gets and the entities behind each series."""
    gpus = (sat.metrics or {}).get("gpus") or []
    many = len(gpus) > 1

    def gpu_label(g):
        return f"GPU {GPU_BRAND.get(g['vendor'], '')}".strip() if many else "GPU"

    groups = [
        {"id": "temps", "title": "Temperatures",
         "series": [("cpu_temp", "CPU")] + [(f"gpu_{g['key']}_temp", gpu_label(g)) for g in gpus]},
        {"id": "gpu_mem", "title": "Graphics memory",
         "series": [(f"gpu_{g['key']}_mem_percent", gpu_label(g)) for g in gpus]},
        {"id": "disk", "title": "Disk usage", "series": [("disk_percent", "Disk")]},
        {"id": "net", "title": "Network", "series": [("net_rx", "Download"), ("net_tx", "Upload")]},
    ]
    out = []
    for grp in groups:
        series = [{"key": k, "label": label, "entity_id": ents[k]} for k, label in grp["series"] if k in ents]
        if series:
            out.append({"id": grp["id"], "title": grp["title"], "series": series})
    return out


def _downsample(points: List[Tuple[float, Optional[float]]], start: float, end: float) -> List[List[Any]]:
    """Average into at most MAX_POINTS buckets; empty buckets become gaps (null)."""
    if not points:
        return []
    width = (end - start) / MAX_POINTS
    buckets: Dict[int, List[float]] = {}
    gaps: set = set()
    for ts, v in points:
        b = min(MAX_POINTS - 1, max(0, int((ts - start) / width)))
        if v is None:
            gaps.add(b)
        else:
            buckets.setdefault(b, []).append(v)
    out = []
    for b in sorted(set(buckets) | gaps):
        vals = buckets.get(b)
        out.append([int((start + (b + 0.5) * width) * 1000), round(sum(vals) / len(vals), 3) if vals else None])
    return out


def _num(state: Any) -> Optional[float]:
    try:
        return float(state)
    except (TypeError, ValueError):
        return None  # unavailable / unknown -> a gap in the line


class HistoryReader:
    def __init__(self, mgr: "Manager") -> None:
        self.mgr = mgr
        self._cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

    async def for_satellite(self, sat: "Satellite", range_key: str) -> Dict[str, Any]:
        if range_key not in RANGES:
            raise ValueError("range must be one of " + ", ".join(RANGES))
        cached = self._cache.get((sat.id, range_key))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        if not SUPERVISOR_TOKEN or not self.mgr.options.publish_to_ha:
            return {"range": range_key, "groups": [], "note": "Graphs need 'Publish to Home Assistant' enabled."}
        ents = (await self.mgr.entities.entity_map()).get(sat.id, {})
        groups = _groups(sat, ents)
        end = time.time()
        start = end - RANGES[range_key]
        entity_ids = [s["entity_id"] for g in groups for s in g["series"]]
        if not entity_ids:
            result = {"range": range_key, "groups": [], "note": "No Home Assistant entities for this satellite yet."}
        else:
            if RANGES[range_key] >= STATISTICS_FROM:
                data, units, source = await self._statistics(entity_ids, start, end)
            else:
                data, units, source = await self._history(entity_ids, start, end)
            for g in groups:
                for s in g["series"]:
                    s["points"] = _downsample(data.get(s["entity_id"], []), start, end)
                    s["unit"] = units.get(s["entity_id"])
                g["unit"] = next((s["unit"] for s in g["series"] if s["unit"]), None)
            result = {"range": range_key, "start": int(start * 1000), "end": int(end * 1000),
                      "source": source, "groups": groups}
        self._cache[(sat.id, range_key)] = (time.monotonic(), result)
        return result

    async def _history(self, entity_ids: List[str], start: float, end: float):
        """Recorded states via HA's REST API."""
        start_iso = dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat()
        end_iso = dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat()
        url = (f"{SUPERVISOR_URL}/core/api/history/period/{quote(start_iso)}"
               f"?filter_entity_id={','.join(entity_ids)}&end_time={quote(end_iso)}&minimal_response")
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        async with aiohttp.ClientSession(headers=headers) as http:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status >= 400:
                    raise RuntimeError(f"Home Assistant history: HTTP {r.status}")
                rows = await r.json()
        data: Dict[str, List] = {}
        units: Dict[str, Optional[str]] = {}
        for states in rows:
            if not states:
                continue
            eid = states[0].get("entity_id")
            units[eid] = (states[0].get("attributes") or {}).get("unit_of_measurement")
            pts = []
            for st in states:
                ts = st.get("last_changed") or st.get("last_updated")
                try:
                    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (AttributeError, ValueError):
                    continue
                pts.append((max(t, start), _num(st.get("state"))))
            if pts and pts[-1][1] is not None:
                pts.append((end, pts[-1][1]))  # HA stores changes only: hold the last value until now
            data[eid] = pts
        return data, units, "history"

    async def _statistics(self, entity_ids: List[str], start: float, end: float):
        """Hourly long-term statistics via the recorder websocket API."""
        url = SUPERVISOR_URL.replace("http://", "ws://").replace("https://", "wss://") + "/core/websocket"
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url) as ws:
                async def call(msg_id: int, payload: Dict[str, Any]) -> Any:
                    await ws.send_str(json.dumps(dict(payload, id=msg_id)))
                    while True:
                        msg = await ws.receive_json()
                        if msg.get("id") == msg_id and msg.get("type") == "result":
                            if not msg.get("success"):
                                raise RuntimeError(f"Home Assistant statistics: {msg.get('error')}")
                            return msg.get("result")

                hello = await ws.receive_json()
                if hello.get("type") == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant websocket authentication failed")
                meta = await call(1, {"type": "recorder/get_statistics_metadata", "statistic_ids": entity_ids})
                stats = await call(2, {
                    "type": "recorder/statistics_during_period",
                    "start_time": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
                    "end_time": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(),
                    "statistic_ids": entity_ids, "period": "hour", "types": ["mean", "state"],
                })
        units = {m["statistic_id"]: m.get("statistics_unit_of_measurement") for m in meta or []}
        data: Dict[str, List] = {}
        for eid, rows in (stats or {}).items():
            pts = []
            for row in rows:
                t = row.get("start")
                t = t / 1000 if isinstance(t, (int, float)) else dt.datetime.fromisoformat(str(t)).timestamp()
                value = row.get("mean") if row.get("mean") is not None else row.get("state")
                pts.append((t + 1800, _num(value)))  # centre of the hour
            data[eid] = pts
        return data, units, "statistics"


async def gather_safely(coro) -> Dict[str, Any]:
    """Graphs are a convenience: report errors instead of failing the page."""
    try:
        return await asyncio.wait_for(coro, 45)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("History for graphs failed: %s", err)
        return {"groups": [], "error": str(err)}
