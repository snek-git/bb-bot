"""Time helpers. All stored timestamps are unix seconds (UTC) as floats."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def now() -> float:
    return time.time()


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_dt(ts: float, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, zone(tz))


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def humanize_ago(seconds: float) -> str:
    """'just now', '5 minutes ago', '3 hours ago', '2 days ago', '3 weeks ago'."""
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{_plural(s // 60, 'minute')} ago"
    if s < 86400:
        return f"{_plural(s // 3600, 'hour')} ago"
    if s < 86400 * 14:
        return f"{_plural(s // 86400, 'day')} ago"
    return f"{_plural(s // (86400 * 7), 'week')} ago"
