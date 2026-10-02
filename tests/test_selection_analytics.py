import json
from itertools import pairwise
from pathlib import Path

import pytest

from copybot.clock import DAY_MS, HOUR_MS
from copybot.hl.models import Fills, Portfolio
from copybot.selection.analytics import (
    fill_metrics,
    joined_pnl,
    looks_truncated,
    max_drawdown,
    portfolio_metrics,
    round_trips,
    twr_index,
    value_at,
)
from tests.factories import NOW, fill, portfolio, steady_portfolio, trip

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------ series helpers


def test_value_at_interpolates_and_clamps() -> None:
    s = [(0, 0.0), (10, 10.0)]
    assert value_at(s, 5) == 5.0
    assert value_at(s, -5) == 0.0
    assert value_at(s, 50) == 10.0
    assert value_at([], 1) is None


def test_max_drawdown() -> None:
    assert max_drawdown([(0, 100), (1, 120), (2, 90), (3, 130), (4, 117)]) == pytest.approx(0.25)
    assert max_drawdown([(0, 1), (1, 2)]) == 0.0


def test_twr_ignores_deposits() -> None:
    """A deposit that doubles the account is not a return; PnL is measured against capital."""
    pnl = [(0, 0.0), (1, 10_000.0), (2, 10_000.0), (3, 30_000.0)]
    av = [(0, 100_000.0), (1, 110_000.0), (2, 220_000.0), (3, 240_000.0)]  # deposit at t=2
    idx = twr_index(pnl, av)
    # +10% then 0% then 20k on ~220k (+9.09%)
    assert idx[-1][1] == pytest.approx(1.10 * (1 + 20_000 / 220_000))


def test_twr_modified_dietz_counts_half_of_unseen_flows() -> None:
    # Lost 50k while depositing 100k inside one coarse interval.
    pnl = [(0, 0.0), (1, -50_000.0)]
    av = [(0, 100_000.0), (1, 150_000.0)]
    idx = twr_index(pnl, av)
    assert idx[-1][1] == pytest.approx(1 - 50_000 / (100_000 + 0.5 * 100_000))


def test_twr_uses_total_pnl_for_flows() -> None:
    """Spot PnL moves the total account value; it must not be mistaken for a deposit."""
    perp = [(0, 0.0), (1, 1_000.0)]
    total = [(0, 0.0), (1, 51_000.0)]  # +50k spot gains
    av = [(0, 100_000.0), (1, 151_000.0)]
    idx = twr_index(perp, av, total)
    assert idx[-1][1] == pytest.approx(1.01)


def test_twr_skips_near_empty_account() -> None:
    idx = twr_index([(0, 0.0), (1, 500.0)], [(0, 10.0), (1, 510.0)])
    assert idx[-1][1] == 1.0


def test_joined_pnl_offsets_month_onto_alltime() -> None:
    p = steady_portfolio(days=100, daily_pnl=1_000)
    j = joined_pnl(p)
    # cumulative series must be continuous and monotone for a steady trader
    assert all(b[1] >= a[1] for a, b in pairwise(j))
    assert j[-1][1] == pytest.approx(100 * 1_000, rel=1e-6)


# ------------------------------------------------------------ portfolio metrics


def test_portfolio_metrics_steady_trader() -> None:
    pm = portfolio_metrics(steady_portfolio(days=120, daily_pnl=2_000), NOW, 90)
    assert pm.pnl_90d == pytest.approx(180_000, rel=1e-3)
    assert pm.max_drawdown_90d == pytest.approx(0.0, abs=1e-9)
    assert pm.positive_weeks_ratio == 1.0
    assert 0.15 < pm.return_90d < 0.20
    assert len(pm.weekly_pnl) == 12


def test_portfolio_metrics_drawdown_and_weeks() -> None:
    pts = list(range(NOW - 100 * DAY_MS, NOW + 1, DAY_MS))
    # up 1%/day of 1M for 50 days, then down 3k/day
    pnl = []
    for i, t in enumerate(pts):
        pnl.append((t, 10_000 * min(i, 50) - 3_000 * max(0, i - 50)))
    av = [(t, 1_000_000 + v) for t, v in pnl]
    pm = portfolio_metrics(portfolio(av, pnl), NOW, 90)
    assert pm.max_drawdown_90d > 0.08
    assert pm.positive_weeks_ratio < 0.5


def test_portfolio_metrics_real_fixture_is_sane() -> None:
    p = Portfolio.model_validate(json.loads((FIX / "portfolio.json").read_text()))
    last = max(t for t, _ in p.window("month").accountValueHistory)  # type: ignore[union-attr]
    pm = portfolio_metrics(p, last, 90)
    assert 0 <= pm.max_drawdown_90d <= 1
    assert 0 <= pm.positive_weeks_ratio <= 1
    assert pm.history_days > 0


# ------------------------------------------------------------ round trips


def test_round_trip_open_close() -> None:
    t0 = NOW - 10 * DAY_MS
    trips = round_trips(trip("BTC", t_open=t0, hours=6, px_open=100, px_close=110))
    assert len(trips) == 1
    assert trips[0].holding_hours == pytest.approx(6)
    assert trips[0].pnl == pytest.approx(10)
    assert trips[0].direction == 1


