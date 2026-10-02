"""Stage 2: per-wallet metrics from portfolio history and fills (§6.3).

Floats are fine here (analytics only, §2). Two independent parts so the refresh can stop at the
first exclusion (D2): `portfolio_metrics` (one cheap request) and `fill_metrics` (expensive).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import pairwise

from copybot.clock import DAY_MS, HOUR_MS
from copybot.hl.models import Fill, Portfolio

WEEK_MS = 7 * DAY_MS

# ---------------------------------------------------------------- series helpers


def _series(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    return sorted(points)


def value_at(series: list[tuple[int, float]], t: int) -> float | None:
    """Linear interpolation; flat beyond the ends; None if empty."""
    if not series:
        return None
    times = [p[0] for p in series]
    i = bisect.bisect_right(times, t)
    if i == 0:
        return series[0][1]
    if i == len(series):
        return series[-1][1]
    (t0, v0), (t1, v1) = series[i - 1], series[i]
    if t1 == t0:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def joined_pnl(portfolio: Portfolio, *, perp: bool = True) -> list[tuple[int, float]]:
    """Cumulative PnL: coarse all-time series up to the start of the month series, then the
    finer month series offset onto it (each window's pnlHistory starts at 0). See D4.
    perp=True uses the perp-only windows (what we copy); False the whole account."""
    at = portfolio.window("perpAllTime" if perp else "allTime")
    mo = portfolio.window("perpMonth" if perp else "month")
    alltime = _series([(t, float(v)) for t, v in at.pnlHistory]) if at else []
    month = _series([(t, float(v)) for t, v in mo.pnlHistory]) if mo else []
    if not month:
        return alltime
    m0 = month[0][0]
    offset = value_at(alltime, m0) or 0.0
    head = [p for p in alltime if p[0] < m0]
    return head + [(t, v + offset) for t, v in month]


def joined_account_value(portfolio: Portfolio) -> list[tuple[int, float]]:
    """Total account value (perp + spot). This is the trader's capital: traders move money
    between spot and perps, so the perp-only value can swing to ~0 while total equity is
    large (D10)."""
    at = portfolio.window("allTime")
    mo = portfolio.window("month")
    alltime = [(t, float(v)) for t, v in at.accountValueHistory] if at else []
    month = [(t, float(v)) for t, v in mo.accountValueHistory] if mo else []
    if not month:
        return _series(alltime)
    m0 = min(t for t, _ in month)
    return _series([p for p in alltime if p[0] < m0] + month)


MIN_EQUITY_USD = 1_000.0
MAX_STEP_LOSS = -0.99


def twr_index(
    cum_pnl: list[tuple[int, float]],
    account_value: list[tuple[int, float]],
    total_pnl: list[tuple[int, float]] | None = None,
) -> list[tuple[int, float]]:
    """Time-weighted return index starting at 1.0, chaining Modified Dietz returns (D10).

    Per interval: net deposits = ΔaccountValue - ΔtotalPnL, and
    r = ΔPnL / (accountValue_start + 0.5 * net deposits). `cum_pnl` is what we measure (perp
    PnL); `total_pnl` (whole account, defaults to cum_pnl) is used only to infer deposits.
    Counting half the period's flows in the denominator is the standard correction when flows
    happen at unknown times inside an interval
    (the coarse allTime series has ~weekly points). Intervals whose denominator is below
    MIN_EQUITY_USD are skipped, and one interval can't lose more than 99%.
    """
    if not cum_pnl:
        return [(0, 1.0)]
    idx = 1.0
    out = [(cum_pnl[0][0], idx)]
    for (t0, p0), (t1, p1) in pairwise(cum_pnl):
        av0 = value_at(account_value, t0) or 0.0
        av1 = value_at(account_value, t1) or 0.0
        dpnl = p1 - p0
        if total_pnl is not None:
            dtot = (value_at(total_pnl, t1) or 0.0) - (value_at(total_pnl, t0) or 0.0)
        else:
            dtot = dpnl
        flows = (av1 - av0) - dtot
        denom = av0 + 0.5 * flows
        if denom >= MIN_EQUITY_USD:
            idx *= 1 + max(dpnl / denom, MAX_STEP_LOSS)
        out.append((t1, idx))
    return out


def max_drawdown(equity: list[tuple[int, float]]) -> float:
    """Max peak-to-trough decline as a fraction of the peak (0.25 = 25%)."""
    peak = -math.inf
    worst = 0.0
    for _, v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


# ---------------------------------------------------------------- portfolio metrics


@dataclass
class PortfolioMetrics:
    base_equity: float  # mean account value over the window (informational)
    pnl_90d: float
    return_90d: float
    max_drawdown_90d: float  # fraction; worse of portfolio- and fills-based (after fill_metrics)
    max_drawdown_portfolio: float
    positive_weeks_ratio: float
    weekly_pnl: list[float]
    history_days: float  # how far back the PnL series actually reaches inside the window

    @property
    def return_to_drawdown(self) -> float:
        return self.return_90d / max(self.max_drawdown_90d, 0.01)


def portfolio_metrics(portfolio: Portfolio, now_ms: int, lookback_days: int) -> PortfolioMetrics:
    start = now_ms - lookback_days * DAY_MS
    pnl = joined_pnl(portfolio)
    av = joined_account_value(portfolio)
    window = [p for p in pnl if p[0] >= start]
    p_start = value_at(pnl, start) or 0.0
    p_end = value_at(pnl, now_ms) or 0.0
    history_start = max(start, pnl[0][0]) if pnl else now_ms

    # Time-weighted index (D10): each interval's PnL over the account value at its start,
    # compounded. Neutral to deposits/withdrawals and to the account growing over time.
    points = [(history_start, p_start)] + [(t, v) for t, v in window if t > history_start]
    if not points or points[-1][0] < now_ms:
        points.append((now_ms, p_end))
    index = twr_index(points, av, joined_pnl(portfolio, perp=False))
    dd = max_drawdown(index)
    av_window = [v for t, v in av if t >= start and v > 0]
    base = sum(av_window) / len(av_window) if av_window else 0.0

    weekly: list[float] = []
    for k in range(12, 0, -1):
        w0, w1 = now_ms - k * WEEK_MS, now_ms - (k - 1) * WEEK_MS
        if w1 <= history_start:
            continue  # no data for this week; don't count it either way
        a = value_at(pnl, max(w0, history_start)) or 0.0
        b = value_at(pnl, w1) or 0.0
        weekly.append(b - a)
    pos_ratio = sum(1 for w in weekly if w > 0) / len(weekly) if weekly else 0.0

    return PortfolioMetrics(
        base_equity=base,
        pnl_90d=p_end - p_start,
        return_90d=index[-1][1] - 1.0,
        max_drawdown_90d=dd,
        max_drawdown_portfolio=dd,
        positive_weeks_ratio=pos_ratio,
        weekly_pnl=weekly,
        history_days=(now_ms - history_start) / DAY_MS,
    )


# ---------------------------------------------------------------- round trips


@dataclass
class RoundTrip:
    coin: str
    direction: int  # +1 long, -1 short
    opened_at: int | None  # None: position was already open before our fill history
    closed_at: int | None  # None: still open at the end of the window
    pnl: float = 0.0  # closedPnl - fees over the trip

    @property
    def holding_hours(self) -> float | None:
        if self.opened_at is None or self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at) / HOUR_MS


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def round_trips(fills: list[Fill]) -> list[RoundTrip]:
    """Reconstruct round trips per coin from `startPosition` and fill direction.

    A trip opens when the position leaves 0 and closes when it returns to 0; a sign flip closes
    one trip and opens another. Each fill's own `startPosition` is trusted (rather than a
    running sum) so gaps in history or non-fill position changes don't corrupt later trips.
    """
    by_coin: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_coin[f.coin].append(f)
    trips: list[RoundTrip] = []
    for coin, cf in by_coin.items():
        cf.sort(key=Fill.chrono_key)
        cur: RoundTrip | None = None
        for f in cf:
            before = float(f.startPosition)
            after = before + float(f.signed_sz)
            if abs(after) < 1e-12:
                after = 0.0
            sb, sa = _sign(before), _sign(after)
            if cur is not None and sb != cur.direction:
                # Gap: the position changed without fills we can see. The stale trip's end is
                # unknown, so keep it out of the closed-trip statistics.
                trips.append(cur)
                cur = None
            if cur is None and sb != 0:
                cur = RoundTrip(coin, sb, None, None)  # opened before our history
            fill_pnl = float(f.closedPnl) - float(f.fee)
            if cur is not None and sb != 0 and sa != sb:
                # this fill closes the current trip (fully, or by flipping through zero)
                cur.pnl += fill_pnl
                cur.closed_at = f.time
                trips.append(cur)
                cur = None
                if sa != 0:
                    cur = RoundTrip(coin, sa, f.time, None)
                continue
            if cur is None and sa != 0:
                cur = RoundTrip(coin, sa, f.time, None)
            if cur is not None:
                cur.pnl += fill_pnl
        if cur is not None:
            trips.append(cur)  # still open
    trips.sort(key=lambda r: (r.closed_at or 2**62, r.coin))
    return trips


# ---------------------------------------------------------------- exit episodes (FIFO)

EPISODE_GAP_MS = HOUR_MS


@dataclass
class Episode:
    """A cluster of position-reducing fills on one coin (gaps < 1h). This is a 'trade' for
    traders who scale in and out without ever going flat, which round trips can't see."""

    coin: str
    start: int
    end: int
    pnl: float = 0.0
    holds: list[tuple[float, float]] = field(default_factory=list)  # (hours, notional) pieces


