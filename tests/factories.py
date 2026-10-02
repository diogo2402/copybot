"""Builders for synthetic API objects used across selection tests."""

from __future__ import annotations

from decimal import Decimal
from itertools import count
from typing import Any

from copybot.clock import DAY_MS, HOUR_MS
from copybot.hl.models import Candle, Fill, LeaderboardRow, Portfolio

NOW = 1_790_000_000_000 - (1_790_000_000_000 % HOUR_MS)  # an exact hour, for tidy candle maths
_tid = count(1)


def fill(
    coin: str = "BTC",
    *,
    t: int,
    start: float,
    sz: float,
    side: str,
    px: float = 100.0,
    closed_pnl: float = 0.0,
    fee: float = 0.0,
    crossed: bool = True,
    tid: int | None = None,
) -> Fill:
    return Fill.model_validate(
        {
            "coin": coin,
            "px": str(px),
            "sz": str(sz),
            "side": side,
            "time": t,
            "startPosition": str(start),
            "dir": "x",
            "closedPnl": str(closed_pnl),
            "hash": "0x",
            "oid": 1,
            "tid": tid if tid is not None else next(_tid),
            "crossed": crossed,
            "fee": str(fee),
        }
    )


def trip(
    coin: str,
    *,
    t_open: int,
    hours: float,
    size: float = 1.0,
    px_open: float = 100.0,
    px_close: float = 101.0,
    long: bool = True,
    crossed: bool = True,
) -> list[Fill]:
    """One open + one close fill; closedPnl on the close."""
    t_close = t_open + int(hours * HOUR_MS)
    pnl = (px_close - px_open) * size * (1 if long else -1)
    sign = 1 if long else -1
    return [
        fill(
            coin, t=t_open, start=0, sz=size, side="B" if long else "A", px=px_open, crossed=crossed
        ),
        fill(
            coin,
            t=t_close,
            start=sign * size,
            sz=size,
            side="A" if long else "B",
            px=px_close,
            closed_pnl=pnl,
            crossed=crossed,
        ),
    ]


def _series(points: list[tuple[int, float]]) -> list[list[Any]]:
    return [[t, str(v)] for t, v in points]


def portfolio(
    av: list[tuple[int, float]],
    pnl: list[tuple[int, float]],
    *,
    month_start: int | None = None,
) -> Portfolio:
    """Builds allTime/month (+perp*) windows from absolute series. `pnl` is cumulative; each
    window's pnlHistory is rebased to start at 0 like the real API."""
    month_start = month_start if month_start is not None else NOW - 30 * DAY_MS

    def window(lo: int) -> dict[str, Any]:
        a = [(t, v) for t, v in av if t >= lo]
        p = [(t, v) for t, v in pnl if t >= lo]
        base = p[0][1] if p else 0.0
        return {
            "accountValueHistory": _series(a),
            "pnlHistory": _series([(t, v - base) for t, v in p]),
            "vlm": "0",
        }

    all_w = window(-1)
    mon_w = window(month_start)
    return Portfolio.model_validate(
        [
            ["allTime", all_w],
            ["month", mon_w],
            ["perpAllTime", all_w],
            ["perpMonth", mon_w],
        ]
    )


def steady_portfolio(
    *, days: int = 120, av: float = 1_000_000.0, daily_pnl: float = 2_000.0, step_h: int = 24
) -> Portfolio:
    pts = list(range(NOW - days * DAY_MS, NOW + 1, step_h * HOUR_MS))
    pnl = [(t, daily_pnl * (t - pts[0]) / DAY_MS) for t in pts]
    return portfolio([(t, av + v) for t, v in pnl], pnl)


def candles(coin: str, start: int, end: int, price: Any) -> list[Candle]:
    """Hourly candles; `price(t)` gives the price at time t (open at t, close at t+1h)."""
    out = []
    t = start - start % HOUR_MS
    while t < end:
        o, c = price(t), price(t + HOUR_MS)
        out.append(
            Candle.model_validate(
                {
                    "t": t,
                    "T": t + HOUR_MS - 1,
                    "s": coin,
                    "i": "1h",
                    "o": str(o),
                    "c": str(c),
                    "h": str(max(o, c)),
                    "l": str(min(o, c)),
                    "v": "1",
                    "n": 1,
                }
            )
        )
        t += HOUR_MS
    return out


def lb_row(
    address: str,
    *,
    av: float = 500_000,
    month_pnl: float = 50_000,
    month_roi: float = 0.1,
    month_vlm: float = 5_000_000,
    alltime_pnl: float = 100_000,
) -> LeaderboardRow:
    def perf(pnl: float, roi: float, vlm: float) -> dict[str, str]:
        return {"pnl": str(pnl), "roi": str(roi), "vlm": str(vlm)}

    return LeaderboardRow.model_validate(
        {
            "ethAddress": address,
            "accountValue": str(av),
            "displayName": None,
            "windowPerformances": [
                ["day", perf(0, 0, 0)],
                ["week", perf(0, 0, 0)],
                ["month", perf(month_pnl, month_roi, month_vlm)],
                ["allTime", perf(alltime_pnl, 0.5, month_vlm * 3)],
            ],
        }
    )


D = Decimal
