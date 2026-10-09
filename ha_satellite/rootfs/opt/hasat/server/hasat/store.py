"""Persistent satellite registry and short-lived enrollment tokens."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TOKEN_TTL = 30 * 60


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "satellite"


@dataclass
class Satellite:
    id: str
    name: str
    host: str
    port: int
    created: float = field(default_factory=time.time)
    hostname: str = ""
    # Runtime-only fields (not persisted)
    online: bool = False
    last_seen: float = 0.0
    last_error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    PERSISTED = ("id", "name", "host", "port", "created", "hostname")

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def to_record(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.PERSISTED}

    def to_api(self) -> dict[str, Any]:
        data = asdict(self)
        data["slug"] = self.slug
        return data


@dataclass
class EnrollToken:
    token: str
    name: str
    expires: float
    used: bool = False


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.satellites: dict[str, Satellite] = {}
        self.tokens: dict[str, EnrollToken] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        for rec in data.get("satellites", []):
            sat = Satellite(**{k: rec[k] for k in Satellite.PERSISTED if k in rec})
            self.satellites[sat.id] = sat

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"satellites": [s.to_record() for s in self.satellites.values()]}, indent=2))
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- tokens
    def new_token(self, name: str) -> EnrollToken:
        self._purge_tokens()
        tok = EnrollToken(token=secrets.token_urlsafe(32), name=name.strip() or "", expires=time.time() + TOKEN_TTL)
        self.tokens[tok.token] = tok
        return tok

    def valid_token(self, token: str) -> EnrollToken | None:
        self._purge_tokens()
        for known, tok in self.tokens.items():
            if secrets.compare_digest(known, token) and not tok.used:
                return tok
        return None

    def _purge_tokens(self) -> None:
        now = time.time()
        for key in [k for k, t in self.tokens.items() if t.expires < now or t.used]:
            del self.tokens[key]

    # ------------------------------------------------------------ satellites
    def new_satellite_id(self) -> str:
        while True:
            sid = "sat-" + secrets.token_hex(4)
            if sid not in self.satellites:
                return sid

    def unique_name(self, wanted: str) -> str:
        names = {s.slug for s in self.satellites.values()}
        name, n = wanted, 2
        while slugify(name) in names:
            name = f"{wanted} {n}"
            n += 1
        return name
