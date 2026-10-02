"""Stage 3: replay-with-lag copyability simulation (§6.4).

Each trader fill becomes visible to us only at the next poll boundary after it happened, plus a
0-3 minute Actions jitter (deterministic per wallet and poll). We then move our position toward
the trader's *current* exposure fraction using the §8.1 sizing rules on a $10,000 sleeve, at the
historical price at that moment plus fixed slippage and taker fees.

Approximations (D8): historical price = linear interpolation between the open and close of the
1h candle containing the moment (finer candles don't reach back 90 days). Funding is ignored
here (the Phase 3 backtest includes it). The sim starts flat, so positions the trader already
held before the window are only copied once the trader trades that coin.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field

from copybot.clock import HOUR_MS
from copybot.config import Config
from copybot.hl.models import Candle, Fill
from copybot.selection.analytics import max_drawdown, value_at

MIN_ORDER_USD = 10.0  # Hyperliquid's minimum order notional (verify; §8.1)
MAX_JITTER_MS = 3 * 60_000


class PriceSeries:
    """Price lookup from 1h candles: interpolate open→close inside the candle."""

    def __init__(self, candles: list[Candle]) -> None:
        cs = sorted(candles, key=lambda c: c.t)
        self._t = [c.t for c in cs]
        self._c = [(c.t, c.T, float(c.o), float(c.c)) for c in cs]

    def __bool__(self) -> bool:
        return bool(self._c)

    def at(self, t: int) -> float | None:
        if not self._c:
            return None
        i = bisect.bisect_right(self._t, t) - 1
        if i < 0:
            return self._c[0][2]
        t0, t1, o, c = self._c[i]
        if t >= t1:
            return c
        return o + (c - o) * (t - t0) / max(t1 - t0, 1)

    def hourly_closes(self, start: int, end: int) -> list[tuple[int, float]]:
        return [(t1, c) for _, t1, _, c in self._c if start <= t1 <= end]


def jitter_ms(seed: str, poll_index: int) -> int:
    h = hashlib.sha256(f"{seed}:{poll_index}".encode()).digest()
    return int.from_bytes(h[:4], "big") % (MAX_JITTER_MS + 1)


@dataclass
class CopyResult:
    copy_return: float
    copy_max_drawdown: float
    n_orders: int
    fees: float
    avg_lag_cost_bps: float
    skipped: Counter[str] = field(default_factory=Counter)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)

    def capture_ratio(self, trader_return: float) -> float:
        if trader_return <= 0:
            return 0.0
        return self.copy_return / trader_return


def simulate_copy(
    fills: list[Fill],
    *,
    prices: dict[str, PriceSeries],
    trader_account_value: list[tuple[int, float]],
    cfg: Config,
    start_ms: int,
    end_ms: int,
    seed: str,
    sleeve: float = 10_000.0,
    ideal: bool = False,
) -> CopyResult:
    """`ideal=True` is the no-lag, no-cost benchmark: every fill is copied the instant it
    happens, at the trader's own fill price, with identical sizing and caps. Lagged return
    divided by ideal return isolates what our polling lag costs (D13)."""
    poll_ms = cfg.schedule.poll_minutes * 60_000
    slip = 0.0 if ideal else float(cfg.costs.backtest_slippage_bps) / 1e4
    fee_rate = 0.0 if ideal else float(cfg.costs.taker_fee_bps) / 1e4
    max_pos_frac = float(cfg.risk.max_position_pct) / 100
    rebalance_pct = float(cfg.signals.rebalance_threshold_pct) / 100
    rebalance_usd = float(cfg.signals.rebalance_threshold_usd)
    max_drift = float(cfg.signals.max_entry_drift_pct) / 100
    mult = float(cfg.portfolio.copy_multiplier)

    # Group fills into the poll at which we'd first see them.
    batches: dict[int, list[Fill]] = {}
    for f in fills:
        if not (start_ms <= f.time <= end_ms) or f.coin not in prices:
            continue
        k = f.time if ideal else math.floor(f.time / poll_ms) + 1
        batches.setdefault(k, []).append(f)

    cash = sleeve
    pos: dict[str, float] = {}  # coin -> signed size
    skipped: Counter[str] = Counter()
    n_orders = 0
    fees = 0.0
    lag_costs: list[float] = []
    curve: list[tuple[int, float]] = [(start_ms, sleeve)]

    def equity(t: int) -> float:
        total = cash
        for c, q in pos.items():
            px = prices[c].at(t)
            if px is not None:
                total += q * px
        return total

    if ideal:
        events = sorted((k, k) for k in batches)
    else:
        events = sorted((k * poll_ms + jitter_ms(seed, k), k) for k in batches)
    hour_marks = list(range(start_ms - start_ms % HOUR_MS + HOUR_MS, end_ms, HOUR_MS))
    hi = 0
    for t_vis, k in events:
        while hi < len(hour_marks) and hour_marks[hi] < t_vis:
            if pos:
                curve.append((hour_marks[hi], equity(hour_marks[hi])))
            hi += 1
        batch = sorted(batches[k], key=Fill.chrono_key)
        last_by_coin: dict[str, Fill] = {}
        for f in batch:
            last_by_coin[f.coin] = f
        trader_av = value_at(trader_account_value, t_vis) or 0.0
        if trader_av <= 0:
            skipped["no_trader_equity"] += len(last_by_coin)
            continue
        eq = equity(t_vis)
        for coin, f in sorted(last_by_coin.items()):
            px = float(f.px) if ideal else prices[coin].at(t_vis)
            if px is None or px <= 0:
                skipped["no_price"] += 1
                continue
            trader_pos = float(f.startPosition) + float(f.signed_sz)
            exposure = trader_pos * px / trader_av
            target_notional = eq * exposure * mult
            cap = max_pos_frac * eq
            target_notional = max(-cap, min(cap, target_notional))
            if abs(target_notional) < MIN_ORDER_USD:
                target_notional = 0.0
            cur = pos.get(coin, 0.0)
            target = target_notional / px
            delta = target - cur
            delta_usd = abs(delta) * px
            closing = target == 0.0 and cur != 0.0
            if delta_usd == 0:
                continue
            if not closing:
                threshold = max(rebalance_pct * abs(cur) * px, rebalance_usd)
                if delta_usd < threshold:
                    skipped["below_rebalance_threshold"] += 1
                    continue
                if delta_usd < MIN_ORDER_USD:
                    skipped["min_size"] += 1
                    continue
                increasing = abs(target) > abs(cur) and (cur == 0 or (target > 0) == (cur > 0))
                if increasing and not ideal:
                    trader_px = float(f.px)
                    drift = (px - trader_px) / trader_px * (1 if target > 0 else -1)
                    if drift > max_drift:
                        skipped["late_entry"] += 1
                        continue
            side = 1 if delta > 0 else -1
            fill_px = px * (1 + side * slip)
            fee = abs(delta) * fill_px * fee_rate
            cash -= delta * fill_px + fee
            fees += fee
            n_orders += 1
            lag_costs.append((fill_px - float(f.px)) / float(f.px) * side * 1e4)
            new = cur + delta
            if abs(new) * px < 1e-9:
                pos.pop(coin, None)
            else:
                pos[coin] = new
        curve.append((t_vis, equity(t_vis)))
    for t in hour_marks[hi:]:
        if pos:
            curve.append((t, equity(t)))
    final = equity(end_ms)
    curve.append((end_ms, final))
    return CopyResult(
        copy_return=final / sleeve - 1,
        copy_max_drawdown=max_drawdown(curve),
        n_orders=n_orders,
        fees=fees,
        avg_lag_cost_bps=sum(lag_costs) / len(lag_costs) if lag_costs else 0.0,
        skipped=skipped,
        equity_curve=curve,
    )