def exit_episodes(fills: list[Fill]) -> list[Episode]:
    """FIFO lot matching per coin: increases add lots, reductions consume the oldest lots and
    record how long that size was held. Each fill's `startPosition` is trusted; if our lots
    disagree with it (history gap), they're replaced by one lot of unknown age."""
    by_coin: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_coin[f.coin].append(f)
    out: list[Episode] = []
    for coin, cf in by_coin.items():
        cf.sort(key=Fill.chrono_key)
        lots: list[list[float | None]] = []  # [open_time or None, size]
        lot_sign = 0
        cur: Episode | None = None
        for f in cf:
            before = float(f.startPosition)
            after = before + float(f.signed_sz)
            if abs(after) < 1e-12:
                after = 0.0
            sb = _sign(before)
            held = sum(float(lot[1] or 0) for lot in lots)
            if sb != lot_sign or abs(held - abs(before)) > 1e-9 * max(1.0, abs(before)):
                lots = [[None, abs(before)]] if before else []
                lot_sign = sb
            px = float(f.px)
            reduce = abs(before) if _sign(after) != sb else max(0.0, abs(before) - abs(after))
            if reduce > 0:
                if cur is None or cur.coin != coin or f.time - cur.end > EPISODE_GAP_MS:
                    if cur is not None:
                        out.append(cur)
                    cur = Episode(coin, f.time, f.time)
                cur.end = f.time
                cur.pnl += float(f.closedPnl) - float(f.fee)
                left = reduce
                while left > 1e-12 and lots:
                    t_open, size = lots[0]
                    take = min(left, float(size or 0))
                    if t_open is not None:
                        cur.holds.append(((f.time - t_open) / HOUR_MS, take * px))
                    lots[0][1] = float(size or 0) - take
                    left -= take
                    if (lots[0][1] or 0) <= 1e-12:
                        lots.pop(0)
            elif cur is not None and cur.coin == coin:
                cur.pnl -= float(f.fee)  # opening fees count against the next episode
            add = abs(after) if _sign(after) != sb else max(0.0, abs(after) - abs(before))
            if add > 0:
                if _sign(after) != lot_sign:
                    lots = []
                    lot_sign = _sign(after)
                lots.append([float(f.time), add])
            if after == 0:
                lot_sign = 0
        if cur is not None:
            out.append(cur)
    out.sort(key=lambda e: (e.end, e.coin))
    return out


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float | None:
    pairs = [(v, w) for v, w in pairs if w > 0]
    if not pairs:
        return None
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total:
            return v
    return pairs[-1][0]


