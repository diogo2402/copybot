"""Injectable clock. All internal time is UTC milliseconds (Hyperliquid's native unit)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FixedClock:
    """Test clock: returns a set time, advanced manually."""

    def __init__(self, now_ms: int) -> None:
        self._now = now_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all times must be timezone-aware")
    return int(dt.timestamp() * 1000)


HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
