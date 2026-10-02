import json
from pathlib import Path

import pytest

from copybot.clock import DAY_MS, HOUR_MS
from copybot.config import Config
from copybot.hl.models import Candles
from copybot.selection.copyability import PriceSeries, jitter_ms, simulate_copy
from tests.factories import NOW, candles, fill

START = NOW - 10 * DAY_MS
AV = [(START - DAY_MS, 100_000.0), (NOW, 100_000.0)]


def flat(px: float = 100.0):  # type: ignore[no-untyped-def]
    return lambda t: px


FLAT = flat()


def sim(fills, price=FLAT, cfg=None, coin="BTC", **kw):  # type: ignore[no-untyped-def]
    prices = {coin: PriceSeries(candles(coin, START - HOUR_MS, NOW + HOUR_MS, price))}
    return simulate_copy(
        fills,
        prices=prices,
        trader_account_value=AV,
        cfg=cfg or Config(),
        start_ms=START,
        end_ms=NOW,
        seed="0xseed",
        **kw,
    )


def test_price_series_interpolates_within_candle() -> None:
    ps = PriceSeries(candles("BTC", 0, 2 * HOUR_MS, lambda t: 100 + t / HOUR_MS * 10))
    assert ps.at(HOUR_MS // 2) == pytest.approx(105, rel=1e-3)
    assert ps.at(-5) == 100


def test_price_series_real_fixture() -> None:
    cs = Candles.model_validate(
        json.loads((Path(__file__).parent / "fixtures" / "candles_BTC_1h.json").read_text())
    ).root
    ps = PriceSeries(cs)
    mid = cs[len(cs) // 2]
    v = ps.at(mid.t + 1)
    assert v is not None and float(mid.l) * 0.99 <= v <= float(mid.h) * 1.01


def test_jitter_deterministic_and_bounded() -> None:
    assert jitter_ms("a", 5) == jitter_ms("a", 5)
    assert all(0 <= jitter_ms("a", k) <= 180_000 for k in range(200))


def test_flat_price_round_trip_costs_fees_and_slippage() -> None:
    t = START + DAY_MS
    fills = [
        fill(t=t, start=0, sz=100, side="B"),  # trader 10k long on 100k = 10% exposure
        fill(t=t + 5 * HOUR_MS, start=100, sz=100, side="A"),
    ]
    r = sim(fills)
    assert r.n_orders == 2
    # 1,000 notional each way, (8bps slippage + 4.5bps fee) per side
    assert r.copy_return == pytest.approx(-2 * 1_000 * 12.5e-4 / 10_000, rel=0.05)


def test_trend_profit_is_captured() -> None:
    t = START + DAY_MS
    up = lambda ts: 100 + max(0, ts - t) / HOUR_MS  # +1/hour after entry  # noqa: E731
    fills = [
        fill(t=t, start=0, sz=100, side="B", px=100),
        fill(t=t + 48 * HOUR_MS, start=100, sz=100, side="A", px=148, closed_pnl=4800),
    ]
    r = sim(fills, price=up)
    assert r.copy_return > 0.03  # ~10% exposure x ~47% move, less lag and costs


def test_position_cap_20pct() -> None:
    t = START + DAY_MS
    up = lambda ts: 100 + max(0, ts - t) / HOUR_MS  # noqa: E731
    # Trader goes 3x levered long (300k on 100k): we cap at 20% of equity.
    fills = [fill(t=t, start=0, sz=3000, side="B", px=100)]
    r = sim(fills, price=up)
    move = (up(NOW) - up(t)) / up(t)
    assert r.copy_return == pytest.approx(0.20 * move, rel=0.1)


def test_late_entry_skipped() -> None:
    t = START + DAY_MS
    # Market is already 5% above the trader's fill price by the time we see it.
    jump = lambda ts: 100 if ts < t - 2 * HOUR_MS else 105  # noqa: E731
    r = sim([fill(t=t, start=0, sz=100, side="B", px=100)], price=jump)
    assert r.skipped["late_entry"] == 1 and r.n_orders == 0


def test_close_never_skipped_for_drift() -> None:
    t = START + DAY_MS
    jump = lambda ts: 100 if ts <= t + HOUR_MS else 90  # noqa: E731
    fills = [
        fill(t=t, start=0, sz=100, side="B", px=100),
        fill(t=t + 2 * HOUR_MS, start=100, sz=100, side="A", px=95, closed_pnl=-500),
    ]
    r = sim(fills, price=jump)
    assert r.n_orders == 2


def test_tiny_target_below_min_size() -> None:
    # 0.05% exposure of 10k = $5 < $10 minimum
    r = sim([fill(t=START + DAY_MS, start=0, sz=0.5, side="B")])
    assert r.n_orders == 0


def test_small_change_below_rebalance_threshold() -> None:
    t = START + DAY_MS
    fills = [
        fill(t=t, start=0, sz=100, side="B"),
        fill(t=t + HOUR_MS, start=100, sz=5, side="B"),  # +5% of position < 10% threshold
    ]
    r = sim(fills)
    assert r.n_orders == 1
    assert r.skipped["below_rebalance_threshold"] == 1


def test_lag_groups_fills_into_one_poll() -> None:
    t = START + DAY_MS - DAY_MS % (5 * 60_000)
    fills = [fill(t=t + i * 1000, start=i * 10, sz=10, side="B") for i in range(10)]
    r = sim(fills)
    assert r.n_orders == 1  # all seen at the same poll → one order to the final target


def test_unsupported_coin_ignored() -> None:
    r = sim([fill("XYZ", t=START + DAY_MS, start=0, sz=100, side="B")])
    assert r.n_orders == 0


def test_capture_ratio() -> None:
    r = sim([])
    assert r.capture_ratio(-0.1) == 0.0
    assert r.capture_ratio(0.1) == pytest.approx(0.0)


def test_ideal_benchmark_beats_lagged_on_trend() -> None:
    t = START + DAY_MS
    up = lambda ts: 100 + max(0, ts - t) / HOUR_MS  # noqa: E731
    fills = [
        fill(t=t, start=0, sz=100, side="B", px=100),
        fill(t=t + 48 * HOUR_MS, start=100, sz=100, side="A", px=148, closed_pnl=4800),
    ]
    lagged = sim(fills, price=up)
    ideal = sim(fills, price=up, ideal=True)
    # ideal: 10% of 10k at 100 -> 148, no costs = +4.8%
    assert ideal.copy_return == pytest.approx(0.048, rel=1e-6)
    assert 0 < lagged.copy_return < ideal.copy_return
    assert ideal.fees == 0