# ---------------------------------------------------------------- fill metrics


@dataclass
class FillMetrics:
    n_fills: int
    n_trades: int  # completed round trips
    trades_per_day: float
    median_holding_hours: float | None
    p25_holding_hours: float | None
    maker_ratio: float
    top_trade_concentration: float
    avg_leverage: float
    max_leverage: float
    n_coins: int
    pct_volume_in_supported_coins: float
    active_days_last_30: int
    realized_pnl: float
    max_drawdown_fills: float
    history_truncated: bool
    coins: list[str] = field(default_factory=list)
    # Episode/FIFO-based alternatives (see exit_episodes); evaluated in docs/DECISIONS.md D13.
    n_episodes: int = 0
    episodes_per_day: float = 0.0
    median_hold_fifo_hours: float | None = None  # notional-weighted
    p25_hold_fifo_hours: float | None = None
    top_episode_concentration: float = 1.0


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def fill_metrics(
    fills: list[Fill],
    *,
    now_ms: int,
    lookback_days: int,
    supported_coins: set[str],
    account_value: list[tuple[int, float]],
    history_truncated: bool = False,
) -> FillMetrics:
    start = now_ms - lookback_days * DAY_MS
    perp = sorted((f for f in fills if f.is_perp and f.time >= start), key=Fill.chrono_key)
    trips = round_trips(perp)
    closed = [t for t in trips if t.closed_at is not None]
    holds = [h for t in closed if (h := t.holding_hours) is not None]

    positive = [t.pnl for t in closed if t.pnl > 0]
    total_pos = sum(positive)
    concentration = max(positive) / total_pos if total_pos > 0 else 1.0

    total_vol = 0.0
    supported_vol = 0.0
    maker = 0
    positions: dict[str, float] = {}
    last_px: dict[str, float] = {}
    lev_weighted = 0.0
    lev_weight = 0.0
    max_lev = 0.0
    realized = 0.0
    realized_index = 1.0
    realized_curve: list[tuple[int, float]] = [(start, 1.0)]
    days_30: set[int] = set()
    for f in perp:
        px, sz = float(f.px), float(f.sz)
        notional = px * sz
        total_vol += notional
        if f.coin in supported_coins:
            supported_vol += notional
        if not f.crossed:
            maker += 1
        positions[f.coin] = float(f.startPosition) + float(f.signed_sz)
        last_px[f.coin] = px
        gross = sum(abs(q) * last_px[c] for c, q in positions.items())
        av = value_at(account_value, f.time) or 0.0
        if av > 0:
            lev = gross / av
            lev_weighted += lev * notional
            lev_weight += notional
            max_lev = max(max_lev, lev)
        fill_pnl = float(f.closedPnl) - float(f.fee)
        realized += fill_pnl
        if av >= MIN_EQUITY_USD:
            realized_index *= 1 + max(fill_pnl / av, MAX_STEP_LOSS)
        realized_curve.append((f.time, realized_index))
        if f.time >= now_ms - 30 * DAY_MS:
            days_30.add(f.time // DAY_MS)

    episodes = exit_episodes(perp)
    pieces = [h for e in episodes for h in e.holds]
    ep_pos = [e.pnl for e in episodes if e.pnl > 0]
    ep_conc = max(ep_pos) / sum(ep_pos) if ep_pos else 1.0

    return FillMetrics(
        n_episodes=len(episodes),
        episodes_per_day=len(episodes) / lookback_days,
        median_hold_fifo_hours=_weighted_quantile(pieces, 0.5),
        p25_hold_fifo_hours=_weighted_quantile(pieces, 0.25),
        top_episode_concentration=ep_conc,
        n_fills=len(perp),
        n_trades=len(closed),
        trades_per_day=len(closed) / lookback_days,
        median_holding_hours=_quantile(holds, 0.5),
        p25_holding_hours=_quantile(holds, 0.25),
        maker_ratio=maker / len(perp) if perp else 0.0,
        top_trade_concentration=concentration,
        avg_leverage=lev_weighted / lev_weight if lev_weight else 0.0,
        max_leverage=max_lev,
        n_coins=len({f.coin for f in perp}),
        pct_volume_in_supported_coins=supported_vol / total_vol if total_vol else 0.0,
        active_days_last_30=len(days_30),
        realized_pnl=realized,
        max_drawdown_fills=max_drawdown(realized_curve),
        history_truncated=history_truncated,
        coins=sorted({f.coin for f in perp}),
    )


def looks_truncated(fills: list[Fill], start_ms: int, page_cap: int) -> bool:
    """Truncation is silent (D3): a wallet with at least ~5 full pages whose oldest available
    fill is more than a week younger than the requested start has lost history."""
    if len(fills) < 4 * page_cap:
        return False
    return min(f.time for f in fills) > start_ms + WEEK_MS
