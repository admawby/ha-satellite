"""Maintenance-window logic shared by package and container updates."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

WINDOW_MINUTES = 120


def due_window(cfg: Dict[str, Any], last_window: Optional[str], now: Optional[dt.datetime] = None) -> Optional[str]:
    """Return today's window key if the schedule is enabled, we are inside today's
    two-hour window and it has not run yet today; otherwise None."""
    if not cfg.get("enabled"):
        return None
    now = now or dt.datetime.now()
    if now.weekday() not in cfg.get("days", []):
        return None
    hh, mm = (int(x) for x in str(cfg.get("time", "04:00")).split(":"))
    into = (now.hour * 60 + now.minute) - (hh * 60 + mm)
    if not 0 <= into < WINDOW_MINUTES:
        return None
    today = now.strftime("%Y-%m-%d")
    return None if last_window == today else today