def test_round_trip_partial_fills_and_flip() -> None:
    t = NOW - 5 * DAY_MS
    fills = [
        fill(t=t, start=0, sz=1, side="B"),
        fill(t=t + HOUR_MS, start=1, sz=1, side="B"),  # increase
        fill(t=t + 2 * HOUR_MS, start=2, sz=1, side="A", closed_pnl=5),  # decrease
        fill(t=t + 3 * HOUR_MS, start=1, sz=3, side="A", closed_pnl=7),  # flip to -2
        fill(t=t + 5 * HOUR_MS, start=-2, sz=2, side="B", closed_pnl=-1),  # close short
    ]
    trips = round_trips(fills)
    assert [(tr.direction, tr.holding_hours, tr.pnl) for tr in trips] == [
        (1, 3.0, 12.0),
        (-1, 2.0, -1.0),
    ]


def test_round_trip_preexisting_position_has_unknown_start() -> None:
    trips = round_trips([fill(t=NOW - DAY_MS, start=2, sz=2, side="A", closed_pnl=3)])
    assert trips[0].opened_at is None and trips[0].holding_hours is None
    assert trips[0].closed_at is not None


def test_round_trip_gap_does_not_leak_pnl() -> None:
    t = NOW - DAY_MS
    fills = [
        fill(t=t, start=0, sz=1, side="B"),
        # position silently went to 0 (unseen); next fill opens fresh short
        fill(t=t + HOUR_MS, start=0, sz=1, side="A"),
        fill(t=t + 2 * HOUR_MS, start=-1, sz=1, side="B", closed_pnl=4),
    ]
    trips = round_trips(fills)
    closed = [tr for tr in trips if tr.closed_at is not None]
    assert len(closed) == 1 and closed[0].direction == -1 and closed[0].pnl == 4
    stale = [tr for tr in trips if tr.closed_at is None]
    assert stale and stale[0].direction == 1


def test_open_trip_not_counted_as_closed() -> None:
    trips = round_trips([fill(t=NOW - DAY_MS, start=0, sz=1, side="B")])
    assert trips[0].closed_at is None


# ------------------------------------------------------------ fill metrics


def _metrics(fills, **kw):  # type: ignore[no-untyped-def]
    av = [(NOW - 100 * DAY_MS, 100_000.0), (NOW, 100_000.0)]
    args = {
        "now_ms": NOW,
        "lookback_days": 90,
        "supported_coins": {"BTC", "ETH"},
        "account_value": av,
    }
    args.update(kw)
    return fill_metrics(fills, **args)


def test_fill_metrics_basic() -> None:
    fills = []
    for i in range(30):
        fills += trip(
            "BTC", t_open=NOW - (60 - i) * DAY_MS, hours=1 + i / 2, px_close=101, crossed=i % 3 != 0
        )
    m = _metrics(fills)
    assert m.n_trades == 30
    assert m.trades_per_day == pytest.approx(30 / 90)
    assert m.median_holding_hours == pytest.approx(1 + 14.5 / 2)
    assert m.maker_ratio == pytest.approx(1 / 3)
    assert m.top_trade_concentration == pytest.approx(1 / 30)
    assert m.pct_volume_in_supported_coins == 1.0
    assert m.n_coins == 1
    assert m.max_leverage == pytest.approx(100 * 1 / 100_000)


def test_fill_metrics_concentration_and_unsupported_volume() -> None:
    fills = trip("BTC", t_open=NOW - 20 * DAY_MS, hours=5, px_close=200)  # +100
    fills += trip("XYZ", t_open=NOW - 10 * DAY_MS, hours=5, px_close=101)  # +1, unsupported
    m = _metrics(fills)
    assert m.top_trade_concentration == pytest.approx(100 / 101)
    assert 0 < m.pct_volume_in_supported_coins < 1


def test_fill_metrics_active_days_and_window() -> None:
    fills = trip("BTC", t_open=NOW - 200 * DAY_MS, hours=1)  # outside lookback
    for d in range(10):
        fills += trip("BTC", t_open=NOW - (d + 1) * DAY_MS + HOUR_MS, hours=1)
    m = _metrics(fills)
    assert m.n_trades == 10
    assert m.active_days_last_30 == 10


def test_fill_metrics_ignores_spot() -> None:
    m = _metrics(trip("@107", t_open=NOW - DAY_MS, hours=1))
    assert m.n_fills == 0 and m.n_trades == 0


def test_leverage_uses_gross_notional_over_equity() -> None:
    t = NOW - DAY_MS
    fills = [
        fill("BTC", t=t, start=0, sz=1000, side="B", px=100),  # 100k long
        fill("ETH", t=t + 1, start=0, sz=1000, side="A", px=100),  # 100k short
    ]
    m = _metrics(fills)
    assert m.max_leverage == pytest.approx(2.0)


def test_real_fills_fixture() -> None:
    fills = Fills.model_validate(json.loads((FIX / "userFillsByTime.json").read_text())).root
    end = max(f.time for f in fills)
    m = fill_metrics(
        fills,
        now_ms=end,
        lookback_days=7,
        supported_coins={f.coin for f in fills},
        account_value=[(end - 10 * DAY_MS, 1e6), (end, 1e6)],
    )
    assert m.n_fills == sum(f.is_perp for f in fills)
    assert 0 <= m.maker_ratio <= 1


def test_looks_truncated() -> None:
    start = NOW - 90 * DAY_MS
    many = [fill(t=NOW - DAY_MS + i, start=0, sz=1, side="B") for i in range(8000)]
    assert looks_truncated(many, start, 2000)
    assert not looks_truncated(many[:100], start, 2000)
